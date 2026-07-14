@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   PlantNet2Anki - Installation (Windows)
echo ========================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    py --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found.
        echo Please install Python from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

for /f "tokens=*" %%i in ('!PYTHON! --version 2^>^&1') do set PYVER=%%i
echo [OK] Found %PYVER%

:: Check Python version >= 3.8
for /f "tokens=2 delims= " %%v in ('!PYTHON! --version 2^>^&1') do set PYVER_NUM=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER_NUM%") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)
if %PY_MAJOR% LSS 3 (
    echo [ERROR] Python 3.8 or higher is required. Found: %PYVER_NUM%
    pause
    exit /b 1
)
if %PY_MAJOR% EQU 3 if %PY_MINOR% LSS 8 (
    echo [ERROR] Python 3.8 or higher is required. Found: %PYVER_NUM%
    pause
    exit /b 1
)

echo.
echo Installing dependencies...
echo.

!PYTHON! -m pip install --upgrade pip
if errorlevel 1 (
    echo [WARNING] Could not upgrade pip, continuing anyway...
)

!PYTHON! -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. Try running as administrator,
    echo or add --user flag: pip install --user -r requirements.txt
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo To launch the app, run:
echo   %PYTHON% plantnet2anki_gui.py
echo.
echo Or simply double-click: launch.bat
echo.
pause
