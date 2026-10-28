@echo off
REM Builds a single self-contained ytbsd.exe (Python + all libraries + icon).
REM Output: dist\ytbsd.exe
cd /d "%~dp0"

echo ================================================
echo Building ytbsd.exe
echo ================================================
echo.

if not exist venv\Scripts\python.exe (
    echo Virtual environment not found.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

echo Installing/updating PyInstaller...
venv\Scripts\python.exe -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo Failed to install PyInstaller.
    pause
    exit /b 1
)
echo.

REM --onefile            : everything packed into one exe
REM --console            : CLI app, needs a console window
REM --icon               : exe icon shown in Explorer / taskbar
REM --collect-submodules : rich loads some modules dynamically
venv\Scripts\python.exe -m PyInstaller ^
    --noconfirm --clean ^
    --onefile ^
    --console ^
    --name ytbsd ^
    --icon icon.ico ^
    --collect-submodules rich ^
    ytbsd.py
if errorlevel 1 (
    echo.
    echo Build FAILED - check the messages above.
    pause
    exit /b 1
)

echo.
echo ================================================
echo Build complete: dist\ytbsd.exe
echo ================================================
echo Copy ytbsd.exe to any folder and run it. The subtitles folder,
echo proxies.txt and .progress.json are created next to the exe.
echo.
pause
