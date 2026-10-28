@echo off
REM Always work from the folder this script is in
cd /d "%~dp0"

echo ================================================
echo Starting YT Bulk Subtitles Downloader
echo ================================================
echo.

REM Check if the virtual environment exists
if not exist venv\Scripts\python.exe (
    echo Virtual environment not found.
    echo Please run setup.bat first to set up the environment.
    pause
    exit /b 1
)

REM Run the downloader with the venv's Python
venv\Scripts\python.exe ytbsd.py

REM This part runs if the program exits or crashes
echo.
echo ================================================
echo Program has stopped. Check the output above for any errors.
echo ================================================
echo.
pause
