@echo off
REM Always work from the folder this script is in (also when started
REM from another folder or via "Run as administrator")
cd /d "%~dp0"

echo ================================================
echo Setting up YT Bulk Subtitles Downloader
echo ================================================
echo.

REM Check if Python is installed
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not in PATH.
    echo Please install Python 3.10 or newer and try again.
    pause
    exit /b 1
)

REM The script uses "str | None" type hints, which need Python 3.10+
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if %errorlevel% neq 0 (
    echo Python 3.10 or newer is required. Found:
    python --version
    pause
    exit /b 1
)

if exist venv\Scripts\python.exe (
    echo Virtual environment already exists, reusing it.
) else (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        echo Make sure you have Python's venv module available.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
)
echo.

echo Upgrading pip...
venv\Scripts\python.exe -m pip install --upgrade pip
echo.

echo Installing dependencies from requirements.txt...
venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt
if %errorlevel% neq 0 (
    echo Warning: Some dependencies may have failed to install.
    echo Check the error messages above.
    echo You can try running the setup script again.
)
echo.

echo ================================================
echo Setup complete!
echo ================================================
echo.
echo To run YT Bulk Subtitles Downloader, use start.bat
echo To build ytbsd.exe, use build.bat
echo.
pause
