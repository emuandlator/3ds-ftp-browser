# 3DS FTP Browser

Browse and manage the SD card on a modded 3DS from your computer's web browser, over Wi-Fi. Browsers can't talk FTP on their own, so this Python script runs a small web server on your computer, opens the page, and passes each request to the 3DS's FTP app.

It's one Python file with no extra packages to install.

![The file browser connected to a 3DS SD card](https://github.com/user-attachments/assets/1ecd4f14-651d-4ef6-bd33-b3e0a0debcdd)

## Screenshots

Connecting to the 3DS:

![Connect screen](https://github.com/user-attachments/assets/2091e0e1-484b-4612-9620-a1137d89685e)

Previewing an image from the SD card:

![File preview](https://github.com/user-attachments/assets/646f08c4-1cd5-4a53-9152-7abb15583d20)

All screenshots and photos in one place:

![Screenshots and photos view](https://github.com/user-attachments/assets/d74b3891-50f3-43f7-8b79-af82209f39b2)

The screenshots show sample files, not a real SD card.

## What it does

- Finds your 3DS on the local network, or connects to the IP address it shows on screen
- Browse folders, search the current folder, and see folder sizes
- Upload files or whole folders, download files, or download a folder as a .zip
- Rename, delete and make new folders
- Preview images, text files and audio right in the page
- Pull up all your screenshots and photos in one place
- Back up your save folders to a .zip and restore them later

## What you need

- A 3DS running an FTP homebrew app such as ftpd, on the same Wi-Fi as your computer
- Python 3.8 or newer on your computer ([python.org/downloads](https://www.python.org/downloads/)). On Windows, check "Add python.exe to PATH" during setup.

## Installation

There's nothing to install beyond Python. Click the green Code button on this page, choose Download ZIP, and unzip it anywhere.

## How to use it

1. Open ftpd on the 3DS. It shows an IP address and port (usually 5000).
2. Start the app:
   - Windows: double-click `Start 3DS FTP (Windows).bat`
   - Mac: double-click `Start 3DS FTP (Mac).command`. The first time, macOS may block it. Right-click it, choose Open, then confirm. If it still won't run, open Terminal in the folder and run `chmod +x "Start 3DS FTP (Mac).command"`.
   - Or from a terminal: `python3 3ds_ftp_browser.py`
3. Your browser opens to http://127.0.0.1:8021. If it doesn't, open that address yourself.
4. Click Find my 3DS, or type the IP address and port from the 3DS screen, then Connect.

Leave the terminal window open while you use it. Closing it stops the app.

## Privacy

The page is only reachable from your own computer (it listens on 127.0.0.1). Nothing is sent anywhere except between your computer and your 3DS.
