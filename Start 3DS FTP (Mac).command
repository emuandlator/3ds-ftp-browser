#!/bin/bash
# 3DS FTP Browser launcher for macOS
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
    python3 3ds_ftp_browser.py
else
    echo "Python 3 isn't installed."
    echo "Download it from https://www.python.org/downloads/ and run this again."
    read -n 1 -s -r -p "Press any key to close."
fi
