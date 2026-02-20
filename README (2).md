# DaVinciPrint — Portable 3D Printer Interface
## v0.3.1 — No installer, no admin, no Cura required

A fully self-contained web-based interface for XYZ da Vinci printers.
Runs from a folder on any Windows PC — no installation needed.

Tested on: Windows 11 Enterprise 24H2, Intel i5-14500, no admin rights.

## Quick Start

### One-Time Setup (5 minutes)

1. **Copy the DaVinciPrint folder** to your Documents:
   ```
   C:\Users\YourName\Documents\DaVinciPrint\
   ```

2. **Get Portable Python** (no admin needed):
   - Go to https://www.python.org/downloads/windows/
   - Under "Python Releases for Windows", click the latest 3.13.x release
   - Scroll to "Files" and download **"Windows embeddable package (64-bit)"**
     (a `.zip` file, ~11MB — NOT the installer)
   - Extract the `.zip` contents into the `DaVinciPrint\python\` subfolder
   - Verify: you should see `DaVinciPrint\python\python.exe`
   - Quick test: double-click `python\python.exe` — you should see:
     ```
     Python 3.13.x ... on win32
     Type "help" ...
     >>>
     ```
     Type `exit()` to close.

3. **Run Setup**:
   - Double-click `SETUP.bat`
   - This does four things automatically:
     1. Detects portable Python
     2. Configures the `._pth` file for pip and local imports
     3. Creates a `.pth` path file pointing to the app root
     4. Downloads and installs pip, pyserial, and pycryptodome
   - Requires internet access (one time only)

4. **(Optional) Add CuraEngine** for STL slicing:
   - If you have Cura installed anywhere (home PC, etc.), copy these two files:
     - `CuraEngine.exe` → `DaVinciPrint\cura-engine\`
     - `fdmprinter.def.json` → `DaVinciPrint\cura-engine\definitions\`
   - Where to find them in a Cura install:
     - `C:\Program Files\UltiMaker Cura 5.x\CuraEngine.exe`
     - `C:\Program Files\UltiMaker Cura 5.x\share\cura\resources\definitions\fdmprinter.def.json`
   - Without CuraEngine you can still send pre-sliced `.gcode` files

### Daily Use

1. Plug in da Vinci printer via USB cable
2. Double-click `START.bat`
3. Browser opens automatically to http://localhost:8080
4. Click **Scan for Printers** → click your printer (e.g. COM3)
5. Drop a `.stl` or `.gcode` file → pick quality → click **Slice & Print**

## Folder Structure

```
DaVinciPrint/
  START.bat               ← Double-click to launch (daily)
  SETUP.bat               ← One-time setup (installs pip + packages)
  app.py                  ← Local web server
  README.md               ← This file
  python/                 ← Portable Python (you extract here)
    python.exe
    python313._pth        ← Path config (SETUP.bat modifies this)
    Lib/site-packages/    ← Packages installed here by pip
      davinci.pth         ← Points Python back to app root
  templates/
    index.html            ← Browser UI (3-panel: connect | print | status)
  cura-engine/            ← (Optional) CuraEngine + definitions
    CuraEngine.exe
    definitions/
      fdmprinter.def.json
  XYZDaVinciPlugin/       ← Protocol + encryption modules
    XYZProtocol.py        ← V3 serial protocol (USB communication)
    XYZFileConverter.py   ← GCode → .3w AES encryption
    __init__.py
  uploads/                ← Temporary uploaded files (auto-created)
  wheels/                 ← (Optional) Offline .whl packages
```

## Two Workflows

### Workflow A: STL File (full pipeline, requires CuraEngine)
```
Drop STL → CuraEngine slices → GCode → encrypt to .3w → upload via USB
```

### Workflow B: Pre-sliced GCode (works immediately, no CuraEngine)
```
Drop .gcode → encrypt to .3w → upload via USB
```
Slice at home in Cura, PrusaSlicer, or any slicer. Save the `.gcode` file,
bring it on a USB stick, and drop it in the browser UI.
**This is the recommended workflow if you can't install CuraEngine on the work PC.**

## Browser UI

The interface has three panels:

- **Left — Connection & Controls**: Scan ports, connect/disconnect, home axes,
  load/unload filament, pause/resume/cancel print
- **Center — Print Job**: Drag-drop file upload area, quality picker
  (fine 0.1mm / normal 0.2mm / draft 0.3mm), slice & print button,
  job progress bar
- **Right — Live Status**: Printer state, extruder temperature (live),
  print progress ring with % / elapsed / remaining, error codes,
  filament remaining

## API Endpoints

The web UI talks to these local endpoints (also useful for scripting):

| Endpoint              | Method | Description                                          |
|-----------------------|--------|------------------------------------------------------|
| `/api/ports`          | GET    | List serial ports                                    |
| `/api/status`         | GET    | Current printer status (polled every 2s by UI)       |
| `/api/connect`        | POST   | Connect to port `{"port":"COM3"}`                    |
| `/api/disconnect`     | POST   | Disconnect printer                                   |
| `/api/upload_stl`     | POST   | Upload .stl file (multipart form)                    |
| `/api/upload_gcode`   | POST   | Upload .gcode file (multipart form)                  |
| `/api/print`          | POST   | Slice STL & print `{"stl_path":"...","quality":"normal"}` |
| `/api/send_gcode`     | POST   | Convert GCode & print `{"gcode_path":"..."}`         |
| `/api/home`           | POST   | Home all axes                                        |
| `/api/load_filament`  | POST   | Start filament load (heats nozzle first)             |
| `/api/unload_filament`| POST   | Start filament unload                                |
| `/api/pause`          | POST   | Pause current print                                  |
| `/api/resume`         | POST   | Resume paused print                                  |
| `/api/cancel`         | POST   | Cancel current print                                 |

## Troubleshooting

### "ModuleNotFoundError: No module named 'XYZDaVinciPlugin'"
This means embeddable Python can't find the app modules. Fix:
1. Open PowerShell and navigate to your DaVinciPrint folder:
   ```powershell
   cd C:\Users\YourName\Documents\DaVinciPrint
   ```
2. Verify the `._pth` file has `import site` uncommented and `.` added:
   ```powershell
   Get-Content python\python*._pth
   ```
   Should show:
   ```
   python313.zip
   .
   import site
   ```
   If `import site` still has a `#` prefix, or `.` is missing, run:
   ```powershell
   $pth = Get-ChildItem python\python*._pth
   $c = Get-Content $pth
   $c = $c -replace '#import site','import site'
   if ($c -notcontains '.') { $c += '.' }
   $c | Set-Content $pth
   ```
3. Verify the `.pth` file exists:
   ```powershell
   Get-Content python\Lib\site-packages\davinci.pth
   ```
   Should show your DaVinciPrint path. If missing, create it:
   ```powershell
   mkdir -Force python\Lib\site-packages
   (Get-Item .).FullName | Set-Content python\Lib\site-packages\davinci.pth
   ```
4. Try `START.bat` again. The latest `app.py` has a fallback that loads
   modules by absolute file path if all else fails.

### "No serial ports found"
- Is the printer plugged in via USB and powered on?
- Verify in PowerShell:
  ```powershell
  [System.IO.Ports.SerialPort]::GetPortNames()
  ```
  You should see `COM3` (or similar) appear when the printer is plugged in.
- If no COM port appears, the USB CDC serial driver may not be loaded.
  Ask IT to ensure `usbser.sys` is enabled, or try a different USB port.

### "CuraEngine not found"
- STL slicing requires `CuraEngine.exe` in the `cura-engine\` folder
- Alternative: slice at home, bring the `.gcode` file on a USB stick
- This is only needed for Workflow A (STL files)

### "Failed to connect"
- Only one program can use a COM port at a time
- Close XYZware, Device Manager serial monitors, or any other serial tool
- Try unplugging and replugging the USB cable
- Try a different USB port on the PC

### "Upload failed"
- Printer must be idle (state 9511) to accept uploads
- Check that the printer isn't in an error state (red error code in UI)
- Try: click Home → wait for idle → then upload again

### Garbled box-drawing characters in START.bat window
This is cosmetic only — Windows CMD uses code page 437 by default and can't
render the UTF-8 box characters. It doesn't affect functionality. The server
still starts and the browser UI works normally.

### Offline Setup (no internet on work PC)
On a PC WITH internet access, download these `.whl` files:
- pyserial: https://pypi.org/project/pyserial/#files
  (download `pyserial-3.5-py2.py3-none-any.whl`)
- pycryptodome: https://pypi.org/project/pycryptodome/#files
  (download `pycryptodome-...-cp313-cp313-win_amd64.whl` matching your Python version)

Copy both `.whl` files to the `wheels\` folder. Then `SETUP.bat` will
install from there instead of downloading.

## Supported Printers

| Printer              | Connection | Status   |
|----------------------|------------|----------|
| da Vinci miniMaker   | USB        | Primary  |
| da Vinci mini w/wA/w+| USB        | Supported|
| da Vinci nano / nano w| USB       | Supported|
| da Vinci Jr. 1.0 / W / A | USB   | Supported|
| da Vinci Jr. 2.0 Mix | USB       | Supported|
| da Vinci Pro series  | USB        | Supported|

## Technical Notes

- All traffic is localhost only (`127.0.0.1:8080`) — nothing sent to the internet
- The printer uses XYZ's V3 serial protocol at 115200 baud
- GCode is encrypted with AES-128/256 (keys baked into printer firmware)
- `.3w` files are uploaded in 8KB blocks with per-block acknowledgment
- The app creates `python\sitecustomize.py` on first run (path helper)
- Dependencies: Python 3.8+ (tested 3.13.12), pyserial, pycryptodome
- No admin rights required for any part of setup or operation

## Version History

- **v0.3.1** — Fixed embeddable Python module loading (._pth, .pth, importlib fallback)
- **v0.3.0** — Initial portable web app (app.py + browser UI + START/SETUP.bat)
- **v0.2.0** — Phase 2: printer control panel (filament, calibrate, jog, z-offset)
- **v0.1.0** — Phase 1: Cura plugin with USB print pipeline
