#!/usr/bin/env python3
"""
3DS FTP Browser

A small local web app for browsing and managing the SD card on a 3DS running
an FTP homebrew app (ftpd, FTPD Pro, etc.). Browsers can't speak FTP, so this
script runs a tiny web server on your computer, serves the page, and relays
each request to the 3DS over FTP.

Usage:
    python3 3ds_ftp_browser.py
Then open http://127.0.0.1:8021 (it tries to open on its own).

Needs Python 3.8 or newer. No extra packages.
"""

import atexit
import ftplib
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

LISTEN_HOST = "127.0.0.1"   # only your computer can open the page
LISTEN_PORT = 8021
ALLOWED_HOSTS = {f"127.0.0.1:{LISTEN_PORT}", f"localhost:{LISTEN_PORT}"}

# Windows reads these from the registry, which is sometimes wrong. Pin them.
for _ext, _type in {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".bmp": "image/bmp", ".webp": "image/webp", ".txt": "text/plain", ".ini": "text/plain",
}.items():
    mimetypes.add_type(_type, _ext)


# --------------------------------------------------------------------------
# FTP side
# --------------------------------------------------------------------------

class FTPSession:
    """One shared FTP connection, guarded by a lock, that reconnects if the
    3DS drops it (ftpd closes idle connections after a while)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ftp = None
        self.cfg = None

    def connect(self, host, port, user, password):
        with self.lock:
            self._close()
            self.cfg = dict(host=host, port=port, user=user, password=password)
            EXTRA["ok"] = True
            try:
                self._open()
            except Exception:
                self.cfg = None  # don't keep retrying an address that didn't work
                raise
            return self.ftp.getwelcome()

    def disconnect(self):
        with self.lock:
            if self.ftp:
                try:
                    self.ftp.quit()
                except ftplib.all_errors:
                    pass
            self._close()
            self.cfg = None

    def _open(self):
        c = self.cfg
        ftp = ftplib.FTP()
        ftp.connect(c["host"], c["port"], timeout=15)
        ftp.login(c["user"] or "anonymous", c["password"] or "")
        ftp.set_pasv(True)
        self.ftp = ftp

    def _close(self):
        if self.ftp:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None

    def run(self, fn):
        with self.lock:
            if not self.cfg:
                raise RuntimeError("Not connected. Enter the 3DS address and connect first.")
            # Check the link is alive; reconnect once if it isn't.
            try:
                if self.ftp is None:
                    raise EOFError
                self.ftp.voidcmd("NOOP")
            except ftplib.all_errors:
                self._close()
                self._open()
            try:
                return fn(self.ftp)
            except (EOFError, OSError, ftplib.error_temp, ftplib.error_reply):
                self._close()  # next request will reconnect
                raise


def list_dir(ftp, path):
    entries = []
    try:
        for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
            kind = facts.get("type", "")
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            entries.append({
                "name": name,
                "dir": kind == "dir",
                "size": int(facts.get("size") or 0),
                "modified": facts.get("modify", ""),
            })
    except ftplib.error_perm:
        # Server without MLSD: fall back to parsing a Unix-style LIST.
        lines = []
        ftp.retrlines("LIST " + path, lines.append)
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9 or parts[8] in (".", ".."):
                continue
            entries.append({
                "name": parts[8],
                "dir": line.startswith("d"),
                "size": int(parts[4]) if parts[4].isdigit() else 0,
                "modified": " ".join(parts[5:8]),
            })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries


def delete_path(ftp, path, is_dir):
    if not is_dir:
        ftp.delete(path)
        return
    for e in list_dir(ftp, path):
        delete_path(ftp, posixpath.join(path, e["name"]), e["dir"])
    ftp.rmd(path)


def dir_usage(ftp, path):
    """Total bytes, file count and folder count under a folder."""
    total = files = folders = 0
    for e in list_dir(ftp, path):
        if e["dir"]:
            s, f, d = dir_usage(ftp, posixpath.join(path, e["name"]))
            total, files, folders = total + s, files + f, folders + d + 1
        else:
            total, files = total + e["size"], files + 1
    return total, files, folders


def walk_files(ftp, path, rel):
    """Everything under a folder as (remote path, name in zip, size, modified, is_dir)."""
    items = []
    entries = list_dir(ftp, path)
    if not entries:
        items.append((None, rel + "/", 0, "", True))
    for e in entries:
        remote = posixpath.join(path, e["name"])
        arc = rel + "/" + e["name"]
        if e["dir"]:
            items += walk_files(ftp, remote, arc)
        else:
            items.append((remote, arc, e["size"], e["modified"], False))
    return items


def zip_time(modified):
    try:
        t = time.strptime(modified[:14], "%Y%m%d%H%M%S")
        if t.tm_year >= 1980:
            return t[:6]
    except (ValueError, TypeError):
        pass
    return time.localtime()[:6]


def stat_paths(ftp, paths):
    """Which of these paths exist, and whether each is a folder."""
    by_dir = {}
    for p in paths:
        p = p.rstrip("/")
        if p:
            by_dir.setdefault(posixpath.dirname(p) or "/", []).append(p)
    found = {}
    for d, ps in by_dir.items():
        try:
            # SD cards use FAT32, which ignores upper/lower case in names.
            listing = {e["name"].lower(): e for e in list_dir(ftp, d)}
        except ftplib.error_perm:
            continue  # parent folder isn't there
        for p in ps:
            e = listing.get(posixpath.basename(p).lower())
            if e:
                found[p] = {"dir": e["dir"], "size": e["size"], "modified": e["modified"]}
    return found


def existing_files(ftp, paths):
    return {p: v for p, v in stat_paths(ftp, paths).items() if not v["dir"]}


def find_files(ftp, roots, exts):
    """Every file under these folders with one of these extensions."""
    out = []

    def walk(path):
        for e in list_dir(ftp, path):
            p = posixpath.join(path, e["name"])
            if e["dir"]:
                walk(p)
            elif posixpath.splitext(e["name"])[1][1:].lower() in exts:
                out.append({"path": p, "name": e["name"], "size": e["size"], "modified": e["modified"]})

    for r in roots:
        try:
            walk(r)
        except ftplib.error_perm:
            pass  # folder isn't on this SD card
    return out


def zip_items(ftp, spec):
    """spec is a list of (path, is_dir, name in zip)."""
    items = []
    for path, is_dir, arc in spec:
        if is_dir:
            items += walk_files(ftp, path, arc)
        else:
            try:
                size = ftp.size(path) or 0
            except ftplib.all_errors:
                size = 0
            try:
                modified = ftp.sendcmd("MDTM " + path)[4:].strip()
            except ftplib.all_errors:
                modified = ""
            items.append((path, arc, size, modified, False))
    return items


def safe_member(name):
    """Clean up a path from inside a zip; None if it tries to escape."""
    parts = [x for x in name.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


RESTORES = {}  # token -> temp zip waiting to be restored


@atexit.register
def _clean_restores():
    for tmp in RESTORES.values():
        try:
            os.remove(tmp)
        except OSError:
            pass


def make_dirs(ftp, paths):
    for p in sorted(set(paths), key=lambda x: x.count("/")):
        try:
            ftp.mkd(p)
        except ftplib.error_perm:
            pass  # already exists


def local_subnets():
    ordered = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # picks the main network adapter; sends nothing
        ordered.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ordered += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    mine = set(ordered)
    nets = []
    for ip in ordered:  # main adapter's network first, then any others
        if ip.startswith(("127.", "169.254.")):
            continue
        base = ip.rsplit(".", 1)[0]
        if base not in nets:
            nets.append(base)
    return nets[:4], mine


def probe(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=0.6) as s:
            s.settimeout(1.5)
            banner = s.recv(256).decode("latin-1").strip()
            if banner.startswith("220"):
                return {"host": ip, "port": port, "banner": banner[4:80].strip()}
    except OSError:
        pass
    return None


def scan_network(port):
    nets, mine = local_subnets()
    targets = [f"{n}.{i}" for n in nets for i in range(1, 255) if f"{n}.{i}" not in mine]
    with ThreadPoolExecutor(max_workers=96) as pool:
        return [r for r in pool.map(lambda ip: probe(ip, port), targets) if r]


class LimitedReader:
    """Lets storbinary read exactly Content-Length bytes from the request."""

    def __init__(self, f, n):
        self.f, self.left = f, n

    def read(self, size=-1):
        if self.left <= 0:
            return b""
        if size < 0 or size > self.left:
            size = self.left
        data = self.f.read(size)
        self.left -= len(data)
        return data


SESSION = FTPSession()

# Downloads, previews, music and zips use their own short-lived FTP
# connection so browsing doesn't wait on them. If the 3DS won't take a
# second connection, fall back to the shared one.
STREAM_SLOTS = threading.BoundedSemaphore(2)
EXTRA = {"ok": True}


def open_extra():
    c = SESSION.cfg
    if not c:
        raise RuntimeError("Not connected. Enter the 3DS address and connect first.")
    ftp = ftplib.FTP()
    ftp.connect(c["host"], c["port"], timeout=8)
    ftp.login(c["user"] or "anonymous", c["password"] or "")
    ftp.set_pasv(True)
    return ftp


def with_stream_ftp(fn):
    with STREAM_SLOTS:
        ftp = None
        if EXTRA["ok"]:
            try:
                ftp = open_extra()
            except (RuntimeError,):
                raise
            except Exception:
                EXTRA["ok"] = False
        if ftp is None:
            return SESSION.run(fn)
        try:
            return fn(ftp)
        finally:
            try:
                ftp.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Web side
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "3DSFTPBrowser/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        self._started = True
        super().send_response(code, message)

    def _blocked(self, is_api):
        """Refuse requests that didn't come from this app's own page.

        The Host check stops "DNS rebinding", where a website tricks the
        browser into treating it as 127.0.0.1. The Origin and Sec-Fetch-Site
        checks stop other websites from sending requests here in the
        background, like deleting files.
        """
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return True
        if not is_api:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + h for h in ALLOWED_HOSTS}:
            return True
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return True
        return False

    def _refuse(self):
        data = b"Forbidden"
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _fail(self, e):
        if getattr(self, "_started", False):
            # Headers already went out (partway through a download or zip).
            # Adding an error now would corrupt the file, so just hang up.
            self.close_connection = True
            return
        msg = str(e) or e.__class__.__name__
        if isinstance(e, (TimeoutError, ConnectionRefusedError)) or "timed out" in msg:
            msg += ". Check the IP and port shown on the 3DS screen, and that the 3DS and this computer are on the same Wi-Fi."
        try:
            self._json({"error": msg}, 400)
        except OSError:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if self._blocked(u.path.startswith("/api/")):
            return self._refuse()
        try:
            if u.path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/list":
                path = q.get("path", ["/"])[0]
                self._json({"path": path, "entries": SESSION.run(lambda f: list_dir(f, path))})
            elif u.path == "/api/download":
                self._download(q["path"][0], inline="inline" in q, cache="v" in q)
            elif u.path == "/api/zip":
                path = q.get("path", ["/"])[0]
                base = posixpath.basename(path.rstrip("/")) or "SD card"
                self._send_zip([(path, True, base)], base + ".zip")
            elif u.path == "/api/find":
                roots = q.get("root", [])
                exts = set(q.get("ext", [""])[0].lower().split(","))
                self._json({"files": SESSION.run(lambda f: find_files(f, roots, exts))})
            elif u.path == "/api/du":
                path = q["path"][0]
                size, files, folders = SESSION.run(lambda f: dir_usage(f, path))
                self._json({"size": size, "files": files, "folders": folders})
            elif u.path == "/api/scan":
                self._json({"found": scan_network(int(q.get("port", ["5000"])[0]))})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # browser cancelled a download
        except Exception as e:
            self._fail(e)

    def _download(self, path, inline=False, cache=False):
        name = posixpath.basename(path)
        rng = (self.headers.get("Range") or "").strip()

        def fn(ftp):
            ftp.voidcmd("TYPE I")
            try:
                size = ftp.size(path)
            except ftplib.all_errors:
                size = None
            start, end, partial = 0, None, False
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng) if rng and size else None
            if m and (m.group(1) or m.group(2)):
                a, b = m.groups()
                if a:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                else:
                    start, end = max(0, size - int(b)), size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
            try:
                conn, _ = ftp.ntransfercmd("RETR " + path, start or None)
            except ftplib.error_perm:
                if not start:
                    raise
                start, end, partial = 0, None, False  # server can't resume; send it all
                conn, _ = ftp.ntransfercmd("RETR " + path)

            self.send_response(206 if partial else 200)
            if inline:
                self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
                self.send_header("Content-Disposition", "inline")
                self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
            else:
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
            elif size is not None:
                self.send_header("Content-Length", str(size))
            self.end_headers()

            remaining = end - start + 1 if partial else None
            try:
                while remaining is None or remaining > 0:
                    chunk = conn.recv(65536 if remaining is None else min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
            finally:
                conn.close()
            early = partial and end < size - 1
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                if early:
                    raise EOFError("stopped partway")  # makes the shared link reconnect cleanly
                raise

        with_stream_ftp(fn)

    def _send_zip(self, spec, zipname):
        def fn(ftp):
            ftp.voidcmd("TYPE I")
            items = zip_items(ftp, spec)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(zipname))
            self.end_headers()
            # Streams straight to the browser; zipfile handles a non-seekable output.
            # If a file fails partway, we skip zf.close() on purpose: without its
            # closing index the zip won't open, instead of opening with a file
            # quietly cut short.
            zf = zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_DEFLATED, compresslevel=1)
            try:
                for remote, arc, size, modified, is_dir in items:
                    if is_dir:
                        zf.writestr(zipfile.ZipInfo(arc, zip_time(modified)), b"")
                        continue
                    info = zipfile.ZipInfo(arc, zip_time(modified))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with zf.open(info, "w", force_zip64=size > 2_000_000_000) as out:
                        ftp.retrbinary("RETR " + remote, out.write, blocksize=65536)
            except BaseException:
                zf.fp = None  # stops zipfile from writing the index when it's cleaned up
                raise
            zf.close()

        with_stream_ftp(fn)

    def _restore_inspect(self):
        left = int(self.headers.get("Content-Length") or 0)
        fd, tmp = tempfile.mkstemp(prefix="3ds-restore-", suffix=".zip")
        with os.fdopen(fd, "wb") as out:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        try:
            zf = zipfile.ZipFile(tmp)
        except zipfile.BadZipFile:
            os.remove(tmp)
            raise RuntimeError("That file isn't a zip, or it's damaged.")
        files = []
        with zf:
            for info in zf.infolist():
                rel = safe_member(info.filename)
                if rel and not info.is_dir():
                    files.append({"path": "/" + rel, "size": info.file_size,
                                  "date": "%04d%02d%02d%02d%02d%02d" % info.date_time})
        try:
            existing = SESSION.run(lambda f: existing_files(f, [x["path"] for x in files])) if files else {}
        except Exception:
            os.remove(tmp)
            raise
        token = secrets.token_hex(8)
        RESTORES[token] = tmp
        self._json({"token": token, "files": files, "existing": existing})

    def _restore_apply(self, b):
        tmp = RESTORES.pop(b.get("token"), None)
        if not tmp:
            raise RuntimeError("That restore expired. Pick the zip again.")
        skip = set(b.get("skip", []))

        def fn(ftp):
            with zipfile.ZipFile(tmp) as zf:
                dirs, work = set(), []
                for info in zf.infolist():
                    rel = safe_member(info.filename)
                    if not rel:
                        continue
                    parts = rel.split("/")
                    depth = len(parts) if info.is_dir() else len(parts) - 1
                    for i in range(1, depth + 1):
                        dirs.add("/" + "/".join(parts[:i]))
                    if not info.is_dir() and "/" + rel not in skip:
                        work.append((info, "/" + rel))
                make_dirs(ftp, dirs)
                for info, dest in work:
                    with zf.open(info) as src:
                        ftp.storbinary("STOR " + dest, src, blocksize=65536)
                return len(work)

        try:
            written = SESSION.run(fn)
        finally:
            os.remove(tmp)
        self._json({"ok": True, "written": written})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if self._blocked(True):
            return self._refuse()
        try:
            if u.path == "/api/connect":
                b = self._body()
                try:
                    port = int(str(b.get("port") or 5000).strip())
                    assert 0 < port < 65536
                except (ValueError, AssertionError):
                    raise RuntimeError("The port should be a number, like 5000.")
                if not b.get("host", "").strip():
                    raise RuntimeError("Enter the 3DS's IP address.")
                welcome = SESSION.connect(
                    b.get("host", "").strip(),
                    port,
                    b.get("user", "").strip(),
                    b.get("password", ""),
                )
                self._json({"ok": True, "welcome": welcome})
            elif u.path == "/api/quit":
                # A newer copy is starting up and wants this port.
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif u.path == "/api/disconnect":
                SESSION.disconnect()
                self._json({"ok": True})
            elif u.path == "/api/upload":
                dest = q["path"][0]
                reader = LimitedReader(self.rfile, int(self.headers.get("Content-Length") or 0))

                def store(f):
                    try:
                        f.storbinary("STOR " + dest, reader, blocksize=65536)
                    finally:
                        if reader.left > 0:  # upload was cancelled partway; don't leave half a file
                            try:
                                f.delete(dest)
                            except ftplib.all_errors:
                                pass
                    if reader.left > 0:
                        raise RuntimeError("Upload cancelled")

                SESSION.run(store)
                self._json({"ok": True})
            elif u.path == "/api/zipmany":
                # Submitted as a regular form so the browser treats the reply as a download.
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode("utf-8"))
                items = json.loads(form.get("items", ["[]"])[0])
                name = form.get("name", ["3DS files.zip"])[0]
                spec = [(i["path"], bool(i.get("dir")), i["path"].strip("/")) for i in items]
                self._send_zip(spec, name)
            elif u.path == "/api/stat":
                b = self._body()
                self._json({"found": SESSION.run(lambda f: stat_paths(f, b["paths"]))})
            elif u.path == "/api/restore/inspect":
                self._restore_inspect()
            elif u.path == "/api/restore/apply":
                self._restore_apply(self._body())
            elif u.path == "/api/restore/discard":
                tmp = RESTORES.pop(self._body().get("token"), None)
                if tmp:
                    os.remove(tmp)
                self._json({"ok": True})
            elif u.path == "/api/exists":
                b = self._body()
                self._json({"existing": SESSION.run(lambda f: existing_files(f, b["paths"]))})
            elif u.path == "/api/mkdirs":
                b = self._body()
                SESSION.run(lambda f: make_dirs(f, b["paths"]))
                self._json({"ok": True})
            elif u.path == "/api/mkdir":
                b = self._body()
                SESSION.run(lambda f: f.mkd(b["path"]))
                self._json({"ok": True})
            elif u.path == "/api/rename":
                b = self._body()
                SESSION.run(lambda f: f.rename(b["from"], b["to"]))
                self._json({"ok": True})
            elif u.path == "/api/delete":
                b = self._body()
                SESSION.run(lambda f: delete_path(f, b["path"], bool(b.get("dir"))))
                self._json({"ok": True})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self._fail(e)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>3DS FTP</title>
<style>
  :root {
    color-scheme: light dark;
    --win: rgba(243, 243, 243, .88);
    --pane: rgba(255, 255, 255, .72);
    --line: rgba(0, 0, 0, .08);
    --text: #1b1b1b;
    --muted: #616161;
    --hover: rgba(0, 95, 184, .07);
    --sel: #cce8ff;
    --sel-line: #99d1ff;
    --accent: #005fb8;
    --field: rgba(255, 255, 255, .85);
    --stop: #c42b1c;
    --folder: #ffc83d;
    --folder-back: #e8a83a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --win: rgba(24, 24, 24, .84);
      --pane: rgba(25, 25, 25, .75);
      --line: rgba(255, 255, 255, .08);
      --text: #f3f3f3;
      --muted: #a8a8a8;
      --hover: rgba(255, 255, 255, .05);
      --sel: rgba(96, 205, 255, .18);
      --sel-line: rgba(96, 205, 255, .45);
      --accent: #60cdff;
      --field: rgba(255, 255, 255, .06);
      --stop: #ff99a4;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: #000;
    font: 13px/1.4 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    color: var(--text);
    overflow: hidden;
  }
  #rain { position: fixed; inset: 0; width: 100%; height: 100%; display: block; }
  button { font: inherit; color: inherit; cursor: default; }
  svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; flex: none; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

  /* ---------- window ---------- */
  .window {
    position: relative;
    z-index: 1;
    width: min(1040px, calc(100% - 96px));
    height: min(660px, calc(100% - 120px));
    margin: max(40px, calc((100vh - 660px) / 2)) auto 0;
    display: grid;
    grid-template-rows: 34px auto auto minmax(0, 1fr) auto 26px;
    grid-template-columns: minmax(0, 1fr);
    background: var(--win);
    backdrop-filter: blur(5px) saturate(1.2);
    -webkit-backdrop-filter: blur(5px) saturate(1.2);
    border: 1px solid rgba(255, 255, 255, .12);
    border-radius: 8px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, .6), 0 0 0 1px rgba(0, 0, 0, .4);
    overflow: hidden;
  }
  .window.max { width: 100%; height: 100%; margin: 0; border-radius: 0; }
  .window.min { display: none; }

  .titlebar { display: flex; align-items: center; user-select: none; }
  .titlebar .tab {
    display: flex; align-items: center; gap: 8px;
    height: 100%;
    padding: 0 14px;
    font-size: 12px;
  }
  .titlebar .tab .ico { transform: scale(.75); }
  .titlebar .spacer { flex: 1; }
  .titlebar .ctl {
    width: 46px; height: 100%;
    display: grid; place-items: center;
    border: 0; background: none;
  }
  .titlebar .ctl svg { width: 10px; height: 10px; stroke-width: 1; }
  .titlebar .ctl:hover { background: var(--hover); }
  .titlebar .ctl.close:hover { background: #c42b1c; color: #fff; }

  .commands {
    display: flex; align-items: center; gap: 2px; flex-wrap: wrap;
    padding: 6px 10px;
    border-bottom: 1px solid var(--line);
  }
  .commands .sep { width: 1px; height: 22px; margin: 0 4px; background: var(--line); }
  .cmd {
    display: flex; align-items: center; gap: 6px;
    height: 32px;
    padding: 0 8px;
    border: 0; border-radius: 4px;
    background: none;
    white-space: nowrap;
  }
  .cmd:hover:not(:disabled) { background: var(--hover); }
  .cmd:active:not(:disabled) { opacity: .75; }
  .cmd:disabled { opacity: .4; }
  .cmd.primary svg { color: var(--accent); }
  .cmd.danger svg { color: var(--stop); }

  .addressrow {
    display: flex; align-items: center; gap: 4px;
    padding: 6px 10px;
  }
  .nav { width: 32px; height: 30px; display: grid; place-items: center; border: 0; border-radius: 4px; background: none; }
  .nav:hover:not(:disabled) { background: var(--hover); }
  .nav:disabled { opacity: .35; }
  .address {
    flex: 1;
    display: flex; align-items: center; gap: 2px;
    height: 32px;
    min-width: 0;
    padding: 0 6px;
    background: var(--field);
    border: 1px solid var(--line);
    border-bottom-color: rgba(0, 0, 0, .25);
    border-radius: 4px;
    overflow: hidden;
    white-space: nowrap;
  }
  .address .ico { transform: scale(.7); margin-right: 2px; }
  .crumb { border: 0; background: none; padding: 3px 6px; border-radius: 3px; }
  .crumb:hover { background: var(--hover); }
  .chev { color: var(--muted); font-size: 11px; }
  .search {
    width: 240px;
    height: 32px;
    display: flex; align-items: center; gap: 6px;
    padding: 0 10px;
    background: var(--field);
    border: 1px solid var(--line);
    border-bottom-color: rgba(0, 0, 0, .25);
    border-radius: 4px;
    color: var(--muted);
  }
  .search input { flex: 1; min-width: 0; border: 0; background: none; font: inherit; color: var(--text); outline: none; }

  /* ---------- body ---------- */
  .body { display: grid; grid-template-columns: 210px 1fr; min-height: 0; }
  .sidebar {
    overflow-y: auto;
    padding: 6px 6px 12px;
    border-right: 1px solid var(--line);
  }
  .side {
    width: 100%;
    display: flex; align-items: center; gap: 8px;
    padding: 5px 10px;
    border: 0; border-radius: 4px;
    background: none;
    text-align: left;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .side.child { padding-left: 30px; }
  .side:hover { background: var(--hover); }
  .side.on { background: var(--hover); position: relative; }
  .side.on::before {
    content: ""; position: absolute; left: 0; top: 7px; bottom: 7px; width: 3px;
    background: var(--accent); border-radius: 2px;
  }
  .side .ico { transform: scale(.8); }

  .content { position: relative; display: flex; flex-direction: column; min-height: 0; min-width: 0; background: var(--pane); container-type: inline-size; }
  .content.drag::after {
    content: "Drop to upload to this folder";
    position: absolute; inset: 8px;
    display: grid; place-items: center;
    border: 2px dashed var(--accent); border-radius: 6px;
    background: var(--sel);
    font-size: 15px; font-weight: 600;
  }
  .cols, .row {
    display: grid;
    grid-template-columns: minmax(140px, 1fr) 150px 110px 84px;
    align-items: center;
  }
  .cols {
    padding: 0 12px;
    border-bottom: 1px solid var(--line);
  }
  .cols button {
    height: 30px;
    padding: 0 8px;
    border: 0; border-right: 1px solid var(--line);
    background: none;
    text-align: left;
    color: var(--muted);
  }
  .cols button:hover { background: var(--hover); color: var(--text); }
  .cols button.size { text-align: right; }
  .cols button[data-dir]::after { margin-left: 6px; font-size: 9px; }
  .cols button[data-dir="1"]::after { content: "▲"; }
  .cols button[data-dir="-1"]::after { content: "▼"; }

  .list { list-style: none; margin: 0; padding: 4px 12px; overflow-y: auto; flex: 1; }
  .row {
    height: 30px;
    border: 1px solid transparent;
    border-radius: 3px;
    user-select: none;
  }
  .row > span { padding: 0 8px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .row .nm { display: flex; align-items: center; gap: 8px; }
  .row .nm b { font-weight: 400; overflow: hidden; text-overflow: ellipsis; }
  .row .dt, .row .ty, .row .sz { color: var(--muted); }
  .row .sz { text-align: right; }
  .row:hover { background: var(--hover); }
  .row[aria-selected="true"] { background: var(--sel); border-color: var(--sel-line); }
  #empty { margin: 40px auto; color: var(--muted); text-align: center; }

  .statusbar {
    display: flex; align-items: center; gap: 16px;
    padding: 0 14px;
    font-size: 12px;
    color: var(--muted);
    border-top: 1px solid var(--line);
    white-space: nowrap; overflow: hidden;
  }
  #status { margin-left: auto; overflow: hidden; text-overflow: ellipsis; }
  #status[data-kind="ok"] { color: #0f7b0f; }
  #status[data-kind="err"] { color: var(--stop); }
  @media (prefers-color-scheme: dark) { #status[data-kind="ok"] { color: #6ccb5f; } }

  /* icons */
  .ico { display: inline-block; position: relative; width: 20px; height: 16px; flex: none; }
  .ico.folder { background: var(--folder); border-radius: 1px 2px 2px 2px; }
  .ico.folder::before {
    content: ""; position: absolute; left: 0; top: -3px; width: 8px; height: 4px;
    background: var(--folder-back); border-radius: 2px 2px 0 0;
  }
  .ico.file {
    width: 15px; height: 19px; margin: 0 2px 0 3px;
    background: #fff; border: 1px solid #8a8a8a; border-radius: 1px 5px 1px 1px;
  }
  .ico.drive { width: 20px; height: 13px; background: linear-gradient(#d9d9d9, #a9a9a9); border-radius: 2px; }
  .ico.drive::after { content: ""; position: absolute; right: 3px; bottom: 3px; width: 3px; height: 3px; border-radius: 50%; background: #38c172; }

  /* connect */
  .connect {
    margin: auto;
    width: min(420px, 100% - 32px);
    padding: 28px;
    background: var(--win);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, .12);
  }
  .connect h1 { margin: 0 0 4px; font-size: 20px; font-weight: 600; }
  .connect p { margin: 0 0 18px; color: var(--muted); }
  #connect-form { display: grid; grid-template-columns: 1fr 110px; gap: 12px; }
  #connect-form .wide { grid-column: 1 / -1; }
  label { display: block; color: var(--muted); }
  label input {
    display: block; width: 100%; margin-top: 4px;
    height: 32px; padding: 0 10px;
    font: inherit; color: var(--text);
    background: var(--field);
    border: 1px solid var(--line); border-bottom: 2px solid rgba(0, 0, 0, .3);
    border-radius: 4px;
    outline: none;
  }
  label input:focus { border-bottom-color: var(--accent); }
  .btn {
    height: 32px; padding: 0 20px;
    border: 1px solid transparent; border-radius: 4px;
    background: var(--accent); color: #fff;
    font-weight: 600;
  }
  @media (prefers-color-scheme: dark) { .btn { color: #000; } }
  .btn:hover { filter: brightness(1.08); }
  .connected .connect, body:not(.connected) .browser { display: none; }
  body:not(.connected) .body { display: flex; }

  /* context menu */
  #menu {
    position: fixed; z-index: 5;
    min-width: 180px;
    padding: 4px;
    background: var(--win);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
  }
  #menu button {
    width: 100%; display: flex; align-items: center; gap: 10px;
    padding: 7px 12px; border: 0; border-radius: 4px; background: none; text-align: left;
  }
  #menu button:hover { background: var(--hover); }
  #menu hr { border: 0; border-top: 1px solid var(--line); margin: 4px 0; }

  #restore {
    position: fixed; z-index: 2; left: 50%; bottom: 20px; transform: translateX(-50%);
    display: none; align-items: center; gap: 8px;
    padding: 8px 16px;
    border: 1px solid rgba(255, 255, 255, .15); border-radius: 6px;
    background: rgba(32, 32, 32, .85); color: #fff;
    backdrop-filter: blur(12px);
  }
  body.minimized #restore { display: flex; }

  @media (max-width: 760px) {
    .window { width: 100%; height: 100%; margin: 0; border-radius: 0; }
    .sidebar, .search, .cmd span, .titlebar .ctl.maxbtn { display: none; }
    .body { grid-template-columns: 1fr; }
    .cols, .row { grid-template-columns: 1fr 80px; }
    .cols .date, .cols .type, .row .dt, .row .ty { display: none; }
  }

  /* ---------- preview pane ---------- */
  .body.with-preview { grid-template-columns: 210px 1fr 300px; }
  #preview { display: none; }
  .with-preview #preview {
    display: flex; flex-direction: column; gap: 10px;
    min-height: 0; overflow-y: auto;
    padding: 16px;
    border-left: 1px solid var(--line);
  }
  #preview .pv-empty { margin: auto; color: var(--muted); text-align: center; }
  #preview .pv-big { align-self: center; margin: 28px 0 18px; transform: scale(3); }
  #preview h3 { margin: 0; font-size: 15px; font-weight: 600; word-break: break-all; }
  #preview dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; color: var(--muted); }
  #preview dd { margin: 0; color: var(--text); }
  #preview .pv-img {
    display: grid; place-items: center;
    min-height: 120px;
    border-radius: 4px;
    background: repeating-conic-gradient(rgba(128, 128, 128, .15) 0 25%, transparent 0 50%) 0 0 / 16px 16px;
  }
  #preview .pv-img img { max-width: 100%; max-height: 320px; display: block; }
  #preview textarea {
    flex: 1; min-height: 220px; width: 100%;
    padding: 8px;
    font: 12px/1.5 Consolas, "Cascadia Mono", ui-monospace, monospace;
    color: var(--text);
    background: var(--field);
    border: 1px solid var(--line); border-radius: 4px;
    resize: none;
    white-space: pre; overflow: auto;
  }
  #preview textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  #preview .pv-row { display: flex; gap: 8px; flex-wrap: wrap; }
  #preview .pv-note { color: var(--muted); margin: 0; }
  .btn2 {
    height: 32px; padding: 0 16px;
    border: 1px solid var(--line); border-radius: 4px;
    background: var(--field);
  }
  .btn2:hover:not(:disabled) { background: var(--hover); }
  .btn:disabled, .btn2:disabled { opacity: .45; }
  .cmd[aria-pressed="true"] { background: var(--hover); }

  /* ---------- status bar extras ---------- */
  #cancel {
    display: none;
    height: 20px; padding: 0 10px;
    border: 1px solid var(--line); border-radius: 3px;
    background: var(--field); font-size: 12px;
  }
  body.busy #cancel { display: inline-block; }

  /* ---------- connect extras ---------- */
  .connect .actions { grid-column: 1 / -1; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .link { border: 0; background: none; padding: 0; color: var(--accent); cursor: pointer; }
  .link:hover { text-decoration: underline; }
  .link:disabled { color: var(--muted); text-decoration: none; cursor: default; }
  #found { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 6px; }
  #found:empty { display: none; }
  #found button {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 8px 12px;
    border: 1px solid var(--line); border-radius: 4px;
    background: var(--field); text-align: left; cursor: pointer;
  }
  #found button:hover { border-color: var(--accent); }
  #found small { color: var(--muted); }

  /* ---------- replace-or-skip dialog ---------- */
  dialog {
    width: min(480px, calc(100% - 32px));
    padding: 24px;
    color: var(--text);
    background: var(--win);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, .5);
  }
  @media (prefers-color-scheme: dark) { dialog { background: rgba(38, 38, 38, .96); } }
  dialog::backdrop { background: rgba(0, 0, 0, .45); }
  dialog h2 { margin: 0 0 6px; font-size: 18px; font-weight: 600; }
  dialog p { margin: 0 0 16px; word-break: break-word; }
  .cf-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
  .cf-compare div { padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--field); }
  .cf-compare b { display: block; font-weight: 600; margin-bottom: 2px; }
  .cf-compare span { color: var(--muted); white-space: pre-line; }
  .cf-all { display: flex; align-items: center; gap: 8px; margin-bottom: 18px; color: var(--text); }
  .cf-all[hidden] { display: none; }
  .cf-all input { width: 16px; height: 16px; margin: 0; display: inline-block; accent-color: var(--accent); }
  .cf-buttons { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }

  @media (max-width: 760px) {
    .body.with-preview { grid-template-columns: 1fr; }
    .with-preview #preview { display: none; }
    [data-act="preview"] { display: none; }
  }
  @container (max-width: 600px) {
    .cols, .row { grid-template-columns: minmax(120px, 1fr) 150px 84px; }
    .cols .type, .row .ty { display: none; }
  }
  @container (max-width: 440px) {
    .cols, .row { grid-template-columns: minmax(100px, 1fr) 84px; }
    .cols .date, .row .dt { display: none; }
  }

  /* fixed rows so hidden parts collapse cleanly */
  .titlebar { grid-row: 1; } .commands { grid-row: 2; } .addressrow { grid-row: 3; }
  .body { grid-row: 4; } #player { grid-row: 5; } .statusbar { grid-row: 6; }

  /* sidebar tools */
  .side-head { margin: 16px 10px 4px; font-size: 12px; color: var(--muted); }
  .side .tool-ico { display: grid; place-items: center; width: 20px; color: var(--accent); }

  /* gallery */
  #gallery { display: none; flex: 1; min-height: 0; overflow-y: auto; padding: 0 16px 16px; }
  .gallery-mode #gallery { display: block; }
  .gallery-mode .cols, .gallery-mode #list, .gallery-mode #empty { display: none !important; }
  .gallery-mode .body.with-preview { grid-template-columns: 210px 1fr; }
  .gallery-mode #preview { display: none !important; }
  .g-bar {
    position: sticky; top: 0; z-index: 1;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    margin: 0 -16px 12px; padding: 10px 16px;
    background: var(--win);
    border-bottom: 1px solid var(--line);
  }
  .g-count { margin-right: auto; color: var(--muted); }
  .g-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }
  .g-card { position: relative; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: var(--field); cursor: pointer; }
  .g-card:hover { border-color: var(--sel-line); }
  .g-card.on { outline: 2px solid var(--accent); outline-offset: -1px; }
  .g-card img {
    display: block; width: 100%; aspect-ratio: 5 / 3; object-fit: contain;
    background: repeating-conic-gradient(rgba(128, 128, 128, .15) 0 25%, transparent 0 50%) 0 0 / 16px 16px;
  }
  .g-card input { position: absolute; top: 8px; left: 8px; width: 18px; height: 18px; margin: 0; accent-color: var(--accent); opacity: 0; }
  .g-card:hover input, .g-card input:checked, .g-card input:focus-visible { opacity: 1; }
  .g-cap { display: flex; flex-direction: column; padding: 6px 8px; min-width: 0; }
  .g-cap b { font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .g-cap small { color: var(--muted); }

  /* lightbox */
  #lightbox { width: min(1040px, calc(100% - 32px)); padding: 0; overflow: hidden; }
  .lb-top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 12px 16px; border-bottom: 1px solid var(--line); }
  .lb-top b { font-weight: 600; }
  .lb-top span { color: var(--muted); }
  .lb-actions { margin-left: auto; display: flex; gap: 8px; }
  .lb-stage { position: relative; display: grid; place-items: center; min-height: 50vh; background: #000; }
  .lb-stage img { width: 100%; height: 72vh; object-fit: contain; display: block; }
  .lb-stage img.small { image-rendering: pixelated; }
  .lb-nav {
    position: absolute; top: 50%; transform: translateY(-50%);
    width: 44px; height: 44px; border: 0; border-radius: 50%;
    background: rgba(255, 255, 255, .15); color: #fff; font-size: 26px; line-height: 1;
    cursor: pointer;
  }
  .lb-nav:hover:not(:disabled) { background: rgba(255, 255, 255, .3); }
  .lb-nav:disabled { opacity: .2; cursor: default; }
  #lb-prev { left: 12px; } #lb-next { right: 12px; }

  /* music player */
  #player { align-items: center; gap: 6px; padding: 6px 12px; border-top: 1px solid var(--line); }
  #player:not([hidden]) { display: flex; }
  .pl-btn { width: 32px; height: 32px; flex: none; display: grid; place-items: center; border: 0; border-radius: 50%; background: none; cursor: pointer; }
  .pl-btn:hover { background: var(--hover); }
  .pl-btn.big { background: var(--accent); color: #fff; }
  @media (prefers-color-scheme: dark) { .pl-btn.big { color: #000; } }
  .pl-info { width: 220px; min-width: 0; display: flex; flex-direction: column; margin: 0 8px; }
  .pl-info b, .pl-info small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pl-info b { font-weight: 600; }
  .pl-info small { color: var(--muted); }
  .pl-time { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  #pl-seek { flex: 1; min-width: 60px; accent-color: var(--accent); }
  @media (max-width: 760px) {
    .pl-info { width: auto; flex: 1; }
    .pl-time, #pl-seek { display: none; }
  }
</style>
</head>
<body>
<canvas id="rain" aria-hidden="true"></canvas>

<div class="window" id="win">
  <div class="titlebar">
    <div class="tab"><span class="ico drive"></span>3DS FTP</div>
    <div class="spacer"></div>
    <button class="ctl" type="button" id="minbtn" aria-label="Minimize"><svg viewBox="0 0 10 10"><path d="M0 5h10"/></svg></button>
    <button class="ctl maxbtn" type="button" id="maxbtn" aria-label="Maximize"><svg viewBox="0 0 10 10"><rect x=".5" y=".5" width="9" height="9"/></svg></button>
    <button class="ctl close" type="button" id="closebtn" aria-label="Disconnect and close"><svg viewBox="0 0 10 10"><path d="M0 0l10 10M10 0L0 10"/></svg></button>
  </div>

  <div class="commands browser">
    <button class="cmd primary" type="button" data-act="mkdir"><svg viewBox="0 0 16 16"><path d="M1.5 4.5h4l1.5 1.5h7.5v7.5h-13z"/><path d="M10 8v4M8 10h4"/></svg><span>New folder</span></button>
    <button class="cmd primary" type="button" data-act="upload"><svg viewBox="0 0 16 16"><path d="M8 11V2.5M4.5 6L8 2.5 11.5 6M2.5 13.5h11"/></svg><span>Upload files</span></button>
    <button class="cmd primary" type="button" data-act="uploadFolder"><svg viewBox="0 0 16 16"><path d="M1.5 4.5h4l1.5 1.5h7.5v7.5h-13z"/><path d="M8 12V8.5M6.5 10L8 8.5 9.5 10"/></svg><span>Upload folder</span></button>
    <div class="sep"></div>
    <button class="cmd" type="button" data-act="download" data-needs="any"><svg viewBox="0 0 16 16"><path d="M8 2.5V11M4.5 7.5L8 11l3.5-3.5M2.5 13.5h11"/></svg><span>Download</span></button>
    <button class="cmd" type="button" data-act="rename" data-needs="any"><svg viewBox="0 0 16 16"><path d="M2.5 13.5h3l7.5-7.5-3-3-7.5 7.5z"/></svg><span>Rename</span></button>
    <button class="cmd danger" type="button" data-act="delete" data-needs="any"><svg viewBox="0 0 16 16"><path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4"/></svg><span>Delete</span></button>
    <div class="sep"></div>
    <button class="cmd" type="button" data-act="sizes"><svg viewBox="0 0 16 16"><path d="M2.5 13.5v-4M6.2 13.5v-7M9.8 13.5V4M13.5 13.5v-9"/></svg><span>Folder sizes</span></button>
    <button class="cmd" type="button" data-act="preview" aria-pressed="false"><svg viewBox="0 0 16 16"><rect x="1.5" y="2.5" width="13" height="11" rx="1"/><path d="M9.5 2.5v11"/></svg><span>Preview</span></button>
    <div class="sep"></div>
    <button class="cmd" type="button" data-act="disconnect"><svg viewBox="0 0 16 16"><path d="M6.5 2.5h-4v11h4M10 5l3 3-3 3M13 8H6"/></svg><span>Disconnect</span></button>
  </div>

  <div class="addressrow browser">
    <button class="nav" type="button" id="backbtn" data-act="back" aria-label="Back"><svg viewBox="0 0 16 16"><path d="M13 8H3M7 4L3 8l4 4"/></svg></button>
    <button class="nav" type="button" id="fwdbtn" data-act="forward" aria-label="Forward"><svg viewBox="0 0 16 16"><path d="M3 8h10M9 4l4 4-4 4"/></svg></button>
    <button class="nav" type="button" id="upbtn" data-act="parent" aria-label="Up one folder"><svg viewBox="0 0 16 16"><path d="M8 13V3M4 7l4-4 4 4"/></svg></button>
    <button class="nav" type="button" data-act="refresh" aria-label="Refresh"><svg viewBox="0 0 16 16"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"/></svg></button>
    <nav class="address" id="crumbs" aria-label="Current folder"></nav>
    <label class="search"><svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5l3 3"/></svg><input id="filter" placeholder="Search this folder" aria-label="Search this folder"></label>
  </div>

  <div class="body" id="body">
    <aside class="sidebar browser" id="sidebar" aria-label="Folders"></aside>

    <main class="content browser" id="drop">
      <div class="cols" id="cols">
        <button type="button" data-key="name">Name</button>
        <button type="button" class="date" data-key="modified">Date modified</button>
        <button type="button" class="type" data-key="type">Type</button>
        <button type="button" class="size" data-key="size">Size</button>
      </div>
      <ul class="list" id="list" role="listbox" aria-label="Files"></ul>
      <p id="empty" hidden>This folder is empty.</p>
      <div id="gallery"></div>
      <input id="restore-input" type="file" accept=".zip,application/zip" hidden>
      <input id="file-input" type="file" multiple hidden>
      <input id="folder-input" type="file" webkitdirectory hidden>
    </main>

    <aside id="preview" class="browser" aria-label="Preview"></aside>

    <section class="connect">
      <h1>Connect to your 3DS</h1>
      <p>Open ftpd on the 3DS, then find it on your network or type the address it shows on screen.</p>
      <form id="connect-form">
        <label>IP address <input name="host" placeholder="192.168.1.50" required autocomplete="off"></label>
        <label>Port <input name="port" value="5000" inputmode="numeric"></label>
        <label>User <input name="user" placeholder="Optional" autocomplete="off"></label>
        <label>Password <input name="password" type="password" placeholder="Optional"></label>
        <div id="found"></div>
        <div class="actions">
          <button class="btn" type="submit">Connect</button>
          <button class="link" type="button" id="findbtn">Find my 3DS</button>
        </div>
      </form>
    </section>
  </div>

  <div id="player" hidden>
    <button type="button" class="pl-btn" id="pl-prev" aria-label="Previous song"><svg viewBox="0 0 16 16"><path d="M4 3v10M13 3L6 8l7 5z"/></svg></button>
    <button type="button" class="pl-btn big" id="pl-play" aria-label="Pause"><svg viewBox="0 0 16 16" id="pl-icon"><path d="M5.5 3.5v9M10.5 3.5v9"/></svg></button>
    <button type="button" class="pl-btn" id="pl-next" aria-label="Next song"><svg viewBox="0 0 16 16"><path d="M12 3v10M3 3l7 5-7 5z"/></svg></button>
    <div class="pl-info"><b id="pl-title"></b><small id="pl-where"></small></div>
    <span class="pl-time" id="pl-cur">0:00</span>
    <input type="range" id="pl-seek" min="0" max="1000" value="0" aria-label="Seek">
    <span class="pl-time" id="pl-dur">0:00</span>
    <button type="button" class="pl-btn" id="pl-stop" aria-label="Stop and close player"><svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>
    <audio id="audio" preload="auto"></audio>
  </div>

  <div class="statusbar">
    <span id="count"></span>
    <span id="selinfo"></span>
    <span id="status" role="status"></span>
    <button id="cancel" type="button">Cancel</button>
  </div>
</div>

<button id="restore" type="button"><span class="ico drive"></span>3DS FTP</button>
<div id="menu" hidden></div>

<dialog id="conflict">
  <h2>Replace or skip file</h2>
  <p id="cf-msg"></p>
  <div class="cf-compare">
    <div><b id="cf-local-label">From this computer</b><span id="cf-local"></span></div>
    <div><b>Already on the 3DS</b><span id="cf-remote"></span></div>
  </div>
  <label class="cf-all" id="cf-all-row"><input type="checkbox" id="cf-all"><span id="cf-all-text"></span></label>
  <div class="cf-buttons">
    <button class="btn" type="button" data-choice="replace">Replace</button>
    <button class="btn2" type="button" data-choice="skip">Skip</button>
    <button class="btn2" type="button" data-choice="cancel">Cancel upload</button>
  </div>
</dialog>

<dialog id="lightbox" aria-label="Image viewer">
  <div class="lb-top">
    <b id="lb-name"></b><span id="lb-meta"></span>
    <div class="lb-actions">
      <button class="btn2" type="button" id="lb-dl">Download</button>
      <button class="btn2" type="button" id="lb-del">Delete</button>
      <button class="btn2" type="button" id="lb-close">Close</button>
    </div>
  </div>
  <div class="lb-stage">
    <button class="lb-nav" type="button" id="lb-prev" aria-label="Previous image">‹</button>
    <img id="lb-img" alt="">
    <button class="lb-nav" type="button" id="lb-next" aria-label="Next image">›</button>
  </div>
</dialog>

<iframe name="dlframe" id="dlframe" hidden></iframe>

<script>
/* ---------------- matrix rain ---------------- */
(() => {
  const cv = document.getElementById("rain");
  const cx = cv.getContext("2d");
  const glyphs = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789";
  const fs = 16;
  const slow = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let w, h, drops, last = 0;
  const pick = () => glyphs[(Math.random() * glyphs.length) | 0];

  function size() {
    const dpr = window.devicePixelRatio || 1;
    w = innerWidth; h = innerHeight;
    cv.width = w * dpr; cv.height = h * dpr;
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.fillStyle = "#000"; cx.fillRect(0, 0, w, h);
    drops = Array.from({ length: Math.ceil(w / fs) }, () => Math.random() * -h / fs);
  }
  function frame(t) {
    requestAnimationFrame(frame);
    if (t - last < (slow ? 400 : 55)) return;
    last = t;
    cx.fillStyle = "rgba(0, 0, 0, 0.07)";
    cx.fillRect(0, 0, w, h);
    cx.font = fs + "px monospace";
    cx.textAlign = "center";
    for (let i = 0; i < drops.length; i++) {
      const x = i * fs + fs / 2, y = drops[i] * fs;
      if (y > 0) {
        cx.fillStyle = "#17c942";
        cx.fillText(pick(), x, y - fs);
        cx.fillStyle = "#d4ffdf";
        cx.fillText(pick(), x, y);
      }
      if (y > h && Math.random() > 0.975) drops[i] = 0;
      drops[i]++;
    }
  }
  addEventListener("resize", size);
  size();
  requestAnimationFrame(frame);
})();

/* ---------------- state and helpers ---------------- */
const $ = s => document.querySelector(s);
let cwd = "/", entries = [], view = [], sel = -1, connected = false;
let sortKey = "name", sortDir = 1, hist = [], fwd = [], topFolders = [];
let job = null;                 // the running upload or size calculation
const sizes = new Map();        // folder path -> {size, files, folders}
let previewOn = true, pv = { dirty: false, token: 0 };

const IMAGE_EXT = ["png", "jpg", "jpeg", "gif", "bmp", "webp"];
const TEXT_EXT = ["txt", "ini", "cfg", "conf", "json", "xml", "log", "md", "yml", "yaml", "toml", "csv", "lua", "py", "sh", "js", "html", "htm", "nfo"];
const AUDIO_EXT = ["mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac"];
const MAX_TEXT = 512 * 1024, MAX_IMAGE = 15 * 1024 * 1024;
const GALLERY_ROOTS = ["/luma/screenshots", "/DCIM"];
const SAVE_DIRS = ["/3ds/Checkpoint/saves", "/3ds/Checkpoint/extdata", "/JKSV"];
let mode = "files", gallery = [], gsel = new Set(), lbIndex = -1;
let queue = [], qi = -1, seeking = false;
const JUNK = ["thumbs.db", "desktop.ini", ".ds_store", "__macosx"];

const join = (dir, name) => (dir.endsWith("/") ? dir : dir + "/") + name;
const parent = p => p.replace(/\/[^/]+\/?$/, "") || "/";
const extOf = n => (/\.([^.]+)$/.exec(n) || [, ""])[1].toLowerCase();
const isJunk = rel => rel.split("/").some(seg => JUNK.includes(seg.toLowerCase()) || seg.startsWith("._"));
const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function api(url, opts = {}) {
  let r;
  try {
    r = await fetch(url, opts);
  } catch {
    throw new Error("Lost contact with the launcher. Is its window still open?");
  }
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : {};
  if (!r.ok || data.error) {
    const msg = data.error || r.statusText;
    if (connected && msg.startsWith("Not connected")) {
      // The launcher restarted and forgot the 3DS. Go back to the connect screen.
      setTimeout(() => { setConnected(false); status("The launcher was restarted. Connect again.", "err"); }, 0);
    }
    throw new Error(msg);
  }
  return data;
}

// Names the SD card (FAT32) will accept.
function badName(n, exceptName) {
  if (!n || !n.trim()) return "The name can't be empty.";
  if (n === "." || n === "..") return "That name isn't allowed.";
  if (/[\\/:*?"<>|]/.test(n)) return 'Names can\'t contain any of these: \\ / : * ? " < > |';
  if (/[. ]$/.test(n)) return "Names can't end with a space or a dot.";
  const clash = entries.find(x => x.name.toLowerCase() === n.toLowerCase() && x.name !== exceptName);
  if (clash) return `There's already something named "${clash.name}" here.`;
  return "";
}
function askName(title, start, exceptName) {
  let n = start;
  for (;;) {
    n = prompt(title, n);
    if (n == null) return null;
    n = n.trim();
    const problem = badName(n, exceptName);
    if (!problem) return n;
    alert(problem);
  }
}
const affectsPlayer = path => {
  const t = queue[qi];
  return t && (t.path === path || t.path.startsWith(path + "/"));
};
const post = (url, body) => api(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function status(msg, kind = "") {
  $("#status").textContent = msg;
  $("#status").dataset.kind = kind;
}
function fmtSize(n) {
  if (n < 1024) return n + " bytes";
  const u = ["KB", "MB", "GB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return (n < 10 ? n.toFixed(1) : Math.round(n).toLocaleString()) + " " + u[i];
}
function fmtDate(m) {
  const x = /^(\d{4})(\d\d)(\d\d)(\d\d)(\d\d)/.exec(m || "");
  if (!x) return m || "";
  const d = new Date(Date.UTC(+x[1], x[2] - 1, +x[3], +x[4], +x[5]));
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function typeOf(e) {
  if (e.dir) return "File folder";
  const x = extOf(e.name);
  return x ? x.toUpperCase() + " File" : "File";
}
const icon = e => el("span", "ico " + (e.dir ? "folder" : "file"));
const dlUrl = (path, inline, v) => "/api/download?path=" + encodeURIComponent(path) + (inline ? "&inline=1" : "") + (v ? "&v=" + encodeURIComponent(v) : "");
const isAudio = e => e && !e.dir && AUDIO_EXT.includes(extOf(e.name));
const svg = d => `<svg viewBox="0 0 16 16"><path d="${d}"/></svg>`;
function fmtStamp(t) {
  const x = /^(\d{4})(\d\d)(\d\d)(\d\d)(\d\d)/.exec(t || "");
  return x ? new Date(+x[1], x[2] - 1, +x[3], +x[4], +x[5]).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "";
}
function fmtTime(sec) {
  if (!isFinite(sec)) return "0:00";
  sec = Math.floor(sec);
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}
// Downloads go through a hidden frame so an error can't navigate the page away.
function startDownload(url) {
  const a = el("a");
  a.href = url;
  a.target = "dlframe";
  document.body.append(a);
  a.click();
  a.remove();
}
function zipMany(items, name) {
  const f = el("form");
  f.method = "post";
  f.action = "/api/zipmany";
  f.target = "dlframe";
  f.hidden = true;
  for (const [k, v] of [["items", JSON.stringify(items)], ["name", name]]) {
    const i = el("input");
    i.type = "hidden"; i.name = k; i.value = v;
    f.append(i);
  }
  document.body.append(f);
  f.submit();
  f.remove();
}
$("#dlframe").addEventListener("load", () => {
  try {
    const t = $("#dlframe").contentDocument.body.textContent;
    const err = JSON.parse(t).error;
    if (err) status(err, "err");
  } catch {}
});

function setConnected(on) {
  connected = on;
  document.body.classList.toggle("connected", on);
}

function startJob() {
  job = { cancelled: false, xhr: null };
  document.body.classList.add("busy");
  return job;
}
function endJob() {
  job = null;
  document.body.classList.remove("busy");
}
$("#cancel").onclick = () => {
  if (!job) return;
  job.cancelled = true;
  if (job.xhr) job.xhr.abort();
  status("Cancelling…");
};

/* unsaved text edits */
function canLeave() {
  if (!pv.dirty) return true;
  if (confirm("You have unsaved changes in the preview pane. Discard them?")) {
    pv.dirty = false;
    return true;
  }
  return false;
}
addEventListener("beforeunload", e => { if (pv.dirty) e.preventDefault(); });

/* ---------------- rendering ---------------- */
function renderCrumbs() {
  const nav = $("#crumbs");
  nav.replaceChildren(el("span", "ico drive"));
  const add = (label, path) => {
    const b = el("button", "crumb", label);
    b.type = "button";
    b.onclick = () => load(path);
    nav.append(b);
  };
  add("SD card", "/");
  let acc = "";
  for (const p of cwd.split("/").filter(Boolean)) {
    acc += "/" + p;
    nav.append(el("span", "chev", "›"));
    add(p, acc);
  }
  nav.scrollLeft = nav.scrollWidth;
}

function renderSidebar() {
  const sb = $("#sidebar");
  sb.replaceChildren();
  const top = mode === "gallery" ? null : "/" + (cwd.split("/").filter(Boolean)[0] || "");
  const item = (label, path, cls, ico) => {
    const b = el("button", "side " + cls + (path === top ? " on" : ""));
    b.type = "button";
    b.append(el("span", "ico " + ico), el("span", "", label));
    b.title = label;
    b.onclick = () => load(path);
    sb.append(b);
  };
  item("SD card", "/", "", "drive");
  for (const f of topFolders) item(f, "/" + f, "child", "folder");

  sb.append(el("div", "side-head", "Tools"));
  const tool = (label, path, fn, on) => {
    const b = el("button", "side" + (on ? " on" : ""));
    b.type = "button";
    const ic = el("span", "tool-ico");
    ic.innerHTML = svg(path);
    b.append(ic, el("span", "", label));
    b.title = label;
    b.onclick = fn;
    sb.append(b);
  };
  tool("Screenshots and photos", "M2 3.5h12v9H2zM2 10.5l3.5-3 3 2.5 2-1.5 3.5 3M10.5 6.5h.01", openGallery, mode === "gallery");
  tool("Back up saves", "M8 2.5V10M5 7l3 3 3-3M2.5 12v1.5h11V12", backupSaves);
  tool("Restore saves", "M8 10V2.5M5 5.5l3-3 3 3M2.5 12v1.5h11V12", () => $("#restore-input").click());
}

const sizeOf = e => e.dir ? (sizes.get(join(cwd, e.name))?.size ?? -1) : e.size;
const sizeText = e => {
  if (!e.dir) return fmtSize(e.size);
  const s = sizes.get(join(cwd, e.name));
  return s ? fmtSize(s.size) : "";
};

function sortedView() {
  const q = $("#filter").value.trim().toLowerCase();
  const val = e => sortKey === "type" ? typeOf(e) : sortKey === "size" ? sizeOf(e) : e[sortKey];
  return entries
    .filter(e => !q || e.name.toLowerCase().includes(q))
    .sort((a, b) => {
      if (a.dir !== b.dir) return a.dir ? -1 : 1;
      const x = val(a), y = val(b);
      const c = typeof x === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" });
      return c * sortDir;
    });
}

function renderList(focusName) {
  view = sortedView();
  const ul = $("#list");
  ul.replaceChildren();
  $("#empty").hidden = view.length > 0;
  $("#empty").textContent = entries.length ? "No items match your search." : "This folder is empty. Drag files or folders here to upload them.";
  document.querySelectorAll("#cols button").forEach(b => {
    if (b.dataset.key === sortKey) b.dataset.dir = sortDir; else delete b.dataset.dir;
  });

  view.forEach((e, i) => {
    const li = el("li", "row");
    li.setAttribute("role", "option");
    li.dataset.name = e.name;
    const nm = el("span", "nm");
    nm.append(icon(e), el("b", "", e.name));
    li.append(nm, el("span", "dt", fmtDate(e.modified)), el("span", "ty", typeOf(e)), el("span", "sz", sizeText(e)));
    li.title = e.name;
    li.onclick = () => select(i);
    li.ondblclick = () => { if (sel === i) openSel(); };
    li.oncontextmenu = ev => { ev.preventDefault(); select(i); if (sel === i) showMenu(ev.clientX, ev.clientY); };
    ul.append(li);
  });

  const idx = focusName ? view.findIndex(e => e.name === focusName) : -1;
  select(idx, true);
  $("#count").textContent = plural(view.length, "item");
}

function refreshSizeCell(name) {
  const li = [...$("#list").children].find(r => r.dataset.name === name);
  const e = entries.find(x => x.name === name);
  if (li && e) li.querySelector(".sz").textContent = sizeText(e);
}

function select(i, force = false) {
  const next = i >= 0 && view.length ? Math.max(0, Math.min(view.length - 1, i)) : -1;
  if (!force && next === sel) return;
  if (!force && !canLeave()) return;
  sel = next;
  [...$("#list").children].forEach((li, j) => li.setAttribute("aria-selected", j === sel));
  if (sel >= 0) $("#list").children[sel].scrollIntoView({ block: "nearest" });
  updateSelInfo();
  document.querySelectorAll("[data-needs]").forEach(b => { b.disabled = !view[sel]; });
  renderPreview();
}

function updateSelInfo() {
  const e = view[sel];
  let t = "";
  if (e) {
    t = "1 item selected";
    const s = e.dir ? sizes.get(join(cwd, e.name))?.size : e.size;
    if (s != null) t += "  " + fmtSize(s);
  }
  $("#selinfo").textContent = t;
}

function updateNav() {
  $("#backbtn").disabled = !hist.length;
  $("#fwdbtn").disabled = !fwd.length;
  $("#upbtn").disabled = cwd === "/";
}

let loadToken = 0;
async function load(path, focusName, how = "push") {
  if (!canLeave()) return;
  const token = ++loadToken;
  status("Loading…");
  try {
    const data = await api("/api/list?path=" + encodeURIComponent(path));
    if (token !== loadToken) return;  // a newer click already took over
    if (path !== cwd) {
      if (how === "push" && cwd) { hist.push(cwd); fwd = []; }
      $("#filter").value = "";
    }
    cwd = path;
    entries = data.entries;
    setMode("files");
    if (path === "/") topFolders = entries.filter(e => e.dir).map(e => e.name);
    renderCrumbs();
    renderSidebar();
    pv.dirty = false;
    renderList(focusName);
    updateNav();
    status("");
  } catch (err) {
    if (token === loadToken) status(err.message, "err");
  }
}

/* ---------------- preview pane ---------------- */
function setPreview(on) {
  previewOn = on;
  $("#body").classList.toggle("with-preview", on);
  $('[data-act="preview"]').setAttribute("aria-pressed", on);
  try { localStorage.setItem("3ds-ftp-preview", on ? "1" : "0"); } catch {}
  renderPreview();
}

function infoList(pairs) {
  const dl = el("dl");
  for (const [k, v] of pairs) if (v) dl.append(el("dt", "", k), el("dd", "", v));
  return dl;
}

function renderPreview() {
  if (!previewOn) return;
  const box = $("#preview");
  const token = ++pv.token;
  const later = fn => setTimeout(() => { if (token === pv.token) fn(); }, 150);
  pv.dirty = false;
  box.replaceChildren();
  const e = view[sel];
  if (!e) {
    box.append(el("p", "pv-empty", view.length ? "Select a file to preview it." : ""));
    return;
  }
  const path = join(cwd, e.name);
  const x = extOf(e.name);

  if (e.dir) {
    const big = icon(e);
    big.classList.add("pv-big");
    const s = sizes.get(path);
    box.append(big, el("h3", "", e.name), infoList([
      ["Type", "File folder"],
      ["Modified", fmtDate(e.modified)],
      ["Size", s ? fmtSize(s.size) : ""],
      ["Contains", s ? `${plural(s.files, "file")}, ${plural(s.folders, "folder")}` : ""],
    ]));
    const row = el("div", "pv-row");
    if (!s) {
      const c = el("button", "btn2", "Calculate size");
      c.onclick = () => calcSizes([e.name]);
      row.append(c);
    }
    const z = el("button", "btn2", "Download as zip");
    z.onclick = download;
    row.append(z);
    box.append(row);
    return;
  }

  const meta = infoList([["Type", typeOf(e)], ["Size", fmtSize(e.size)], ["Modified", fmtDate(e.modified)]]);

  if (IMAGE_EXT.includes(x) && e.size <= MAX_IMAGE) {
    const frame = el("div", "pv-img");
    const img = el("img");
    img.alt = e.name;
    later(() => { img.src = dlUrl(path, true, e.modified); });
    img.onerror = () => frame.replaceChildren(el("p", "pv-note", "This image couldn't be shown."));
    frame.append(img);
    box.append(frame, el("h3", "", e.name), meta);
    return;
  }

  if (isAudio(e)) {
    const big = icon(e);
    big.classList.add("pv-big");
    const playing = queue[qi]?.path === path;
    const row = el("div", "pv-row");
    const pb = el("button", "btn", playing ? (audio.paused ? "Resume" : "Playing") : "Play");
    pb.disabled = playing && !audio.paused;
    pb.onclick = () => (playing ? audio.play() : playSelected());
    const d = el("button", "btn2", "Download");
    d.onclick = download;
    row.append(pb, d);
    box.append(big, el("h3", "", e.name), meta, row);
    const others = view.filter(isAudio).length - 1;
    if (others > 0 && !playing) box.append(el("p", "pv-note", `Then plays the other ${plural(others, "song")} in this folder.`));
    return;
  }

  if (TEXT_EXT.includes(x) || (!x && e.size < 64 * 1024)) {
    box.append(el("h3", "", e.name), meta);
    if (e.size > MAX_TEXT) {
      box.append(el("p", "pv-note", "This file is too big to preview. Download it to open it."));
      return;
    }
    const note = el("p", "pv-note", "Loading…");
    box.append(note);
    later(() => fetch(dlUrl(path, true)).then(r => {
      if (!r.ok) throw new Error();
      return r.arrayBuffer();
    }).then(buf => {
      if (token !== pv.token) return;
      const bytes = new Uint8Array(buf);
      if (bytes.slice(0, 8000).includes(0)) {
        note.textContent = "This file isn't plain text, so there's no preview.";
        return;
      }
      const ta = el("textarea");
      ta.spellcheck = false;
      let text;
      try {
        // ignoreBOM keeps a leading byte-order mark in the text so saving puts it back.
        text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      } catch {
        ta.value = new TextDecoder("latin1").decode(bytes);
        ta.readOnly = true;
        note.textContent = "This file uses an older text encoding, so editing is off to avoid damaging it. Download it to edit.";
        note.after(ta);
        return;
      }
      // Text boxes turn Windows line endings (CRLF) into plain LF, so compare
      // and save in a way that keeps whatever the file used.
      const crlf = text.includes("\r\n");
      const original = crlf ? text.replace(/\r\n/g, "\n") : text;
      ta.value = original;
      const save = el("button", "btn", "Save");
      const revert = el("button", "btn2", "Undo changes");
      save.disabled = revert.disabled = true;
      ta.oninput = () => {
        pv.dirty = ta.value !== original;
        save.disabled = revert.disabled = !pv.dirty;
      };
      revert.onclick = () => { ta.value = original; ta.oninput(); };
      save.onclick = () => saveText(path, e.name, crlf ? ta.value.replace(/\n/g, "\r\n") : ta.value);
      ta.onkeydown = ev => {
        if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
          ev.preventDefault();
          if (pv.dirty) save.click();
        }
      };
      const row = el("div", "pv-row");
      row.append(save, revert);
      note.replaceWith(ta, row);
    }).catch(() => {
      if (token === pv.token) note.textContent = "Couldn't load this file.";
    }));
    return;
  }

  const big = icon(e);
  big.classList.add("pv-big");
  const d = el("button", "btn2", "Download");
  d.onclick = download;
  const row = el("div", "pv-row");
  row.append(d);
  box.append(big, el("h3", "", e.name), meta, el("p", "pv-note", "No preview for this kind of file."), row);
}

async function saveText(path, name, text) {
  status("Saving " + name + "…");
  try {
    const r = await fetch("/api/upload?path=" + encodeURIComponent(path), { method: "POST", body: new Blob([text]) });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || "Save failed");
    pv.dirty = false;
    sizes.clear();
    await load(cwd, name);
    status("Saved " + name, "ok");
  } catch (err) {
    status(err.message, "err");
  }
}

/* ---------------- actions ---------------- */
const selected = () => view[sel];

function download() {
  const e = selected();
  if (!e) return;
  const path = join(cwd, e.name);
  if (e.dir) {
    startDownload("/api/zip?path=" + encodeURIComponent(path));
    status(`Zipping ${e.name}. Your browser will save it when it's ready.`, "ok");
  } else {
    startDownload(dlUrl(path));
    status("Downloading " + e.name, "ok");
  }
}
function downloadHere() {
  startDownload("/api/zip?path=" + encodeURIComponent(cwd));
  status("Zipping this folder. Your browser will save it when it's ready.", "ok");
}
function openSel() {
  const e = selected();
  if (!e) return;
  if (e.dir) load(join(cwd, e.name));
  else if (isAudio(e)) playSelected();
  else download();
}
function back() {
  if (mode === "gallery") return load(cwd, null, "none");
  if (!hist.length || !canLeave()) return;
  const came = cwd;
  const to = hist.pop();
  fwd.push(came);
  const pre = to === "/" ? "/" : to + "/";
  load(to, came.startsWith(pre) ? came.slice(pre.length).split("/")[0] : null, "none");
}
function forward() {
  if (!fwd.length || !canLeave()) return;
  hist.push(cwd);
  load(fwd.pop(), null, "none");
}
function up() {
  if (cwd === "/") return;
  load(parent(cwd), cwd.split("/").filter(Boolean).pop());
}

async function rename() {
  const e = selected();
  if (!e || !canLeave()) return;
  const n = askName("Rename to", e.name, e.name);
  if (!n || n === e.name) return;
  try {
    if (affectsPlayer(join(cwd, e.name))) stopAudio();
    await post("/api/rename", { from: join(cwd, e.name), to: join(cwd, n) });
    sizes.clear();
    await load(cwd, n);
    status("Renamed to " + n, "ok");
  } catch (err) { status(err.message, "err"); }
}

async function remove() {
  const e = selected();
  if (!e) return;
  const msg = e.dir ? `Delete the folder "${e.name}" and everything inside it?` : `Delete "${e.name}"?`;
  if (!confirm(msg)) return;
  pv.dirty = false;
  status("Deleting " + e.name + "…");
  try {
    if (affectsPlayer(join(cwd, e.name))) stopAudio();
    await post("/api/delete", { path: join(cwd, e.name), dir: e.dir });
    sizes.clear();
    const next = view[sel + 1]?.name || view[sel - 1]?.name;
    await load(cwd, next);
    status("Deleted " + e.name, "ok");
  } catch (err) { status(err.message, "err"); }
}

async function mkdir() {
  let start = "New folder";
  for (let k = 2; entries.some(x => x.name.toLowerCase() === start.toLowerCase()); k++) start = `New folder (${k})`;
  const n = askName("New folder name", start);
  if (!n) return;
  try {
    await post("/api/mkdir", { path: join(cwd, n) });
    await load(cwd, n);
  } catch (err) { status(err.message, "err"); }
}

/* folder sizes */
async function calcSizes(names) {
  if (job) return;
  names = names || view.filter(e => e.dir).map(e => e.name);
  if (!names.length) return status("There are no folders here.");
  const j = startJob();
  const base = cwd;
  let done = 0;
  try {
    for (const name of names) {
      if (j.cancelled || cwd !== base || mode !== "files") break;  // stop if they moved on
      status(`Calculating folder sizes (${done + 1} of ${names.length}): ${name}`);
      const path = join(base, name);
      const result = await api("/api/du?path=" + encodeURIComponent(path));
      sizes.set(path, result);
      if (cwd === base) refreshSizeCell(name);
      done++;
    }
    if (cwd !== base || mode !== "files") j.cancelled = true;
    status(j.cancelled ? `Stopped after ${plural(done, "folder")}.` : `Calculated ${plural(done, "folder size")}.`, j.cancelled ? "" : "ok");
  } catch (err) {
    status(err.message, "err");
  }
  endJob();
  updateSelInfo();
  if (view[sel]?.dir && !pv.dirty) renderPreview();
}

/* ---------------- uploads ---------------- */
function uploadOne(file, target, i, total) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    xhr.open("POST", "/api/upload?path=" + encodeURIComponent(target));
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) {
        const pct = Math.round(e.loaded / e.total * 100);
        status(`Uploading ${total > 1 ? `${i + 1} of ${total}: ` : ""}${file.name} ${pct}%`);
      }
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      xhr.status === 200 && !data.error ? resolve() : reject(new Error(data.error || "Upload failed"));
    };
    xhr.onabort = () => reject(new Error("Upload cancelled"));
    xhr.onerror = () => reject(new Error("Upload failed. Is the launcher window still open?"));
    xhr.send(file);
  });
}

function askConflict(item, remote, remaining) {
  const dlg = $("#conflict");
  $("#cf-msg").textContent = `"${item.name}" is already on the 3DS.`;
  $("#cf-local-label").textContent = item.from || "From this computer";
  dlg.querySelector('[data-choice="cancel"]').textContent = item.cancel || "Cancel upload";
  $("#cf-local").textContent = fmtSize(item.size) + "\n" + item.date;
  $("#cf-remote").textContent = fmtSize(remote.size) + "\n" + fmtDate(remote.modified);
  $("#cf-all").checked = false;
  $("#cf-all-row").hidden = remaining < 1;
  $("#cf-all-text").textContent = `Do this for the next ${plural(remaining, "conflict")}`;
  return new Promise(resolve => {
    const finish = choice => {
      dlg.querySelectorAll("[data-choice]").forEach(b => b.onclick = null);
      dlg.oncancel = null;
      dlg.close();
      resolve({ choice, all: $("#cf-all").checked });
    };
    dlg.querySelectorAll("[data-choice]").forEach(b => b.onclick = () => finish(b.dataset.choice));
    dlg.oncancel = ev => { ev.preventDefault(); finish("cancel"); };
    dlg.showModal();
    dlg.querySelector('[data-choice="replace"]').focus();
  });
}

async function uploadBatch(items, dirs = []) {
  if (mode === "gallery") return status("Open a folder first, then upload into it.");
  if (job) return status("Wait for the current task to finish, or cancel it.");
  const junk = items.filter(f => isJunk(f.rel)).length;
  items = items.filter(f => !isJunk(f.rel));
  dirs = dirs.filter(d => !isJunk(d));
  if (!items.length && !dirs.length) return status("Nothing to upload.");

  const j = startJob();
  let done = 0, skipped = 0, failed = null;
  const focus = (items[0]?.rel || dirs[0]).split("/")[0];
  try {
    const allDirs = new Set();
    const addTree = rel => {
      const parts = rel.split("/");
      for (let i = 1; i <= parts.length; i++) allDirs.add(parts.slice(0, i).join("/"));
    };
    dirs.forEach(addTree);
    items.forEach(f => { const d = f.rel.split("/").slice(0, -1).join("/"); if (d) addTree(d); });

    const targets = items.map(f => join(cwd, f.rel));
    status("Checking for files that are already on the 3DS…");
    const { existing } = targets.length ? await post("/api/exists", { paths: targets }) : { existing: {} };
    if (allDirs.size) {
      status("Creating folders…");
      await post("/api/mkdirs", { paths: [...allDirs].map(d => join(cwd, d)) });
    }

    let left = targets.filter(t => existing[t]).length, always = null;
    for (let i = 0; i < items.length && !j.cancelled; i++) {
      const t = targets[i];
      if (existing[t]) {
        left--;
        let choice = always;
        if (!choice) {
          const f = items[i].file;
          const r = await askConflict({
            name: items[i].rel, size: f.size,
            date: new Date(f.lastModified).toLocaleString([], { dateStyle: "short", timeStyle: "short" }),
          }, existing[t], left);
          choice = r.choice;
          if (r.all) always = choice;
        }
        if (choice === "cancel") { j.cancelled = true; break; }
        if (choice === "skip") { skipped++; continue; }
      }
      await uploadOne(items[i].file, t, i, items.length);
      done++;
    }
  } catch (err) {
    if (!j.cancelled) failed = err.message;
  }
  endJob();
  sizes.clear();
  await load(cwd, focus);

  const parts = [`Uploaded ${plural(done, "file")}`];
  if (skipped) parts.push(`skipped ${skipped}`);
  if (junk) parts.push(`left out ${plural(junk, "system file")}`);
  if (failed) status(failed + ` (${parts.join(", ").toLowerCase()} before that)`, "err");
  else status(parts.join(", ") + (j.cancelled ? ". Cancelled." : "."), j.cancelled ? "" : "ok");
}

async function itemsFromDrop(dt) {
  // Grab the entries right away; the browser clears them after the drop event.
  const roots = [...dt.items].filter(i => i.kind === "file").map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!roots.length) return { items: [...dt.files].map(f => ({ file: f, rel: f.name })), dirs: [] };
  const items = [], dirs = [];
  async function walk(entry, rel) {
    if (entry.isFile) {
      items.push({ file: await new Promise((res, rej) => entry.file(res, rej)), rel });
    } else if (entry.isDirectory) {
      dirs.push(rel);
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const c of batch) await walk(c, rel + "/" + c.name);
      } while (batch.length);
    }
  }
  for (const r of roots) await walk(r, r.name);
  return { items, dirs };
}

async function disconnect() {
  if (!canLeave()) return;
  if (job) { job.cancelled = true; job.xhr?.abort(); }
  stopAudio();
  if ($("#lightbox").open) $("#lightbox").close();
  setMode("files");
  gallery = [];
  try { await post("/api/disconnect", {}); } catch {}
  setConnected(false);
  entries = []; view = []; sel = -1; cwd = "/"; hist = []; fwd = []; topFolders = [];
  sizes.clear();
  $("#list").replaceChildren();
  $("#preview").replaceChildren();
  $("#count").textContent = "";
  $("#selinfo").textContent = "";
  status("Disconnected.");
}

const actions = {
  mkdir,
  upload: () => $("#file-input").click(),
  uploadFolder: () => $("#folder-input").click(),
  download, rename, delete: remove,
  sizes: () => calcSizes(),
  preview: () => setPreview(!previewOn),
  refresh: () => (mode === "gallery" ? openGallery() : load(cwd, selected()?.name)),
  disconnect, back, forward, parent: up, open: openSel,
};
document.querySelectorAll("[data-act]").forEach(b =>
  b.addEventListener("click", () => { if (connected) actions[b.dataset.act](); })
);
document.querySelectorAll(".cmd").forEach(b => { b.title = b.textContent.trim(); b.setAttribute("aria-label", b.title); });

/* ---------------- context menu ---------------- */
function showMenu(x, y) {
  const e = selected();
  const m = $("#menu");
  m.replaceChildren();
  const add = (label, fn) => {
    const b = el("button", "", label);
    b.type = "button";
    b.onclick = () => { hideMenu(); fn(); };
    m.append(b);
  };
  if (e) {
    if (e.dir) {
      add("Open", openSel);
      add("Download as zip", download);
      add("Calculate size", () => calcSizes([e.name]));
    } else {
      if (isAudio(e)) add("Play", playSelected);
      add("Download", download);
    }
    add("Rename", rename);
    add("Delete", remove);
    m.append(el("hr"));
  }
  add("New folder", mkdir);
  add("Upload files", actions.upload);
  add("Upload folder", actions.uploadFolder);
  if (!e) {
    add("Download this folder as zip", downloadHere);
    add("Calculate folder sizes", actions.sizes);
  }
  add("Refresh", actions.refresh);
  m.hidden = false;
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
  m.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
}
function hideMenu() { $("#menu").hidden = true; }
addEventListener("click", e => { if (!$("#menu").contains(e.target)) hideMenu(); });
addEventListener("blur", hideMenu);
$("#list").addEventListener("contextmenu", e => {
  if (e.target === $("#list")) { e.preventDefault(); select(-1); if (sel === -1) showMenu(e.clientX, e.clientY); }
});
$("#list").addEventListener("click", e => { if (e.target === $("#list")) select(-1); });

/* ---------------- sorting and search ---------------- */
document.querySelectorAll("#cols button").forEach(b => b.onclick = () => {
  if (!canLeave()) return;
  if (sortKey === b.dataset.key) sortDir = -sortDir;
  else { sortKey = b.dataset.key; sortDir = 1; }
  renderList(selected()?.name);
});
$("#filter").addEventListener("input", () => { if (canLeave()) renderList(selected()?.name); });

/* ---------------- keyboard ---------------- */
document.addEventListener("keydown", e => {
  if (e.key === "Escape") hideMenu();
  if ($("#lightbox").open) {
    if (e.key === "ArrowLeft") { e.preventDefault(); lbStep(-1); }
    if (e.key === "ArrowRight") { e.preventDefault(); lbStep(1); }
    if (e.key === "Delete") { e.preventDefault(); lbDelete(); }
    return;
  }
  if (!connected || $("#conflict").open) return;
  if (e.key === " " && !$("#player").hidden && !e.target.matches("input, textarea, button, a")) {
    e.preventDefault();
    return audio.paused ? audio.play() : audio.pause();
  }
  const typing = e.target.matches("input, textarea");
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); return back(); }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); return forward(); }
  if (e.altKey && e.key === "ArrowUp") { e.preventDefault(); return up(); }
  if (e.altKey && e.key.toLowerCase() === "p") { e.preventDefault(); return setPreview(!previewOn); }
  if (e.key === "F5") { e.preventDefault(); return actions.refresh(); }
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "n") { e.preventDefault(); return mkdir(); }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (mode === "gallery") {
    if (e.key === "Backspace") { e.preventDefault(); back(); }
    return;
  }
  const keys = {
    ArrowDown: () => select(sel + 1),
    ArrowUp: () => select(sel < 0 ? view.length - 1 : sel - 1),
    Home: () => select(0),
    End: () => select(view.length - 1),
    Enter: openSel,
    Backspace: back,
    Delete: remove,
    F2: rename,
  };
  if (keys[e.key] && !(e.key === "Enter" && e.target.matches("button"))) {
    e.preventDefault();
    keys[e.key]();
  }
});

/* ---------------- window controls ---------------- */
$("#minbtn").onclick = () => { $("#win").classList.add("min"); document.body.classList.add("minimized"); };
$("#restore").onclick = () => { $("#win").classList.remove("min"); document.body.classList.remove("minimized"); };
$("#maxbtn").onclick = () => $("#win").classList.toggle("max");
$(".titlebar").ondblclick = e => { if (!e.target.closest("button")) $("#win").classList.toggle("max"); };
$("#closebtn").onclick = () => { if (connected) disconnect(); };

/* ---------------- connect and find ---------------- */
$("#connect-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  status("Connecting to " + f.host + "…");
  try {
    await post("/api/connect", f);
    try { localStorage.setItem("3ds-ftp", JSON.stringify({ host: f.host, port: f.port, user: f.user })); } catch {}
    setConnected(true);
    cwd = "";
    await load("/", null, "none");
  } catch (err) {
    status(err.message + " You can also try Find my 3DS.", "err");
  }
});

async function findConsoles() {
  const btn = $("#findbtn"), box = $("#found");
  const port = $("[name=port]").value || "5000";
  btn.disabled = true;
  box.replaceChildren();
  status(`Looking for FTP servers on port ${port}…`);
  try {
    const { found } = await api("/api/scan?port=" + encodeURIComponent(port));
    if (connected) return;  // they connected while the search was running
    if (!found.length) {
      status(`Nothing answered on port ${port}. Make sure ftpd is open on the 3DS and it's on the same Wi-Fi as this computer.`, "err");
    } else if (found.length === 1) {
      $("[name=host]").value = found[0].host;
      status(`Found an FTP server at ${found[0].host}. Press Connect.`, "ok");
    } else {
      status(`Found ${found.length} FTP servers. Pick your 3DS.`, "ok");
      for (const f of found) {
        const b = el("button");
        b.type = "button";
        b.append(el("span", "", f.host), el("small", "", f.banner));
        b.onclick = () => { $("[name=host]").value = f.host; box.replaceChildren(); status("Press Connect."); };
        box.append(b);
      }
    }
  } catch (err) {
    if (!connected) status(err.message, "err");
  }
  btn.disabled = false;
}
$("#findbtn").onclick = findConsoles;

/* ---------------- file pickers and drag and drop ---------------- */
$("#file-input").onchange = e => {
  uploadBatch([...e.target.files].map(f => ({ file: f, rel: f.name })));
  e.target.value = "";
};
$("#folder-input").onchange = e => {
  const items = [...e.target.files].map(f => ({ file: f, rel: f.webkitRelativePath || f.name }));
  if (items.length) uploadBatch(items);
  e.target.value = "";
};
const drop = $("#drop");
drop.addEventListener("dragover", e => { if (connected && !job && mode === "files") { e.preventDefault(); drop.classList.add("drag"); } });
drop.addEventListener("dragleave", e => { if (!drop.contains(e.relatedTarget)) drop.classList.remove("drag"); });
drop.addEventListener("drop", async e => {
  e.preventDefault();
  drop.classList.remove("drag");
  if (!connected) return;
  if (mode === "gallery") return status("Open a folder first, then upload into it.");
  const pending = itemsFromDrop(e.dataTransfer);
  status("Reading dropped files…");
  try {
    const { items, dirs } = await pending;
    uploadBatch(items, dirs);
  } catch {
    status("Couldn't read those files. Try the Upload buttons instead.", "err");
  }
});

/* ---------------- modes ---------------- */
function setMode(m) {
  mode = m;
  document.body.classList.toggle("gallery-mode", m === "gallery");
  for (const a of ["mkdir", "upload", "uploadFolder", "sizes"]) $(`[data-act="${a}"]`).disabled = m === "gallery";
  $("#filter").disabled = m === "gallery";
  if (m !== "gallery" && $("#lightbox").open) $("#lightbox").close();
}

/* ---------------- screenshots and photos ---------------- */
async function openGallery() {
  if (!canLeave()) return;
  setMode("gallery");
  select(-1, true);
  $("#filter").value = "";
  const home = el("button", "crumb", "SD card");
  home.type = "button";
  home.onclick = () => load(cwd === "" ? "/" : cwd, null, "none");
  $("#crumbs").replaceChildren(el("span", "ico drive"), home, el("span", "chev", "›"), el("span", "crumb", "Screenshots and photos"));
  renderSidebar();
  $("#count").textContent = "";
  $("#gallery").replaceChildren(el("p", "pv-empty", "Looking for screenshots and photos…"));
  status("Searching /luma/screenshots and /DCIM…");
  try {
    const q = GALLERY_ROOTS.map(r => "root=" + encodeURIComponent(r)).join("&") + "&ext=" + IMAGE_EXT.join(",");
    const { files } = await api("/api/find?" + q);
    if (mode !== "gallery") return;
    gallery = files.sort((a, b) => (b.modified || "").localeCompare(a.modified || "") || b.name.localeCompare(a.name));
    gsel.clear();
    renderGallery();
    status("");
  } catch (err) {
    status(err.message, "err");
  }
}

function renderGallery() {
  const g = $("#gallery");
  g.replaceChildren();
  const bar = el("div", "g-bar");
  bar.append(el("span", "g-count"));
  const dl = el("button", "btn2");
  dl.id = "g-dl";
  dl.onclick = () => {
    const pick = gsel.size ? gallery.filter(f => gsel.has(f.path)) : gallery;
    zipMany(pick.map(f => ({ path: f.path, dir: false })), "3DS screenshots and photos.zip");
    status(`Zipping ${plural(pick.length, "image")}. Your browser will save it when it's ready.`, "ok");
  };
  const clr = el("button", "btn2", "Clear selection");
  clr.id = "g-clr";
  clr.onclick = () => { gsel.clear(); g.querySelectorAll(".g-card").forEach(c => { c.classList.remove("on"); c.querySelector("input").checked = false; }); updateGalleryBar(); };
  const del = el("button", "btn2", "Delete selected");
  del.id = "g-del";
  del.onclick = deleteGallerySelected;
  bar.append(dl, clr, del);
  g.append(bar);
  $("#count").textContent = plural(gallery.length, "image");

  if (!gallery.length) {
    g.append(el("p", "pv-empty", "No screenshots or photos found. Luma saves screenshots to /luma/screenshots and the camera saves photos to /DCIM."));
    updateGalleryBar();
    return;
  }
  const grid = el("div", "g-grid");
  gallery.forEach((f, i) => {
    const card = el("div", "g-card" + (gsel.has(f.path) ? " on" : ""));
    card.tabIndex = 0;
    const img = el("img");
    img.loading = "lazy";
    img.alt = f.name;
    img.src = dlUrl(f.path, true, f.modified);
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = gsel.has(f.path);
    cb.setAttribute("aria-label", "Select " + f.name);
    cb.onclick = ev => {
      ev.stopPropagation();
      cb.checked ? gsel.add(f.path) : gsel.delete(f.path);
      card.classList.toggle("on", cb.checked);
      updateGalleryBar();
    };
    const cap = el("div", "g-cap");
    cap.append(el("b", "", f.name), el("small", "", fmtDate(f.modified)));
    card.append(img, cb, cap);
    card.title = f.path;
    card.onclick = () => openLightbox(i);
    card.onkeydown = ev => { if (ev.key === "Enter") openLightbox(i); };
    grid.append(card);
  });
  g.append(grid);
  updateGalleryBar();
}

function updateGalleryBar() {
  const n = gsel.size;
  const c = $("#gallery .g-count");
  if (!c) return;
  c.textContent = gallery.length ? plural(gallery.length, "image") + (n ? `, ${n} selected` : "") + ". Newest first." : "";
  $("#g-dl").textContent = n ? `Download selected (${n})` : "Download all";
  $("#g-dl").disabled = !gallery.length;
  $("#g-clr").hidden = !n;
  $("#g-del").hidden = !n;
}

async function deleteGallerySelected() {
  const pick = gallery.filter(f => gsel.has(f.path));
  if (!pick.length || !confirm(`Delete ${plural(pick.length, "image")} from the SD card?`)) return;
  let done = 0;
  try {
    for (const f of pick) {
      status(`Deleting ${done + 1} of ${pick.length}…`);
      await post("/api/delete", { path: f.path, dir: false });
      done++;
    }
    status(`Deleted ${plural(done, "image")}.`, "ok");
  } catch (err) {
    status(err.message, "err");
  }
  const gone = new Set(pick.slice(0, done).map(f => f.path));
  gallery = gallery.filter(f => !gone.has(f.path));
  gone.forEach(p => gsel.delete(p));
  sizes.clear();
  renderGallery();
}

function openLightbox(i) {
  lbIndex = i;
  showLightbox();
  if (!$("#lightbox").open) $("#lightbox").showModal();
}
function showLightbox() {
  const f = gallery[lbIndex];
  if (!f) return $("#lightbox").close();
  $("#lb-img").src = dlUrl(f.path, true, f.modified);
  $("#lb-img").alt = f.name;
  $("#lb-name").textContent = f.name;
  $("#lb-meta").textContent = `${fmtSize(f.size)}, ${fmtDate(f.modified)}, in ${parent(f.path)}`;
  $("#lb-prev").disabled = lbIndex <= 0;
  $("#lb-next").disabled = lbIndex >= gallery.length - 1;
}
function lbStep(d) {
  const n = lbIndex + d;
  if (n >= 0 && n < gallery.length) { lbIndex = n; showLightbox(); }
}
async function lbDelete() {
  const f = gallery[lbIndex];
  if (!f || !confirm(`Delete "${f.name}" from the SD card?`)) return;
  try {
    await post("/api/delete", { path: f.path, dir: false });
    gallery.splice(lbIndex, 1);
    gsel.delete(f.path);
    sizes.clear();
    renderGallery();
    status("Deleted " + f.name, "ok");
    if (!gallery.length) return $("#lightbox").close();
    lbIndex = Math.min(lbIndex, gallery.length - 1);
    showLightbox();
  } catch (err) {
    status(err.message, "err");
  }
}
$("#lb-img").addEventListener("load", e => e.target.classList.toggle("small", e.target.naturalWidth < 800));
$("#lb-prev").onclick = () => lbStep(-1);
$("#lb-next").onclick = () => lbStep(1);
$("#lb-close").onclick = () => $("#lightbox").close();
$("#lb-del").onclick = lbDelete;
$("#lb-dl").onclick = () => { const f = gallery[lbIndex]; if (f) startDownload(dlUrl(f.path)); };
$("#lightbox").addEventListener("click", e => { if (e.target === $("#lightbox")) $("#lightbox").close(); });

/* ---------------- save backup and restore ---------------- */
async function backupSaves() {
  status("Looking for save folders…");
  try {
    const { found } = await post("/api/stat", { paths: SAVE_DIRS });
    const dirs = SAVE_DIRS.filter(p => found[p]?.dir);
    if (!dirs.length) {
      return status("Couldn't find Checkpoint or JKSM saves on the SD card. It looks in /3ds/Checkpoint and /JKSV.", "err");
    }
    const d = new Date();
    const stamp = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    zipMany(dirs.map(p => ({ path: p, dir: true })), `3DS save backup ${stamp}.zip`);
    status(`Backing up ${dirs.join(", ")}. Your browser will save the zip when it's ready.`, "ok");
  } catch (err) {
    status(err.message, "err");
  }
}

function postFile(url, file, label) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    xhr.open("POST", url);
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) status(`${label} ${Math.round(e.loaded / e.total * 100)}%`);
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      xhr.status === 200 && !data.error ? resolve(data) : reject(new Error(data.error || "Something went wrong"));
    };
    xhr.onabort = () => reject(new Error("Cancelled"));
    xhr.onerror = () => reject(new Error("Lost contact with the launcher. Is its window still open?"));
    xhr.send(file);
  });
}

async function restoreFrom(file) {
  if (job) return status("Wait for the current task to finish, or cancel it.");
  const j = startJob();
  let info = null, finished = false;
  const discard = () => info && post("/api/restore/discard", { token: info.token }).catch(() => {});
  try {
    info = await postFile("/api/restore/inspect", file, "Reading " + file.name);
    if (!info.files.length) {
      discard();
      status("That zip has no files in it.", "err");
    } else {
      const folders = [...new Set(info.files.map(f => {
        const parts = f.path.split("/").slice(1, -1);
        return parts.length ? "/" + parts.slice(0, 3).join("/") : "/ (top of the SD card)";
      }))];
      const shown = folders.slice(0, 8).join("\n") + (folders.length > 8 ? `\nand ${folders.length - 8} more` : "");
      if (!confirm(`Restore ${plural(info.files.length, "file")} from ${file.name} to the SD card?\n\nThey'll go into:\n${shown}`)) {
        discard();
        status("Restore cancelled.");
      } else {
        const skip = [];
        const clashes = info.files.filter(f => info.existing[f.path]);
        let left = clashes.length, always = null, cancelled = false;
        for (const f of clashes) {
          left--;
          let choice = always;
          if (!choice) {
            const r = await askConflict({ name: f.path.slice(1), size: f.size, date: fmtStamp(f.date), from: "From the backup", cancel: "Cancel restore" }, info.existing[f.path], left);
            choice = r.choice;
            if (r.all) always = choice;
          }
          if (choice === "cancel") { cancelled = true; break; }
          if (choice === "skip") skip.push(f.path);
        }
        if (cancelled) {
          discard();
          status("Restore cancelled.");
        } else {
          status("Restoring…");
          const r = await post("/api/restore/apply", { token: info.token, skip });
          finished = true;
          status(`Restored ${plural(r.written, "file")}${skip.length ? `, skipped ${skip.length}` : ""}.`, "ok");
        }
      }
    }
  } catch (err) {
    discard();
    status(err.message, "err");
  }
  endJob();
  if (finished) {
    sizes.clear();
    if (mode === "files") {
      const msg = $("#status").textContent;
      await load(cwd, selected()?.name);
      status(msg, "ok");
    }
  }
}
$("#restore-input").onchange = e => {
  const f = e.target.files[0];
  e.target.value = "";
  if (f) restoreFrom(f);
};

/* ---------------- music player ---------------- */
const audio = $("#audio");
const PLAY_ICON = "M5 3.5l8 4.5-8 4.5z", PAUSE_ICON = "M5.5 3.5v9M10.5 3.5v9";

function playSelected() {
  const e = selected();
  if (!isAudio(e)) return;
  const tracks = view.filter(isAudio).map(x => ({ path: join(cwd, x.name), name: x.name }));
  queue = tracks;
  qi = tracks.findIndex(t => t.name === e.name);
  startTrack();
}
function startTrack() {
  const t = queue[qi];
  if (!t) return stopAudio();
  audio.src = dlUrl(t.path, true);
  audio.play().catch(() => {});
  $("#player").hidden = false;
  $("#pl-title").textContent = t.name;
  const where = parent(t.path) === "/" ? "SD card" : parent(t.path);
  $("#pl-where").textContent = queue.length > 1 ? `${where}, song ${qi + 1} of ${queue.length}` : where;
  $("#pl-seek").value = 0;
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: t.name.replace(/\.[^.]+$/, ""), album: where });
  }
  refreshAudioPreview();
}
function stopAudio() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  queue = []; qi = -1;
  $("#player").hidden = true;
  refreshAudioPreview();
}
function nextTrack() { if (qi < queue.length - 1) { qi++; startTrack(); } }
function prevTrack() {
  if (audio.currentTime > 3 || qi <= 0) audio.currentTime = 0;
  else { qi--; startTrack(); }
}
function refreshAudioPreview() {
  if (previewOn && mode === "files" && isAudio(view[sel]) && !pv.dirty) renderPreview();
}
function setPlayIcon() {
  const playing = !audio.paused;
  $("#pl-icon").innerHTML = `<path d="${playing ? PAUSE_ICON : PLAY_ICON}"/>`;
  $("#pl-play").setAttribute("aria-label", playing ? "Pause" : "Play");
}
audio.addEventListener("play", () => { setPlayIcon(); refreshAudioPreview(); });
audio.addEventListener("pause", () => { setPlayIcon(); refreshAudioPreview(); });
audio.addEventListener("loadedmetadata", () => { $("#pl-dur").textContent = fmtTime(audio.duration); });
audio.addEventListener("timeupdate", () => {
  $("#pl-cur").textContent = fmtTime(audio.currentTime);
  if (!seeking && audio.duration) $("#pl-seek").value = Math.round(audio.currentTime / audio.duration * 1000);
});
audio.addEventListener("ended", () => { if (qi < queue.length - 1) nextTrack(); });
audio.addEventListener("error", () => {
  if (!audio.getAttribute("src")) return;
  status(`Couldn't play ${queue[qi]?.name || "that file"}. The browser may not support this format.`, "err");
  if (qi < queue.length - 1) setTimeout(nextTrack, 1200);
});
$("#pl-play").onclick = () => (audio.paused ? audio.play() : audio.pause());
$("#pl-prev").onclick = prevTrack;
$("#pl-next").onclick = nextTrack;
$("#pl-stop").onclick = stopAudio;
$("#pl-seek").addEventListener("input", () => {
  seeking = true;
  if (audio.duration) $("#pl-cur").textContent = fmtTime($("#pl-seek").value / 1000 * audio.duration);
});
$("#pl-seek").addEventListener("change", () => {
  if (audio.duration) audio.currentTime = $("#pl-seek").value / 1000 * audio.duration;
  seeking = false;
});
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("previoustrack", prevTrack);
  navigator.mediaSession.setActionHandler("nexttrack", nextTrack);
}

/* ---------------- startup ---------------- */
let savedHost = false;
try {
  const saved = JSON.parse(localStorage.getItem("3ds-ftp") || "null");
  if (saved) for (const k of ["host", "port", "user"]) if (saved[k]) $(`[name=${k}]`).value = saved[k];
  savedHost = !!saved?.host;
  previewOn = localStorage.getItem("3ds-ftp-preview") !== "0";
} catch {}
setPreview(previewOn);
if (!savedHost) findConsoles();
</script>
</body>
</html>
"""


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets two programs share a port, so a second
    # copy would start "fine" while the old one kept answering. Turn it off
    # and claim the port exclusively instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ask_old_copy_to_quit(url):
    try:
        req = urllib.request.Request(url + "/api/quit", data=b"{}", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        return True
    except Exception:
        return False


def main():
    url = f"http://{LISTEN_HOST}:{LISTEN_PORT}"
    server = None
    for attempt in range(3):
        try:
            server = Server((LISTEN_HOST, LISTEN_PORT), Handler)
            break
        except OSError:
            if attempt == 0 and ask_old_copy_to_quit(url):
                print("Closed an older copy that was still running.")
            time.sleep(1.5)
    if server is None:
        print(f"Port {LISTEN_PORT} is already in use, probably by an older copy of this")
        print("program that's still running. Close any other 3DS FTP windows (or end")
        print("python.exe in Task Manager) and start this again.")
        input("Press Enter to close.")
        return

    print(f"3DS FTP Browser (Explorer style) running at {url}")
    print("Leave this window open while you use it. Press Ctrl+C or close it to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    print("Stopped.")


if __name__ == "__main__":
    main()
