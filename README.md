# DaVinciPrint — Portable 3D Printer Interface
## v0.3.0 — No installer, no admin, no Cura required

A fully self-contained web-based interface for XYZ da Vinci printers.
Runs from a folder on any Windows PC — no installation needed.

## Quick Start

### One-Time Setup (5 minutes)

1. **Get Portable Python** (no admin needed):
   - Go to https://www.python.org/downloads/windows/
   - Click the latest Python 3.12+ release
   - Download **"Windows embeddable package (64-bit)"** (a .zip file, ~11MB)
   - Extract the .zip contents into the `python\` subfolder
   - You should see `python\python.exe` after extraction

2. **Run Setup**:
   - Double-click `SETUP.bat`
   - This configures pip and installs pyserial + pycryptodome
   - Requires internet access (one time only)

3. **(Optional) Add CuraEngine** for STL slicing:
   - If you have Cura installed anywhere (home PC, etc.), copy:
     - `CuraEngine.exe` → `cura-engine\`
     - `share\cura\resources\definitions\fdmprinter.def.json` → `cura-engine\definitions\`
   - Without CuraEngine, you can still send pre-sliced `.gcode` files

### Daily Use

1. Plug in da Vinci printer via USB
2. Double-click `START.bat`
3. Browser opens automatically to http://localhost:8080
4. Click **Scan for Printers** → select your printer
5. Drop an `.stl` or `.gcode` file → pick quality → **Slice & Print**

## Folder Structure

```
DaVinciPrint/
  START.bat               ← Double-click to launch
  SETUP.bat               ← One-time setup (installs pip + packages)
  app.py                  ← Local web server (Python)
  python/                 ← Portable Python (you extract here)
  templates/
    index.html            ← Browser interface
  cura-engine/            ← (Optional) CuraEngine + definitions
    CuraEngine.exe
    definitions/
      fdmprinter.def.json
  XYZDaVinciPlugin/       ← Protocol + encryption modules
    XYZProtocol.py        ← V3 serial protocol (USB communication)
    XYZFileConverter.py   ← GCode → .3w encryption
  uploads/                ← Temporary uploaded files
  wheels/                 ← (Optional) Offline .whl packages
```

## Two Workflows

### Workflow A: STL File (full pipeline)
```
STL → CuraEngine slices → GCode → encrypt to .3w → upload to printer
```
Requires CuraEngine.exe in cura-engine/ folder.

### Workflow B: Pre-sliced GCode (no CuraEngine needed)
```
GCode → encrypt to .3w → upload to printer
```
Slice your file in any slicer (Cura at home, PrusaSlicer, etc.),
save the .gcode, bring it on a USB stick, and upload through the UI.
**This workflow works even without CuraEngine installed.**

## API Endpoints

The web UI talks to these local endpoints (useful for scripting):

| Endpoint            | Method | Description                          |
|---------------------|--------|--------------------------------------|
| `/api/ports`        | GET    | List serial ports                    |
| `/api/status`       | GET    | Current printer status               |
| `/api/connect`      | POST   | Connect to port `{"port":"COM3"}`    |
| `/api/disconnect`   | POST   | Disconnect printer                   |
| `/api/upload_stl`   | POST   | Upload .stl file (multipart)         |
| `/api/upload_gcode` | POST   | Upload .gcode file (multipart)       |
| `/api/print`        | POST   | Slice & print `{"stl_path":"...","quality":"normal"}` |
| `/api/send_gcode`   | POST   | Convert & print `{"gcode_path":"..."}` |
| `/api/home`         | POST   | Home all axes                        |
| `/api/load_filament`| POST   | Start filament load                  |
| `/api/unload_filament`| POST | Start filament unload                |
| `/api/pause`        | POST   | Pause print                          |
| `/api/resume`       | POST   | Resume print                         |
| `/api/cancel`       | POST   | Cancel print                         |

## Troubleshooting

### "No serial ports found"
- Is the printer plugged in via USB and powered on?
- Open PowerShell: `[System.IO.Ports.SerialPort]::GetPortNames()`
- If no COM port appears, the USB driver may not be loaded.
  Ask IT to ensure the CDC serial driver (usbser.sys) is enabled.

### "CuraEngine not found"
- STL slicing requires CuraEngine.exe in the cura-engine/ folder
- Alternative: slice at home, bring the .gcode file on a USB stick

### "Failed to connect"
- Only one program can use a COM port at a time
- Close XYZware or any other serial monitor
- Try unplugging and replugging the USB cable

### "Upload failed"
- Printer must be idle (state 9511) to accept uploads
- Check that the printer isn't in an error state
- Try: Home → wait → then upload again

### Offline Setup (no internet on work PC)
On a PC WITH internet, download these .whl files:
- pyserial: https://pypi.org/project/pyserial/#files
- pycryptodome: https://pypi.org/project/pycryptodome/#files
  (get the `cp312-cp312-win_amd64.whl` matching your Python version)
Copy them to the `wheels/` folder, then SETUP.bat will find them.

## Supported Printers

- da Vinci miniMaker (primary target, USB only)
- da Vinci mini w / wA / w+ (USB mode)
- da Vinci nano / nano w (USB mode)
- da Vinci Jr. 1.0 / Jr. 1.0 W / Jr. 1.0 A (USB mode)
- da Vinci Jr. 2.0 Mix (USB mode)
- da Vinci Pro series (USB mode)

## Technical Notes

- All traffic is local: the server runs on 127.0.0.1 only
- No data is sent to the internet
- The printer uses XYZ's V3 serial protocol at 115200 baud
- GCode is encrypted with AES (keys baked into printer firmware)
- Files are uploaded in 8KB blocks with per-block acknowledgment
