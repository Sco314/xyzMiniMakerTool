@echo off
title DaVinciPrint - Setup
echo.
echo   ╔═══════════════════════════════════════════════════╗
echo   ║   DaVinciPrint Setup (one-time)                  ║
echo   ╚═══════════════════════════════════════════════════╝
echo.

cd /d "%~dp0"

:: --- Check for portable python ---
if not exist "python\python.exe" (
    echo   [!] python\python.exe not found.
    echo.
    echo   Download "Windows embeddable package (64-bit)" from:
    echo     https://www.python.org/downloads/windows/
    echo.
    echo   Extract the .zip contents into the "python" subfolder.
    echo   You should see python\python.exe after extraction.
    echo.
    if not exist "python" mkdir python
    echo   Created empty "python" folder — extract files there.
    pause
    exit /b 1
)

echo   [1/4] Found portable Python
"python\python.exe" --version

:: --- Enable pip: uncomment 'import site' and add '.' for local imports ---
echo   [2/4] Configuring Python for pip and local imports...
for %%f in (python\python*._pth) do (
    powershell -Command "$c = Get-Content '%%f'; $c = $c -replace '#import site','import site'; if ($c -notcontains '.') { $c += '.' }; $c | Set-Content '%%f'"
    echo     Updated %%f
)

:: --- Create a .pth file that points back to the app root ---
:: This is the reliable way to add the app directory to embeddable Python's path.
if not exist "python\Lib\site-packages" mkdir "python\Lib\site-packages"
echo %~dp0> "python\Lib\site-packages\davinci.pth"
echo     Created davinci.pth pointing to app root

:: --- Install pip ---
echo   [3/4] Installing pip...
if not exist "python\get-pip.py" (
    echo     Downloading get-pip.py...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'python\get-pip.py'"
)
"python\python.exe" "python\get-pip.py" --no-warn-script-location 2>nul
if %errorlevel% neq 0 (
    echo     [!] pip install failed. You may need internet access.
    echo     Alternatively, download .whl files manually (see README).
    pause
    exit /b 1
)

:: --- Install dependencies ---
echo   [4/4] Installing dependencies (pyserial, pycryptodome)...
"python\python.exe" -m pip install pyserial pycryptodome --no-warn-script-location 2>nul
if %errorlevel% neq 0 (
    echo     [!] Package install failed. Trying offline...
    if exist "wheels" (
        "python\python.exe" -m pip install --no-index --find-links wheels pyserial pycryptodome
    ) else (
        echo     No offline wheels found either. Check internet or add .whl files to wheels\ folder.
        pause
        exit /b 1
    )
)

echo.
echo   ╔═══════════════════════════════════════════════════╗
echo   ║   Setup complete! Run START.bat to launch.       ║
echo   ╚═══════════════════════════════════════════════════╝
echo.
echo   Next steps:
echo     1. (Optional) Place CuraEngine.exe in cura-engine\ folder
echo        for STL slicing support. Without it, you can still
echo        send pre-sliced .gcode files.
echo.
echo     2. Double-click START.bat to launch DaVinciPrint.
echo.
pause
