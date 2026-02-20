#!/usr/bin/env python3
# XYZDaVinciPlugin/XYZFileConverter.py
# GCode to .3w file converter for XYZ da Vinci printers
#
# Handles:
#   - G0 → G1 conversion (required by XYZ firmware)
#   - XYZ header comment injection
#   - AES encryption into .3w format
#   - Print info extraction (time, filament usage)
#
# Encryption details (from miniMover reverse engineering):
#   - AES key derived from "@xyzprinting.com" (16 bytes for AES-128, doubled for AES-256)
#   - AES-256-ECB for non-zip body (miniMaker and newer)
#   - AES-128-CBC for zip body (older models), IV = all zeros
#   - PKCS7 padding
#   - Body starts at offset 8192 in blocks of 0x2010 bytes

import re
import struct
import logging
import io
import zipfile

logger = logging.getLogger("XYZFileConverter")

# Encryption key — same for all XYZ printers
_AES_KEY_BASE = b"@xyzprinting.com"               # 16 bytes
_AES_KEY_256 = _AES_KEY_BASE + _AES_KEY_BASE       # 32 bytes (doubled)
_AES_IV = b"\x00" * 16                             # IV is all zeros


class XYZFileConverter:
    """Converts GCode to XYZ .3w encrypted format.

    Usage:
        info = XYZFileConverter.extract_print_info(gcode_text)
        converter = XYZFileConverter(model_number="dv1MX0A000")
        three_w_bytes = converter.convert_gcode_to_3w(
            gcode_text, info["print_time_sec"], info["filament_mm"]
        )
    """

    # Models that use AES-256-ECB (non-zip body) vs AES-128-CBC (zip body)
    # miniMaker and newer models use ECB, older Jr/Pro use CBC
    _ECB_MODELS = {
        "dv1MX0A000",  # miniMaker
        "dv1MW0A000",  # mini w
        "dv1MW0B000",  # mini wA
        "dv1MW0C000",  # mini w+
        "dv1NX0A000",  # nano
        "dv1NW0A000",  # nano w
    }

    HEADER_SIZE = 8192  # Fixed header region (bytes)
    BODY_BLOCK_SIZE = 0x2010  # 8208 bytes per encrypted body block

    def __init__(self, model_number: str = "dv1MX0A000"):
        self.model_number = model_number
        self.use_ecb = model_number in self._ECB_MODELS

    @staticmethod
    def extract_print_info(gcode: str) -> dict:
        """Extract print metadata from GCode text.

        Looks for Cura/slicer comments and also estimates from GCode content.
        Returns dict with print_time_sec, filament_mm, layer_count.
        """
        print_time_sec = 0
        filament_mm = 0
        layer_count = 0

        for line in gcode.splitlines():
            line_stripped = line.strip()

            # Cura-style time comment: ;TIME:1234
            m = re.match(r';\s*TIME\s*[:=]\s*(\d+)', line_stripped, re.IGNORECASE)
            if m:
                print_time_sec = int(m.group(1))
                continue

            # PrusaSlicer-style: ; estimated printing time = 1h 23m 45s
            m = re.match(r';\s*estimated printing time.*?=\s*(.*)', line_stripped, re.IGNORECASE)
            if m:
                time_str = m.group(1)
                hours = re.search(r'(\d+)\s*h', time_str)
                mins = re.search(r'(\d+)\s*m', time_str)
                secs = re.search(r'(\d+)\s*s', time_str)
                print_time_sec = (
                    (int(hours.group(1)) * 3600 if hours else 0) +
                    (int(mins.group(1)) * 60 if mins else 0) +
                    (int(secs.group(1)) if secs else 0)
                )
                continue

            # Filament usage: ;Filament used: 1234.5mm or ;MATERIAL:1234
            m = re.match(r';\s*(?:Filament\s*used|MATERIAL)\s*[:=]\s*([\d.]+)\s*(mm|m)?',
                         line_stripped, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                unit = (m.group(2) or "mm").lower()
                if unit == "m":
                    filament_mm = val * 1000
                else:
                    filament_mm = val
                continue

            # Layer count: ;LAYER_COUNT:123 or ;Layer count:
            m = re.match(r';\s*LAYER[_\s]*COUNT\s*[:=]\s*(\d+)', line_stripped, re.IGNORECASE)
            if m:
                layer_count = int(m.group(1))
                continue

            # Count ;LAYER: lines as fallback for layer count
            if re.match(r';\s*LAYER\s*[:=]\s*\d+', line_stripped, re.IGNORECASE):
                layer_count += 1

        # Fallback estimates if slicer comments weren't found
        if print_time_sec == 0:
            # Rough estimate: count G1 moves, assume ~0.1s each
            g1_count = sum(1 for line in gcode.splitlines()
                          if line.strip().startswith(('G1 ', 'G0 ')))
            print_time_sec = max(60, g1_count // 10)  # minimum 1 minute

        if filament_mm == 0:
            # Rough estimate from E values
            max_e = 0
            for line in gcode.splitlines():
                m = re.search(r'E([\d.]+)', line)
                if m:
                    try:
                        e_val = float(m.group(1))
                        if e_val > max_e:
                            max_e = e_val
                    except ValueError:
                        pass
            filament_mm = max_e if max_e > 0 else 1000  # default 1m

        return {
            "print_time_sec": print_time_sec,
            "filament_mm": filament_mm,
            "layer_count": layer_count,
        }

    def convert_gcode_to_3w(self, gcode: str, print_time_sec: int,
                            filament_mm: float) -> bytes:
        """Convert GCode text to encrypted .3w file bytes.

        Steps:
        1. Preprocess GCode (G0→G1, inject XYZ header comments)
        2. Encrypt the body
        3. Build the .3w header
        4. Return complete file bytes
        """
        # Step 1: Preprocess
        processed = self._preprocess_gcode(gcode, print_time_sec, filament_mm)
        body_bytes = processed.encode("utf-8")

        # Step 2: Encrypt
        if self.use_ecb:
            encrypted = self._encrypt_ecb(body_bytes)
        else:
            encrypted = self._encrypt_cbc_zip(body_bytes)

        # Step 3: Build header
        header = self._build_header(len(body_bytes), len(encrypted),
                                     print_time_sec, filament_mm)

        # Step 4: Combine
        result = header + encrypted
        logger.info("Created .3w file: %d bytes (header=%d, body=%d, encrypted=%d)",
                    len(result), len(header), len(body_bytes), len(encrypted))
        return result

    def _preprocess_gcode(self, gcode: str, print_time_sec: int,
                          filament_mm: float) -> str:
        """Preprocess GCode for XYZ printers.

        - Convert G0 (rapid move) to G1 (linear move) — XYZ firmware requires this
        - Inject required XYZ header comments if not present
        """
        lines = gcode.splitlines()
        result = []

        # Check if XYZ header comments already exist
        has_machine = any("; machine" in line.lower() for line in lines[:50])

        # Inject XYZ header comments at the top if missing
        if not has_machine:
            result.append(f"; machine = {self.model_number}")
            result.append(f"; print_time = {print_time_sec}")
            result.append(f"; total_filament = {filament_mm:.1f}")
            result.append(f"; nozzle_diameter = 0.4")
            result.append(f"; layer_height = 0.2")
            result.append(f"; filament_diameter = 1.75")
            result.append(f"; filament_type = PLA")
            result.append("")

        for line in lines:
            stripped = line.strip()

            # Convert G0 to G1 (XYZ firmware treats G0 as unknown)
            if stripped.startswith("G0 ") or stripped.startswith("G0\t"):
                stripped = "G1" + stripped[2:]
            elif stripped == "G0":
                stripped = "G1"

            result.append(stripped)

        return "\n".join(result) + "\n"

    def _encrypt_ecb(self, data: bytes) -> bytes:
        """Encrypt body using AES-256-ECB (for miniMaker and newer models).

        Body is encrypted in blocks. Each block is padded to 16-byte alignment.
        """
        from Crypto.Cipher import AES

        # Pad to 16-byte boundary (PKCS7)
        padded = self._pkcs7_pad(data, 16)

        cipher = AES.new(_AES_KEY_256, AES.MODE_ECB)
        encrypted = cipher.encrypt(padded)

        return encrypted

    def _encrypt_cbc_zip(self, data: bytes) -> bytes:
        """Encrypt body using AES-128-CBC with zip compression (for older models).

        Body is first zip-compressed, then AES-128-CBC encrypted.
        """
        from Crypto.Cipher import AES

        # Compress with zip
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("model.gcode", data)
        compressed = zip_buffer.getvalue()

        # Pad to 16-byte boundary (PKCS7)
        padded = self._pkcs7_pad(compressed, 16)

        cipher = AES.new(_AES_KEY_BASE, AES.MODE_CBC, iv=_AES_IV)
        encrypted = cipher.encrypt(padded)

        return encrypted

    @staticmethod
    def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
        """Apply PKCS7 padding."""
        padding_len = block_size - (len(data) % block_size)
        if padding_len == 0:
            padding_len = block_size
        return data + bytes([padding_len]) * padding_len

    def _build_header(self, body_size: int, encrypted_size: int,
                      print_time_sec: int, filament_mm: float) -> bytes:
        """Build the .3w file header (8192 bytes).

        Header structure:
        - Bytes 0-3: Magic "3DPFNKG00000000" (file identifier)
        - Various tag markers with offsets and sizes
        - Padded to 8192 bytes
        """
        header = bytearray(self.HEADER_SIZE)

        # Magic bytes / file signature
        magic = b"3DPFNKG00000000\x00"
        header[0:len(magic)] = magic

        # File format version
        header[16:20] = struct.pack("<I", 2)  # Version 2

        # Tag: model number (at offset 32)
        model_bytes = self.model_number.encode("ascii")[:32]
        header[32:32 + len(model_bytes)] = model_bytes

        # Tag: body offset and size (at offset 80)
        struct.pack_into("<I", header, 80, self.HEADER_SIZE)      # body offset
        struct.pack_into("<I", header, 84, encrypted_size)         # encrypted size
        struct.pack_into("<I", header, 88, body_size)              # original size

        # Tag: print time in seconds (at offset 96)
        struct.pack_into("<I", header, 96, print_time_sec)

        # Tag: filament usage in mm (at offset 100)
        struct.pack_into("<I", header, 100, int(filament_mm))

        # Tag: encryption type (at offset 104)
        # 0 = no encryption, 1 = AES-128-CBC (zip), 2 = AES-256-ECB
        enc_type = 2 if self.use_ecb else 1
        struct.pack_into("<I", header, 104, enc_type)

        return bytes(header)

    @staticmethod
    def decrypt_3w_to_gcode(three_w_data: bytes, model_number: str = "") -> str:
        """Decrypt a .3w file back to GCode (for diagnostics).

        Detects encryption type from header and decrypts accordingly.
        """
        from Crypto.Cipher import AES

        if len(three_w_data) < 8192:
            raise ValueError("File too small to be a valid .3w file")

        # Read header
        body_offset = struct.unpack_from("<I", three_w_data, 80)[0]
        encrypted_size = struct.unpack_from("<I", three_w_data, 84)[0]
        original_size = struct.unpack_from("<I", three_w_data, 88)[0]
        enc_type = struct.unpack_from("<I", three_w_data, 104)[0]

        # Extract encrypted body
        encrypted = three_w_data[body_offset:body_offset + encrypted_size]

        if enc_type == 2:
            # AES-256-ECB
            cipher = AES.new(_AES_KEY_256, AES.MODE_ECB)
            decrypted = cipher.decrypt(encrypted)
        elif enc_type == 1:
            # AES-128-CBC
            cipher = AES.new(_AES_KEY_BASE, AES.MODE_CBC, iv=_AES_IV)
            decrypted = cipher.decrypt(encrypted)
            # Decompress zip
            zip_buffer = io.BytesIO(decrypted)
            try:
                with zipfile.ZipFile(zip_buffer, 'r') as zf:
                    names = zf.namelist()
                    if names:
                        decrypted = zf.read(names[0])
            except zipfile.BadZipFile:
                pass  # Not actually zipped, use raw
        else:
            decrypted = encrypted

        # Remove PKCS7 padding
        if decrypted:
            pad_len = decrypted[-1]
            if 0 < pad_len <= 16 and all(b == pad_len for b in decrypted[-pad_len:]):
                decrypted = decrypted[:-pad_len]

        # Trim to original size
        if original_size > 0:
            decrypted = decrypted[:original_size]

        return decrypted.decode("utf-8", errors="replace")
