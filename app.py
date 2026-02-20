#!/usr/bin/env python3
# DaVinciPrint/app.py
# v0.3.0 — Portable da Vinci Print Server (no install required)
#
# A self-contained local web server that provides:
#   - Browser UI for loading STL files
#   - Slicing via CuraEngine (bundled .exe)
#   - GCode → .3w encryption
#   - Direct USB upload to da Vinci printers
#   - Live printer status monitoring
#
# Runs entirely from a folder — no admin, no installer, no Cura.
# Uses only Python stdlib + pyserial + pycryptodome.

import json
import time
import struct
import logging
import threading
import importlib.util
import tempfile
import subprocess
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ---- Bootstrap ----

# Embeddable Python's ._pth file can restrict sys.path.
# Inject our app directory as an absolute path so the XYZDaVinciPlugin
# package (a subdirectory of this app) is always importable.
import os, sys, pathlib
_APP_DIR = str(pathlib.Path(__file__).parent.resolve())

if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

try:
    from XYZDaVinciPlugin.XYZProtocol import (
        XYZProtocol, XYZPrinterStatus, PRINTER_DB, STATE_NAMES
    )
    from XYZDaVinciPlugin.XYZFileConverter import XYZFileConverter
except ModuleNotFoundError:
    # Fallback: load modules directly by file path (bypasses sys.path)
    import importlib.util, types

    _plugin_dir = os.path.join(_APP_DIR, "XYZDaVinciPlugin")

    # Register the parent package first
    _pkg = types.ModuleType("XYZDaVinciPlugin")
    _pkg.__path__ = [_plugin_dir]
    sys.modules["XYZDaVinciPlugin"] = _pkg

    def _force_load(mod_name, filename):
        fpath = os.path.join(_plugin_dir, filename)
        spec = importlib.util.spec_from_file_location(mod_name, fpath)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    _proto_mod = _force_load("XYZDaVinciPlugin.XYZProtocol", "XYZProtocol.py")
    _conv_mod = _force_load("XYZDaVinciPlugin.XYZFileConverter", "XYZFileConverter.py")

    XYZProtocol = _proto_mod.XYZProtocol
    XYZPrinterStatus = _proto_mod.XYZPrinterStatus
    PRINTER_DB = _proto_mod.PRINTER_DB
    STATE_NAMES = _proto_mod.STATE_NAMES
    XYZFileConverter = _conv_mod.XYZFileConverter

from pathlib import Path

APP_DIR = Path(_APP_DIR)
UPLOADS_DIR = APP_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = APP_DIR / "templates"
CURA_ENGINE_DIR = APP_DIR / "cura-engine"

# ---- Logging ----

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("DaVinciPrint")

# ---- Printer Manager (singleton) ----

class PrinterManager:
    """Manages printer connection and operations."""

    def __init__(self):
        self.protocol = XYZProtocol()
        self.connected = False
        self.port = ""
        self.model_name = "Unknown"
        self.model_number = ""
        self.firmware = ""
        self._status = XYZPrinterStatus()
        self._lock = threading.Lock()
        self._monitor_active = False
        self._monitor_thread = None
        self._job_status = {
            "active": False,
            "stage": "",         # "slicing" | "converting" | "uploading" | "done" | "error"
            "progress": 0,
            "message": "",
            "filename": "",
        }

    def scan_ports(self) -> list:
        ports = XYZProtocol.detect_xyz_ports()
        return [{"port": p, "desc": d} for p, d in ports]

    def connect(self, port: str) -> dict:
        with self._lock:
            if self.connected:
                self.protocol.disconnect()

            if self.protocol.connect(port):
                self.connected = True
                self.port = port
                try:
                    status = self.protocol.query_status()
                    self._status = status
                    self.model_number = status.model_number or ""
                    if self.model_number in PRINTER_DB:
                        self.model_name = PRINTER_DB[self.model_number]["name"]
                    else:
                        self.model_name = status.machine_name or "Unknown Model"
                    self.firmware = status.firmware_version or ""
                    logger.info("✅ Connected to %s on %s (FW %s)",
                                self.model_name, port, self.firmware)
                except Exception as e:
                    logger.warning("⚠️ Connected but status query failed: %s", e)
                    self.model_name = "Connected (query failed)"

                self._start_monitor()
                return {"ok": True, "model": self.model_name, "fw": self.firmware,
                        "port": port, "model_number": self.model_number}
            else:
                return {"ok": False, "error": f"Failed to connect to {port}"}

    def disconnect(self):
        with self._lock:
            self._stop_monitor()
            if self.connected:
                self.protocol.disconnect()
                self.connected = False
                self.port = ""
                logger.info("✅ Disconnected")

    def get_status(self) -> dict:
        s = self._status
        state_name = STATE_NAMES.get(s.state, f"Unknown({s.state})")
        return {
            "connected": self.connected,
            "port": self.port,
            "model": self.model_name,
            "model_number": self.model_number,
            "firmware": self.firmware,
            "state": state_name,
            "state_code": s.state,
            "sub_state": s.sub_state,
            "extruder_temp": s.extruder_temp,
            "extruder_target": s.extruder_target,
            "bed_temp": s.bed_temp,
            "print_pct": s.print_pct,
            "print_elapsed_min": s.print_elapsed_min,
            "print_remaining_min": s.print_remaining_min,
            "error_code": s.error_code,
            "filament_remaining_mm": s.filament_remaining_mm,
            "z_offset_mm": s.z_offset / 100.0 if s.z_offset else 0,
            "auto_level": s.auto_level,
            "job": self._job_status,
        }

    def slice_and_print(self, stl_path: str, quality: str = "normal") -> dict:
        """Run the full pipeline: STL → CuraEngine → GCode → .3w → Upload."""
        if not self.connected:
            return {"ok": False, "error": "Printer not connected"}
        if self._job_status["active"]:
            return {"ok": False, "error": "A job is already running"}

        thread = threading.Thread(
            target=self._do_slice_and_print,
            args=(stl_path, quality),
            daemon=True, name="SliceAndPrint"
        )
        thread.start()
        return {"ok": True, "message": "Job started"}

    def _do_slice_and_print(self, stl_path: str, quality: str):
        self._job_status = {
            "active": True, "stage": "slicing", "progress": 0,
            "message": "Slicing STL file...",
            "filename": os.path.basename(stl_path),
        }

        try:
            # Stage 1: Slice
            gcode_path = self._run_cura_engine(stl_path, quality)
            if not gcode_path:
                self._job_status.update(stage="error", active=False,
                    message="Slicing failed. Check that CuraEngine is in the cura-engine/ folder.")
                return

            with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
                gcode = f.read()

            logger.info("✅ Sliced: %d lines of GCode", gcode.count("\n"))

            # Stage 2: Convert
            self._job_status.update(stage="converting", progress=30,
                message="Converting GCode to .3w format...")

            info = XYZFileConverter.extract_print_info(gcode)
            model = self.model_number or "dv1MX0A000"
            converter = XYZFileConverter(model_number=model)
            three_w = converter.convert_gcode_to_3w(
                gcode, info["print_time_sec"], info["filament_mm"]
            )
            logger.info("✅ Converted: %d bytes .3w, time=%ds, filament=%.0fmm",
                         len(three_w), info["print_time_sec"], info["filament_mm"])

            # Stage 3: Upload
            self._job_status.update(stage="uploading", progress=40,
                message="Uploading to printer...")

            def on_progress(pct):
                # Scale 0-100 upload pct to 40-95 overall
                overall = 40 + int(pct * 0.55)
                self._job_status.update(progress=overall,
                    message=f"Uploading to printer... {pct:.0f}%")

            filename = os.path.basename(stl_path).replace(".stl", ".gcode")
            with self._lock:
                success = self.protocol.upload_file(filename, three_w,
                                                     progress_callback=on_progress)

            if success:
                est_min = info["print_time_sec"] // 60
                self._job_status.update(
                    stage="done", progress=100, active=False,
                    message=f"Print started! Estimated time: {est_min} minutes"
                )
                logger.info("✅ Print job sent successfully!")
            else:
                self._job_status.update(
                    stage="error", active=False,
                    message="Upload failed. Check USB connection and printer state."
                )

        except Exception as e:
            logger.error("❌ Job failed: %s", e, exc_info=True)
            self._job_status.update(
                stage="error", active=False,
                message=f"Error: {e}"
            )

        finally:
            # Clean up temp files
            try:
                if 'gcode_path' in dir() and gcode_path and os.path.exists(gcode_path):
                    os.remove(gcode_path)
            except:
                pass

    def _run_cura_engine(self, stl_path: str, quality: str) -> str:
        """Run CuraEngine to slice an STL file. Returns path to output .gcode or None."""

        # Find CuraEngine executable
        engine_exe = None
        for name in ["CuraEngine.exe", "CuraEngine", "curaengine.exe", "curaengine"]:
            candidate = CURA_ENGINE_DIR / name
            if candidate.exists():
                engine_exe = str(candidate)
                break

        if not engine_exe:
            # Try PATH
            engine_exe = shutil.which("CuraEngine") or shutil.which("curaengine")

        if not engine_exe:
            logger.error("❌ CuraEngine not found in cura-engine/ folder or PATH")
            return None

        # Find definition file
        def_file = CURA_ENGINE_DIR / "definitions" / "fdmprinter.def.json"
        if not def_file.exists():
            # Try to find it in common Cura install locations
            for base in [
                Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "UltiMaker Cura",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "UltiMaker Cura",
            ]:
                if base.exists():
                    for sub in sorted(base.iterdir(), reverse=True):
                        candidate = sub / "share" / "cura" / "resources" / "definitions" / "fdmprinter.def.json"
                        if candidate.exists():
                            def_file = candidate
                            break

        if not def_file.exists():
            logger.error("❌ fdmprinter.def.json not found. Place in cura-engine/definitions/")
            return None

        # Quality profiles → slicer settings
        quality_settings = {
            "fine":   {"layer_height": "0.1", "speed_print": "25", "infill_sparse_density": "20"},
            "normal": {"layer_height": "0.2", "speed_print": "30", "infill_sparse_density": "20"},
            "draft":  {"layer_height": "0.3", "speed_print": "40", "infill_sparse_density": "15"},
        }
        settings = quality_settings.get(quality, quality_settings["normal"])

        # miniMaker-specific settings
        machine_settings = {
            "machine_width": "150",
            "machine_depth": "150",
            "machine_height": "150",
            "machine_heated_bed": "false",
            "machine_nozzle_size": "0.4",
            "material_diameter": "1.75",
            "material_print_temperature": "210",
            "retraction_enable": "true",
            "retraction_amount": "4.5",
            "retraction_speed": "25",
            "speed_travel": "60",
            "adhesion_type": "skirt",
            "support_enable": "false",
        }

        # Build output path
        output_gcode = str(UPLOADS_DIR / (Path(stl_path).stem + ".gcode"))

        # Build command
        cmd = [engine_exe, "slice", "-v", "-j", str(def_file)]

        # Add all settings
        all_settings = {**machine_settings, **settings}
        for key, val in all_settings.items():
            cmd.extend(["-s", f"{key}={val}"])

        cmd.extend(["-o", output_gcode, "-l", stl_path])

        logger.info("🔧 Running CuraEngine: %s", " ".join(cmd[:6]) + " ...")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=str(CURA_ENGINE_DIR)
            )
            if result.returncode != 0:
                logger.error("❌ CuraEngine failed (code %d):\n%s",
                             result.returncode, result.stderr[-500:] if result.stderr else "no output")
                return None
            if not os.path.exists(output_gcode):
                logger.error("❌ CuraEngine ran but no output file created")
                return None

            # Log some stats
            size = os.path.getsize(output_gcode)
            logger.info("✅ CuraEngine output: %s (%d bytes)", output_gcode, size)
            return output_gcode

        except subprocess.TimeoutExpired:
            logger.error("❌ CuraEngine timed out after 120s")
            return None
        except FileNotFoundError:
            logger.error("❌ CuraEngine executable not found: %s", engine_exe)
            return None

    def send_gcode_file(self, gcode_path: str) -> dict:
        """Send a pre-sliced .gcode file (skip CuraEngine)."""
        if not self.connected:
            return {"ok": False, "error": "Printer not connected"}
        if self._job_status["active"]:
            return {"ok": False, "error": "A job is already running"}

        thread = threading.Thread(
            target=self._do_send_gcode, args=(gcode_path,),
            daemon=True, name="SendGcode"
        )
        thread.start()
        return {"ok": True, "message": "GCode upload started"}

    def _do_send_gcode(self, gcode_path: str):
        self._job_status = {
            "active": True, "stage": "converting", "progress": 10,
            "message": "Converting GCode to .3w...",
            "filename": os.path.basename(gcode_path),
        }
        try:
            with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
                gcode = f.read()

            info = XYZFileConverter.extract_print_info(gcode)
            model = self.model_number or "dv1MX0A000"
            converter = XYZFileConverter(model_number=model)
            three_w = converter.convert_gcode_to_3w(
                gcode, info["print_time_sec"], info["filament_mm"]
            )

            self._job_status.update(stage="uploading", progress=30,
                message="Uploading to printer...")

            def on_progress(pct):
                overall = 30 + int(pct * 0.65)
                self._job_status.update(progress=overall,
                    message=f"Uploading... {pct:.0f}%")

            filename = os.path.basename(gcode_path)
            with self._lock:
                success = self.protocol.upload_file(filename, three_w,
                                                     progress_callback=on_progress)

            if success:
                est_min = info["print_time_sec"] // 60
                self._job_status.update(stage="done", progress=100, active=False,
                    message=f"Print started! Est. {est_min} min")
            else:
                self._job_status.update(stage="error", active=False,
                    message="Upload failed.")
        except Exception as e:
            self._job_status.update(stage="error", active=False,
                message=f"Error: {e}")

    # ---- Printer Control ----

    def home(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.home()
        return {"ok": ok}

    def load_filament(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.load_filament_start()
        return {"ok": ok, "message": "Filament load started — feed filament when nozzle is hot"}

    def unload_filament(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.unload_filament_start()
        return {"ok": ok, "message": "Filament unload started — wait for retraction"}

    def cancel_print(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.cancel_print()
        return {"ok": ok}

    def pause_print(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.pause_print()
        return {"ok": ok}

    def resume_print(self) -> dict:
        if not self.connected: return {"ok": False, "error": "Not connected"}
        with self._lock:
            ok = self.protocol.resume_print()
        return {"ok": ok}

    # ---- Monitor ----

    def _start_monitor(self):
        self._monitor_active = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="StatusMonitor"
        )
        self._monitor_thread.start()

    def _stop_monitor(self):
        self._monitor_active = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None

    def _monitor_loop(self):
        while self._monitor_active:
            try:
                if self.connected and not self._job_status.get("stage") == "uploading":
                    with self._lock:
                        self._status = self.protocol.query_status()
            except Exception as e:
                logger.debug("Monitor error: %s", e)
            time.sleep(4)


# ---- Global printer manager ----

printer = PrinterManager()


# ---- HTTP Handler ----

class DaVinciHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler — serves the UI and API endpoints."""

    def log_message(self, fmt, *args):
        # Suppress default access logging clutter
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath: str, content_type: str = "application/octet-stream"):
        if not os.path.exists(filepath):
            self.send_error(404)
            return
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            html_path = TEMPLATES_DIR / "index.html"
            if html_path.exists():
                self._send_file(str(html_path), "text/html; charset=utf-8")
            else:
                self._send_html("<h1>DaVinciPrint</h1><p>templates/index.html not found</p>")

        elif path == "/api/status":
            self._send_json(printer.get_status())

        elif path == "/api/ports":
            self._send_json(printer.scan_ports())

        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            filepath = APP_DIR / "static" / rel
            # Determine content type
            ct = "application/octet-stream"
            if rel.endswith(".css"): ct = "text/css"
            elif rel.endswith(".js"): ct = "application/javascript"
            elif rel.endswith(".png"): ct = "image/png"
            elif rel.endswith(".svg"): ct = "image/svg+xml"
            self._send_file(str(filepath), ct)

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/connect":
            body = json.loads(self._read_body())
            result = printer.connect(body.get("port", ""))
            self._send_json(result)

        elif path == "/api/disconnect":
            printer.disconnect()
            self._send_json({"ok": True})

        elif path == "/api/upload_stl":
            self._handle_stl_upload()

        elif path == "/api/upload_gcode":
            self._handle_gcode_upload()

        elif path == "/api/print":
            body = json.loads(self._read_body())
            stl_path = body.get("stl_path", "")
            quality = body.get("quality", "normal")
            result = printer.slice_and_print(stl_path, quality)
            self._send_json(result)

        elif path == "/api/send_gcode":
            body = json.loads(self._read_body())
            gcode_path = body.get("gcode_path", "")
            result = printer.send_gcode_file(gcode_path)
            self._send_json(result)

        elif path == "/api/home":
            self._send_json(printer.home())
        elif path == "/api/load_filament":
            self._send_json(printer.load_filament())
        elif path == "/api/unload_filament":
            self._send_json(printer.unload_filament())
        elif path == "/api/cancel":
            self._send_json(printer.cancel_print())
        elif path == "/api/pause":
            self._send_json(printer.pause_print())
        elif path == "/api/resume":
            self._send_json(printer.resume_print())

        else:
            self.send_error(404)

    def _handle_stl_upload(self):
        """Handle multipart file upload for STL files."""
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" in content_type:
            # Parse boundary
            boundary = content_type.split("boundary=")[1].strip()
            body = self._read_body()
            # Simple multipart parser — find the file data
            parts = body.split(f"--{boundary}".encode())
            for part in parts:
                if b"filename=" in part:
                    # Extract filename
                    header_end = part.find(b"\r\n\r\n")
                    if header_end == -1:
                        continue
                    header_text = part[:header_end].decode("utf-8", errors="replace")
                    file_data = part[header_end + 4:]
                    # Strip trailing \r\n
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]

                    # Extract filename from header
                    fn_match = None
                    for line in header_text.split("\r\n"):
                        if 'filename="' in line:
                            start = line.index('filename="') + 10
                            end = line.index('"', start)
                            fn_match = line[start:end]

                    if fn_match:
                        # Save file
                        safe_name = os.path.basename(fn_match)
                        save_path = str(UPLOADS_DIR / safe_name)
                        with open(save_path, "wb") as f:
                            f.write(file_data)
                        logger.info("✅ Uploaded: %s (%d bytes)", safe_name, len(file_data))
                        self._send_json({"ok": True, "path": save_path, "name": safe_name,
                                         "size": len(file_data)})
                        return

            self._send_json({"ok": False, "error": "No file found in upload"}, 400)
        else:
            # Raw body upload with filename in query string
            params = parse_qs(urlparse(self.path).query)
            filename = params.get("filename", ["upload.stl"])[0]
            data = self._read_body()
            save_path = str(UPLOADS_DIR / os.path.basename(filename))
            with open(save_path, "wb") as f:
                f.write(data)
            self._send_json({"ok": True, "path": save_path, "name": filename,
                             "size": len(data)})

    def _handle_gcode_upload(self):
        """Handle .gcode file upload."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            boundary = content_type.split("boundary=")[1].strip()
            body = self._read_body()
            parts = body.split(f"--{boundary}".encode())
            for part in parts:
                if b"filename=" in part:
                    header_end = part.find(b"\r\n\r\n")
                    if header_end == -1: continue
                    header_text = part[:header_end].decode("utf-8", errors="replace")
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    fn_match = None
                    for line in header_text.split("\r\n"):
                        if 'filename="' in line:
                            start = line.index('filename="') + 10
                            end = line.index('"', start)
                            fn_match = line[start:end]
                    if fn_match:
                        safe_name = os.path.basename(fn_match)
                        save_path = str(UPLOADS_DIR / safe_name)
                        with open(save_path, "wb") as f:
                            f.write(file_data)
                        self._send_json({"ok": True, "path": save_path, "name": safe_name})
                        return
            self._send_json({"ok": False, "error": "No file found"}, 400)
        else:
            self._send_json({"ok": False, "error": "Expected multipart upload"}, 400)


# ---- Main ----

def main():
    port = 8080
    # Check for port argument
    for arg in sys.argv[1:]:
        if arg.isdigit():
            port = int(arg)

    server = HTTPServer(("127.0.0.1", port), DaVinciHandler)

    print()
    print("  ╔═══════════════════════════════════════════════════╗")
    print("  ║   DaVinciPrint — Portable 3D Printer Interface   ║")
    print("  ║   v0.3.0 — No install required                   ║")
    print("  ╚═══════════════════════════════════════════════════╝")
    print()
    print(f"  🌐 Server running at: http://127.0.0.1:{port}")
    print(f"  📂 App directory: {APP_DIR}")
    print(f"  🔧 CuraEngine dir: {CURA_ENGINE_DIR}")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    print(f"  👉 Open this URL manually if your browser does not launch: http://127.0.0.1:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        printer.disconnect()
        server.shutdown()


if __name__ == "__main__":
    main()
