@echo off
rem 3DS FTP Browser launcher for Windows
title 3DS FTP Browser
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 3ds_ftp_browser.py
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python 3ds_ftp_browser.py
    goto done
)

echo Python 3 isn't installed.
echo Download it from https://www.python.org/downloads/
echo During setup, check the box "Add python.exe to PATH", then run this again.

:done
echo.
pause
