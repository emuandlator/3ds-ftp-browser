# 3DS FTP Browser

Browse and manage the SD card on a modded 3DS from your computer's web browser, over Wi-Fi. Browsers can't talk FTP on their own, so this Python script runs a small web server on your computer, opens the page, and passes each request to the 3DS's FTP app.

It's one Python file with no extra packages to install.

![The file browser connected to a 3DS SD card](https://github.com/user-attachments/assets/0d8045ac-4e08-44ba-98ba-966971a31e0a)

## Screenshots

Connecting to the 3DS:

![Connect screen](https://github.com/user-attachments/assets/4c8f317d-66da-4a1f-9c4c-6acab9a2b903)

Previewing an image from the SD card:

![File preview](https://github.com/user-attachments/assets/cb79305a-3031-4420-a83d-969c45b9d87c)

All screenshots and photos in one place:

![Screenshots and photos view](https://github.com/user-attachments/assets/c074c4c8-267e-48f2-aadd-cb17e6f30cc9)

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
