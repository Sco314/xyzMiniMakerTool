@echo off
title DaVinciPrint - Portable 3D Printer Interface
echo.
echo   ╔═══════════════════════════════════════════════════╗
echo   ║   DaVinciPrint — Portable 3D Printer Interface   ║
echo   ╚═══════════════════════════════════════════════════╝
echo.

cd /d "%~dp0"

:: --- Set PYTHONPATH so embeddable Python finds XYZDaVinciPlugin ---
set "PYTHONPATH=%~dp0"

:: --- Ensure site-packages .pth file points to app root ---
:: Embeddable Python ignores sys.path changes; .pth files are the reliable way.
if exist "python\python.exe" (
    if not exist "python\Lib\site-packages" mkdir "python\Lib\site-packages"
    echo %~dp0> "python\Lib\site-packages\davinci.pth"
)

:: --- Try portable python first ---
if exist "python\python.exe" (
    echo   Using portable Python: python\python.exe
    "python\python.exe" app.py %*
    goto :end
)

:: --- Try system python ---
where python >nul 2>nul
if %errorlevel% equ 0 (
    echo   Using system Python
    python app.py %*
    goto :end
)

where python3 >nul 2>nul
if %errorlevel% equ 0 (
    echo   Using system Python3
    python3 app.py %*
    goto :end
)

:: --- No python found ---
echo.
echo   ERROR: Python not found!
echo.
echo   Option A: Install portable Python (no admin needed):
echo     1. Download "Windows embeddable package (64-bit)" from:
echo        https://www.python.org/downloads/windows/
echo     2. Extract the .zip into a "python" subfolder here
echo     3. Run SETUP.bat to configure it
echo.
echo   Option B: Use system Python if available at a custom path:
echo     Set PYTHON_PATH=C:\path\to\python.exe before running this script
echo.

:end
pause
