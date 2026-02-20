#!/usr/bin/env python3
# XYZDaVinciPlugin/XYZProtocol.py
# V3 serial protocol for XYZ da Vinci printers
#
# Based on reverse engineering documented in the miniMover project
# https://github.com/reality-boy/miniMover
#
# Protocol: text commands over 115200 baud USB serial
# Commands: XYZv3/query=, XYZv3/config=, XYZv3/action=, XYZv3/upload=
# Responses terminate with '$'

import struct
import time
import logging
import re

logger = logging.getLogger("XYZProtocol")

# ---- Printer Database ----
# Model number -> printer info (from miniMover's printer list)

PRINTER_DB = {
    "dv1MX0A000": {"name": "da Vinci miniMaker",      "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1MW0A000": {"name": "da Vinci mini w",          "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": True},
    "dv1MW0B000": {"name": "da Vinci mini wA",         "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": True},
    "dv1MW0C000": {"name": "da Vinci mini w+",         "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": True},
    "dv1NX0A000": {"name": "da Vinci nano",            "width": 120, "depth": 120, "height": 120, "heated_bed": False, "wifi": False},
    "dv1NW0A000": {"name": "da Vinci nano w",          "width": 120, "depth": 120, "height": 120, "heated_bed": False, "wifi": True},
    "dv1JP0A000": {"name": "da Vinci Jr. 1.0",         "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1JW0A000": {"name": "da Vinci Jr. 1.0W",        "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": True},
    "dv1JA0A000": {"name": "da Vinci Jr. 1.0A",        "width": 175, "depth": 175, "height": 175, "heated_bed": False, "wifi": False},
    "dv1JS0A000": {"name": "da Vinci Jr. 1.0 3in1",    "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1JO0A000": {"name": "da Vinci Jr. 1.0 3in1 (Open)", "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1JPWA000": {"name": "da Vinci Jr. 1.0 Pro",     "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1JWWA000": {"name": "da Vinci Jr. 1.0W Pro",    "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": True},
    "dv2JX0A000": {"name": "da Vinci Jr. 2.0 Mix",     "width": 150, "depth": 150, "height": 150, "heated_bed": False, "wifi": False},
    "dv1PA0A000": {"name": "da Vinci 1.0 Pro",         "width": 200, "depth": 200, "height": 200, "heated_bed": True,  "wifi": False},
    "dv1PS0A000": {"name": "da Vinci 1.0 Pro 3in1",    "width": 200, "depth": 200, "height": 200, "heated_bed": True,  "wifi": False},
    "dv1SA0A000": {"name": "da Vinci 1.0 Super",       "width": 300, "depth": 300, "height": 300, "heated_bed": True,  "wifi": False},
}

# ---- State Codes ----
# From miniMover v3 protocol notes

STATE_NAMES = {
    9000: "Initial",
    9001: "Heating",
    9002: "Printing",
    9003: "Calibrating",
    9004: "Calibrating",
    9005: "Cooling Down",
    9006: "Print Complete",
    9007: "Idle (Cooled)",
    9008: "Homing",
    9009: "Unloading Filament",
    9010: "Loading Filament",
    9011: "Idle (Cooled)",
    9012: "Calibrating",
    9021: "Loading Filament",
    9029: "Homing",
    9030: "Calibrating",
    9031: "Calibrating",
    9032: "Calibrating",
    9033: "Calibrating",
    9034: "Idle",
    9039: "Printing",
    9040: "Paused",
    9050: "Cancelling",
    9060: "Error",
    9070: "Busy",
    9080: "Scanning",
    9090: "Cleaning Nozzle",
    9100: "Updating Firmware",
    9500: "Ready",
    9510: "Idle",
    9511: "Idle",
    9520: "Busy",
    9530: "Busy",
}


class XYZPrinterStatus:
    """Parsed printer status from query_status()."""

    def __init__(self):
        self.model_number = ""
        self.machine_name = ""
        self.serial_number = ""
        self.firmware_version = ""
        self.state = 0
        self.sub_state = 0
        self.extruder_temp = 0
        self.extruder_target = 0
        self.bed_temp = 0
        self.bed_target = 0
        self.print_pct = 0
        self.print_elapsed_min = 0
        self.print_remaining_min = 0
        self.error_code = 0
        self.filament_remaining_mm = 0
        self.z_offset = 0
        self.auto_level = False


class XYZProtocol:
    """V3 serial protocol for XYZ da Vinci printers.

    Protocol overview:
    - 115200 baud, 8N1 over USB serial (CDC-ACM)
    - Commands are text strings: XYZv3/query=, XYZv3/config=, XYZv3/action=, XYZv3/upload=
    - Responses are text terminated by '$' character
    - File upload uses binary chunked protocol with per-block acknowledgment
    """

    # XYZ printer USB VID/PID
    XYZ_VID = 0x28E7
    XYZ_PIDS = [0x0301, 0x0100, 0x0200]  # miniMaker, Jr, Pro

    BAUD_RATE = 115200
    TIMEOUT = 5  # seconds for normal commands
    UPLOAD_TIMEOUT = 30  # seconds for upload blocks

    def __init__(self):
        self._serial = None
        self._connected = False

    @staticmethod
    def detect_xyz_ports():
        """Scan for XYZ printers on USB serial ports.

        Returns list of (port_name, description) tuples.
        """
        results = []
        try:
            import serial.tools.list_ports
            for port_info in serial.tools.list_ports.comports():
                desc = port_info.description or ""
                # Match by VID/PID
                if port_info.vid == XYZProtocol.XYZ_VID and port_info.pid in XYZProtocol.XYZ_PIDS:
                    results.append((port_info.device, f"XYZ Printer ({desc})"))
                # Also match by description keywords
                elif any(kw in desc.lower() for kw in ["xyz", "davinci", "da vinci"]):
                    results.append((port_info.device, desc))
                # Also match CDC serial devices that might be XYZ printers
                elif port_info.vid == XYZProtocol.XYZ_VID:
                    results.append((port_info.device, f"XYZ Device ({desc})"))
        except ImportError:
            logger.warning("pyserial not installed — cannot scan ports")
        except Exception as e:
            logger.error("Port scan error: %s", e)

        # If no VID/PID matches, list all COM ports as fallback
        if not results:
            try:
                import serial.tools.list_ports
                for port_info in serial.tools.list_ports.comports():
                    desc = port_info.description or port_info.device
                    results.append((port_info.device, desc))
            except Exception:
                pass

        return results

    def connect(self, port: str) -> bool:
        """Connect to printer on the given serial port."""
        try:
            import serial
            self._serial = serial.Serial(
                port=port,
                baudrate=self.BAUD_RATE,
                timeout=self.TIMEOUT,
                write_timeout=self.TIMEOUT,
            )
            time.sleep(0.5)  # Let the connection stabilize
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            self._connected = True
            logger.info("Connected to %s at %d baud", port, self.BAUD_RATE)
            return True
        except Exception as e:
            logger.error("Failed to connect to %s: %s", port, e)
            self._serial = None
            self._connected = False
            return False

    def disconnect(self):
        """Disconnect from the printer."""
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._connected = False
        logger.info("Disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._serial is not None

    def _send_command(self, cmd: str) -> str:
        """Send a command and read the response (up to '$' terminator).

        Returns the response text with the '$' stripped.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to printer")

        # Send command
        cmd_bytes = cmd.encode("ascii") + b"\n"
        self._serial.write(cmd_bytes)
        self._serial.flush()
        logger.debug("TX: %s", cmd)

        # Read response until '$' or timeout
        response = b""
        deadline = time.time() + self.TIMEOUT
        while time.time() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            if chunk:
                response += chunk
                if b"$" in response:
                    break

        text = response.decode("ascii", errors="replace")
        # Strip the '$' terminator and any trailing whitespace
        text = text.replace("$", "").strip()
        logger.debug("RX: %s", text[:200])
        return text

    def query_status(self) -> XYZPrinterStatus:
        """Query full printer status.

        Sends: XYZv3/query=a
        Response contains lines like:
            j:9511,11
            t:1,205,0,210
            n:dv1MX0A000
            s:SN123456
            v:1.3.5
            ...
        """
        status = XYZPrinterStatus()

        try:
            response = self._send_command("XYZv3/query=a")
        except Exception as e:
            logger.warning("Status query failed: %s", e)
            return status

        for line in response.splitlines():
            line = line.strip()
            if not line:
                continue

            # Parse individual fields — each line starts with a letter:value format
            # Multiple fields can be on one line separated by '.'
            # but be careful: firmware version like '1.3.5' also contains dots
            # We split on dots that are followed by a single letter and colon
            segments = re.split(r'\.(?=[a-zA-Z]:)', line)

            for seg in segments:
                seg = seg.strip()
                if len(seg) < 2 or seg[1] != ':':
                    continue

                key = seg[0]
                val = seg[2:]

                try:
                    if key == 'j':
                        # j:state,substate
                        parts = val.split(',')
                        status.state = int(parts[0])
                        if len(parts) > 1:
                            status.sub_state = int(parts[1])
                    elif key == 't':
                        # t:extruder_count,current_temp,bed_temp,target_temp
                        parts = val.split(',')
                        if len(parts) >= 2:
                            status.extruder_temp = int(parts[1])
                        if len(parts) >= 3:
                            status.bed_temp = int(parts[2])
                        if len(parts) >= 4:
                            status.extruder_target = int(parts[3])
                    elif key == 'n':
                        # n:model_number
                        status.model_number = val.strip()
                        if status.model_number in PRINTER_DB:
                            status.machine_name = PRINTER_DB[status.model_number]["name"]
                    elif key == 's':
                        # s:serial_number
                        status.serial_number = val.strip()
                    elif key == 'v':
                        # v:firmware_version (e.g. 1.3.5)
                        status.firmware_version = val.strip()
                    elif key == 'e':
                        # e:error_code
                        status.error_code = int(val)
                    elif key == 'd':
                        # d:print_pct,elapsed_min,remaining_min
                        parts = val.split(',')
                        if len(parts) >= 1:
                            status.print_pct = int(parts[0])
                        if len(parts) >= 2:
                            status.print_elapsed_min = int(parts[1])
                        if len(parts) >= 3:
                            status.print_remaining_min = int(parts[2])
                    elif key == 'f':
                        # f:filament_remaining_mm
                        parts = val.split(',')
                        status.filament_remaining_mm = int(parts[0])
                    elif key == 'o':
                        # o:z_offset (in 1/100 mm)
                        status.z_offset = int(val)
                    elif key == 'l':
                        # l:auto_level (0 or 1)
                        status.auto_level = val.strip() == '1'
                except (ValueError, IndexError) as e:
                    logger.debug("Parse error for field '%s:%s': %s", key, val, e)

        return status

    def upload_file(self, filename: str, data: bytes,
                    progress_callback=None) -> bool:
        """Upload a .3w file to the printer using V3 chunked protocol.

        Protocol:
        1. Send XYZv3/upload=filename,size
        2. Wait for 'ok' acknowledgment
        3. Send data in 8KB chunks, each prefixed with:
           - 4 bytes big-endian block number
           - 4 bytes big-endian block data size
           and suffixed with 4 null bytes
        4. Wait for 'ok' after each chunk
        5. Send XYZv3/uploadDidFinish
        """
        if not self.is_connected:
            return False

        file_size = len(data)
        logger.info("Uploading %s (%d bytes)", filename, file_size)

        try:
            # Step 1: Initiate upload
            cmd = f"XYZv3/upload={filename},{file_size}"
            self._serial.write(cmd.encode("ascii") + b"\n")
            self._serial.flush()

            if not self._wait_for_ok():
                logger.error("Printer rejected upload initiation")
                return False

            # Step 2: Send data in 8KB blocks
            block_size = 8192
            block_num = 0
            offset = 0

            while offset < file_size:
                chunk = data[offset:offset + block_size]
                chunk_len = len(chunk)

                # Build block: [4B block_num BE][4B chunk_size BE][data][4B zeros]
                header = struct.pack(">II", block_num, chunk_len)
                footer = b"\x00\x00\x00\x00"
                block_data = header + chunk + footer

                self._serial.write(block_data)
                self._serial.flush()

                if not self._wait_for_ok(timeout=self.UPLOAD_TIMEOUT):
                    logger.error("No ack for block %d", block_num)
                    return False

                offset += chunk_len
                block_num += 1

                if progress_callback:
                    pct = (offset / file_size) * 100
                    progress_callback(min(pct, 100))

            # Step 3: Signal upload complete
            self._serial.write(b"XYZv3/uploadDidFinish\n")
            self._serial.flush()

            if not self._wait_for_ok():
                logger.warning("No final ack (print may still start)")

            logger.info("Upload complete: %d blocks sent", block_num)
            return True

        except Exception as e:
            logger.error("Upload failed: %s", e)
            return False

    def _wait_for_ok(self, timeout=None) -> bool:
        """Wait for 'ok' response from printer."""
        if timeout is None:
            timeout = self.TIMEOUT

        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            if chunk:
                buf += chunk
                text = buf.decode("ascii", errors="replace").lower()
                if "ok" in text:
                    return True
                if "err" in text or "error" in text:
                    logger.error("Printer error: %s", text.strip())
                    return False
        logger.warning("Timeout waiting for ok (got: %s)", buf[:100])
        return False

    # ---- Printer Control Commands ----

    def home(self) -> bool:
        """Home all axes."""
        try:
            resp = self._send_command("XYZv3/action=home")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Home failed: %s", e)
            return False

    def load_filament_start(self) -> bool:
        """Start filament load (heats nozzle, then feeds filament)."""
        try:
            resp = self._send_command("XYZv3/action=loadfilament")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Load filament failed: %s", e)
            return False

    def load_filament_cancel(self) -> bool:
        """Cancel filament load in progress."""
        try:
            resp = self._send_command("XYZv3/action=loadfilamentcancel")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Cancel load failed: %s", e)
            return False

    def unload_filament_start(self) -> bool:
        """Start filament unload (heats nozzle, then retracts filament)."""
        try:
            resp = self._send_command("XYZv3/action=unloadfilament")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Unload filament failed: %s", e)
            return False

    def unload_filament_cancel(self) -> bool:
        """Cancel filament unload in progress."""
        try:
            resp = self._send_command("XYZv3/action=unloadfilamentcancel")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Cancel unload failed: %s", e)
            return False

    def cancel_print(self) -> bool:
        """Cancel the current print job."""
        try:
            resp = self._send_command("XYZv3/action=cancel")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Cancel print failed: %s", e)
            return False

    def pause_print(self) -> bool:
        """Pause the current print job."""
        try:
            resp = self._send_command("XYZv3/action=pause")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Pause failed: %s", e)
            return False

    def resume_print(self) -> bool:
        """Resume a paused print job."""
        try:
            resp = self._send_command("XYZv3/action=resume")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Resume failed: %s", e)
            return False

    def calibrate_start(self) -> bool:
        """Start bed calibration."""
        try:
            resp = self._send_command("XYZv3/action=calibratejr")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Calibrate failed: %s", e)
            return False

    def clean_nozzle_start(self) -> bool:
        """Start nozzle cleaning (heats and allows wipe)."""
        try:
            resp = self._send_command("XYZv3/action=cleannozzle")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Clean nozzle failed: %s", e)
            return False

    def clean_nozzle_cancel(self) -> bool:
        """Cancel nozzle cleaning."""
        try:
            resp = self._send_command("XYZv3/action=cleannozzlecancel")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Cancel clean failed: %s", e)
            return False

    def jog(self, axis: str, direction: int, distance: int = 10) -> bool:
        """Jog an axis. axis='x'|'y'|'z', direction=1|-1, distance in mm."""
        val = distance * direction
        try:
            resp = self._send_command(f"XYZv3/action=jog:{{{axis}:{val}}}")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Jog failed: %s", e)
            return False

    def z_offset_get(self) -> int:
        """Get current z-offset in 1/100 mm."""
        try:
            resp = self._send_command("XYZv3/config=zoffset:get")
            # Response like "zoffset:15"
            match = re.search(r'zoffset[=:](-?\d+)', resp)
            if match:
                return int(match.group(1))
        except Exception as e:
            logger.error("Z-offset get failed: %s", e)
        return 0

    def z_offset_set(self, value: int) -> bool:
        """Set z-offset in 1/100 mm."""
        try:
            resp = self._send_command(f"XYZv3/config=zoffset:{value}")
            return "ok" in resp.lower() or "E0" not in resp
        except Exception as e:
            logger.error("Z-offset set failed: %s", e)
            return False

    def auto_level_on(self) -> bool:
        """Enable auto-leveling."""
        try:
            resp = self._send_command("XYZv3/config=autolevel:on")
            return "ok" in resp.lower()
        except Exception as e:
            logger.error("Auto level on failed: %s", e)
            return False

    def auto_level_off(self) -> bool:
        """Disable auto-leveling."""
        try:
            resp = self._send_command("XYZv3/config=autolevel:off")
            return "ok" in resp.lower()
        except Exception as e:
            logger.error("Auto level off failed: %s", e)
            return False

    def buzzer_on(self) -> bool:
        """Enable the buzzer."""
        try:
            resp = self._send_command("XYZv3/config=buzzer:on")
            return "ok" in resp.lower()
        except Exception as e:
            logger.error("Buzzer on failed: %s", e)
            return False

    def buzzer_off(self) -> bool:
        """Disable the buzzer."""
        try:
            resp = self._send_command("XYZv3/config=buzzer:off")
            return "ok" in resp.lower()
        except Exception as e:
            logger.error("Buzzer off failed: %s", e)
            return False
