from __future__ import annotations

import ctypes
import ctypes.util
import os
import pty
import select
import time
from pathlib import Path
from typing import Dict, Optional

from .driver import PositionFix
from .nmea import parse_gga, parse_nmea_sentence, parse_rmc


LIBUSB_SUCCESS = 0
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_NOT_SUPPORTED = -12

PL2303_VENDOR_ID = 0x067B
PL2303_PRODUCT_ID = 0xAAA0


class PL2303Error(RuntimeError):
    pass


class _LibUSB:
    def __init__(self) -> None:
        self.lib = self._load_library()
        self._bind()

    @staticmethod
    def _load_library() -> ctypes.CDLL:
        candidates = []

        env_override = os.environ.get("GPS360_LIBUSB_PATH")
        if env_override:
            candidates.append(env_override)

        module_dir = Path(__file__).resolve().parent
        bundled_candidates = [
            module_dir / "libusb-1.0.dylib",
            module_dir.parent / "lib" / "libusb-1.0.dylib",
            module_dir.parent.parent / "lib" / "libusb-1.0.dylib",
        ]
        candidates.extend(str(path) for path in bundled_candidates)

        candidates.extend([
            "/opt/homebrew/lib/libusb-1.0.dylib",
            "/usr/local/lib/libusb-1.0.dylib",
        ])
        for path in candidates:
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue

        name = ctypes.util.find_library("usb-1.0") or ctypes.util.find_library(
            "libusb-1.0"
        )
        if not name:
            raise PL2303Error(
                "libusb not found. Install it (for Homebrew: `brew install libusb`)."
            )
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            raise PL2303Error(f"Failed to load libusb library '{name}': {exc}") from exc

    def _bind(self) -> None:
        lib = self.lib
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        lib.libusb_exit.restype = None

        lib.libusb_open_device_with_vid_pid.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint16,
        ]
        lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_close.restype = None

        lib.libusb_set_auto_detach_kernel_driver.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int

        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int

        lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint,
        ]
        lib.libusb_control_transfer.restype = ctypes.c_int

        lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
        ]
        lib.libusb_bulk_transfer.restype = ctypes.c_int

        if hasattr(lib, "libusb_error_name"):
            lib.libusb_error_name.argtypes = [ctypes.c_int]
            lib.libusb_error_name.restype = ctypes.c_char_p

    def error_name(self, code: int) -> str:
        if hasattr(self.lib, "libusb_error_name"):
            name = self.lib.libusb_error_name(code)
            if name:
                try:
                    return name.decode("ascii", errors="replace")
                except Exception:
                    pass
        return f"libusb_error({code})"


_LIBUSB_INSTANCE: Optional[_LibUSB] = None


def _libusb() -> _LibUSB:
    global _LIBUSB_INSTANCE
    if _LIBUSB_INSTANCE is None:
        _LIBUSB_INSTANCE = _LibUSB()
    return _LIBUSB_INSTANCE


class PL2303Driver:
    def __init__(
        self,
        vendor_id: int = PL2303_VENDOR_ID,
        product_id: int = PL2303_PRODUCT_ID,
        baud: int = 4800,
        auto_switch_to_nmea: bool = True,
        interface: int = 0,
        in_endpoint: int = 0x83,
        out_endpoint: int = 0x02,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.baud = baud
        self.auto_switch_to_nmea = auto_switch_to_nmea
        self.interface = interface
        self.in_endpoint = in_endpoint
        self.out_endpoint = out_endpoint

        self._ctx: Optional[ctypes.c_void_p] = None
        self._handle: Optional[ctypes.c_void_p] = None
        self._buffer = bytearray()
        self._state: Dict[str, object] = {}

        self._control_timeout_ms = 1_000

    def __enter__(self) -> "PL2303Driver":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def device_present(
        vendor_id: int = PL2303_VENDOR_ID,
        product_id: int = PL2303_PRODUCT_ID,
    ) -> bool:
        lib = _libusb().lib
        ctx = ctypes.c_void_p()
        rc = lib.libusb_init(ctypes.byref(ctx))
        if rc != LIBUSB_SUCCESS:
            return False
        try:
            handle = lib.libusb_open_device_with_vid_pid(ctx, vendor_id, product_id)
            if handle:
                lib.libusb_close(handle)
                return True
            return False
        finally:
            lib.libusb_exit(ctx)

    def open(self) -> None:
        if self._handle is not None:
            return

        lib = _libusb().lib
        ctx = ctypes.c_void_p()
        rc = lib.libusb_init(ctypes.byref(ctx))
        if rc != LIBUSB_SUCCESS:
            raise PL2303Error(f"libusb init failed: {_libusb().error_name(rc)}")
        self._ctx = ctx

        handle = lib.libusb_open_device_with_vid_pid(ctx, self.vendor_id, self.product_id)
        if not handle:
            self.close()
            raise PL2303Error(
                "Prolific PL2303 device not found. Confirm the receiver is plugged in."
            )
        self._handle = handle

        try:
            detach_rc = lib.libusb_set_auto_detach_kernel_driver(handle, 1)
            if detach_rc not in (LIBUSB_SUCCESS, LIBUSB_ERROR_NOT_SUPPORTED):
                raise PL2303Error(
                    f"Failed to set auto-detach: {_libusb().error_name(detach_rc)}"
                )

            claim_rc = lib.libusb_claim_interface(handle, self.interface)
            if claim_rc != LIBUSB_SUCCESS:
                if claim_rc in (LIBUSB_ERROR_ACCESS, LIBUSB_ERROR_BUSY):
                    raise PL2303Error(
                        "Failed to claim PL2303 USB interface. Another process may "
                        "already be using the GPS device."
                    )
                raise PL2303Error(
                    f"Failed to claim interface {self.interface}: "
                    f"{_libusb().error_name(claim_rc)}"
                )

            self._initialize_chip()
            self._set_line_coding(baud=self.baud)
            self._set_control_line_state(dtr=True, rts=True)
            if self.auto_switch_to_nmea:
                self._switch_output_to_nmea(self.baud)
        except Exception:
            self.close()
            raise

        self._buffer.clear()
        self._state.clear()

    def close(self) -> None:
        lib = _libusb().lib

        if self._handle is not None:
            try:
                lib.libusb_release_interface(self._handle, self.interface)
            except Exception:
                pass
            try:
                lib.libusb_close(self._handle)
            except Exception:
                pass
            self._handle = None

        if self._ctx is not None:
            try:
                lib.libusb_exit(self._ctx)
            except Exception:
                pass
            self._ctx = None

    def read_sentence(self, timeout: float = 1.0) -> Optional[str]:
        self._require_open()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._pop_line()
            if line is not None:
                return line

            remaining = max(0.0, deadline - time.monotonic())
            chunk = self.read_bytes(max_len=512, timeout_ms=max(1, int(remaining * 1000)))
            if chunk:
                self._buffer.extend(chunk)

        return self._pop_line()

    def read_fix(self, timeout: float = 2.0) -> Optional[PositionFix]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            sentence = self.read_sentence(timeout=remaining)
            if sentence is None:
                continue

            parsed = parse_nmea_sentence(sentence)
            if not parsed or not parsed.checksum_valid:
                continue

            update = None
            if parsed.message_type == "GGA":
                update = parse_gga(parsed.fields)
            elif parsed.message_type == "RMC":
                update = parse_rmc(parsed.fields)
            if not update:
                continue

            valid_fix = bool(update.pop("is_valid_fix", False))
            self._merge_update(update, valid_fix=valid_fix)

            if valid_fix and "latitude" in self._state and "longitude" in self._state:
                return PositionFix(
                    timestamp_utc=self._state.get("timestamp_utc"),  # type: ignore[arg-type]
                    latitude=float(self._state["latitude"]),
                    longitude=float(self._state["longitude"]),
                    altitude_m=_as_optional_float(self._state.get("altitude_m")),
                    speed_knots=_as_optional_float(self._state.get("speed_knots")),
                    course_deg=_as_optional_float(self._state.get("course_deg")),
                    satellites=_as_optional_int(self._state.get("satellites")),
                    hdop=_as_optional_float(self._state.get("hdop")),
                    fix_quality=_as_optional_int(self._state.get("fix_quality")),
                )
        return None

    def read_bytes(self, max_len: int = 512, timeout_ms: int = 200) -> bytes:
        self._require_open()

        buffer = (ctypes.c_ubyte * max_len)()
        transferred = ctypes.c_int()
        rc = _libusb().lib.libusb_bulk_transfer(
            self._handle,
            self.in_endpoint,
            buffer,
            max_len,
            ctypes.byref(transferred),
            timeout_ms,
        )
        if rc == LIBUSB_ERROR_TIMEOUT:
            return b""
        if rc != LIBUSB_SUCCESS:
            raise PL2303Error(f"USB read failed: {_libusb().error_name(rc)}")
        return bytes(buffer[: transferred.value])

    def write_bytes(self, payload: bytes, timeout_ms: int = 200) -> int:
        self._require_open()
        if not payload:
            return 0

        offset = 0
        total = 0
        while offset < len(payload):
            chunk = payload[offset : offset + 512]
            arr = (ctypes.c_ubyte * len(chunk))(*chunk)
            transferred = ctypes.c_int()
            rc = _libusb().lib.libusb_bulk_transfer(
                self._handle,
                self.out_endpoint,
                arr,
                len(chunk),
                ctypes.byref(transferred),
                timeout_ms,
            )
            if rc == LIBUSB_ERROR_TIMEOUT:
                continue
            if rc != LIBUSB_SUCCESS:
                raise PL2303Error(f"USB write failed: {_libusb().error_name(rc)}")
            wrote = transferred.value
            if wrote <= 0:
                break
            offset += wrote
            total += wrote
        return total

    def switch_output_to_nmea(self, baud: Optional[int] = None) -> None:
        self._require_open()
        target_baud = self.baud if baud is None else int(baud)
        # Send protocol switch first at current UART settings.
        self._switch_output_to_nmea(target_baud)
        if target_baud != self.baud:
            self._set_line_coding(baud=target_baud)
            self.baud = target_baud

    def switch_output_to_sirf_binary(self, baud: Optional[int] = None) -> None:
        self._require_open()
        target_baud = self.baud if baud is None else int(baud)
        # Send protocol switch first at current UART settings.
        self.write_bytes(_build_psrf100_binary_switch(target_baud), timeout_ms=300)
        if target_baud != self.baud:
            self._set_line_coding(baud=target_baud)
            self.baud = target_baud
        self._drain_input(duration_sec=0.35)

    def _initialize_chip(self) -> None:
        # This sequence mirrors the standard PL2303 startup handshake.
        self._vendor_read(0x8484, 0x0000)
        self._vendor_write(0x0404, 0x0000)
        self._vendor_read(0x8484, 0x0000)
        self._vendor_read(0x8383, 0x0000)
        self._vendor_read(0x8484, 0x0000)
        self._vendor_write(0x0404, 0x0001)
        self._vendor_read(0x8484, 0x0000)
        self._vendor_read(0x8383, 0x0000)
        self._vendor_write(0x0000, 0x0001)
        self._vendor_write(0x0001, 0x0000)

        # Different PL2303 variants require a different final index.
        last_error: Optional[Exception] = None
        for variant in (0x0024, 0x0044):
            try:
                self._vendor_write(0x0002, variant)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error

    def _set_line_coding(self, baud: int) -> None:
        payload = baud.to_bytes(4, byteorder="little", signed=False) + bytes(
            [0x00, 0x00, 0x08]
        )
        self._class_write(request=0x20, value=0x0000, payload=payload)

    def _set_control_line_state(self, dtr: bool, rts: bool) -> None:
        value = 0
        if dtr:
            value |= 0x0001
        if rts:
            value |= 0x0002
        self._class_write(request=0x22, value=value, payload=b"")

    def _switch_output_to_nmea(self, baud: int) -> None:
        # SiRF-based GPS units can boot in binary mode; this command switches
        # them to NMEA sentences at the requested baud.
        try:
            self.write_bytes(_build_sirf_switch_to_nmea(baud), timeout_ms=300)
        except Exception:
            pass

        # If it is already in NMEA mode, the SiRF packet above may be ignored.
        try:
            self.write_bytes(_build_psrf100_nmea_switch(baud), timeout_ms=300)
        except Exception:
            pass

        # Drain startup garbage and stale packets after mode switch.
        self._drain_input(duration_sec=0.4)

    def _drain_input(self, duration_sec: float = 0.4) -> None:
        drain_deadline = time.monotonic() + duration_sec
        while time.monotonic() < drain_deadline:
            _ = self.read_bytes(max_len=256, timeout_ms=50)

    def _vendor_read(self, value: int, index: int, length: int = 1) -> bytes:
        self._require_open()
        buf = (ctypes.c_ubyte * length)()
        rc = _libusb().lib.libusb_control_transfer(
            self._handle,
            0xC0,  # vendor | device | in
            0x01,
            value,
            index,
            buf,
            length,
            self._control_timeout_ms,
        )
        if rc < 0:
            raise PL2303Error(
                f"PL2303 vendor read failed: {_libusb().error_name(int(rc))}"
            )
        return bytes(buf[:rc])

    def _vendor_write(self, value: int, index: int) -> None:
        self._require_open()
        rc = _libusb().lib.libusb_control_transfer(
            self._handle,
            0x40,  # vendor | device | out
            0x01,
            value,
            index,
            None,
            0,
            self._control_timeout_ms,
        )
        if rc < 0:
            raise PL2303Error(
                f"PL2303 vendor write failed: {_libusb().error_name(int(rc))}"
            )

    def _class_write(self, request: int, value: int, payload: bytes) -> None:
        self._require_open()
        buf = None
        length = 0
        if payload:
            buf = (ctypes.c_ubyte * len(payload))(*payload)
            length = len(payload)

        rc = _libusb().lib.libusb_control_transfer(
            self._handle,
            0x21,  # class | interface | out
            request,
            value,
            self.interface,
            buf,
            length,
            self._control_timeout_ms,
        )
        if rc < 0:
            raise PL2303Error(
                f"PL2303 class write failed: {_libusb().error_name(int(rc))}"
            )

    def _pop_line(self) -> Optional[str]:
        while b"\n" in self._buffer:
            idx = self._buffer.index(0x0A)
            raw = self._buffer[: idx + 1]
            del self._buffer[: idx + 1]

            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue

            start = line.find("$")
            if start >= 0:
                line = line[start:]
            if not line.startswith("$"):
                continue
            return line
        return None

    def _merge_update(self, update: Dict[str, object], valid_fix: bool) -> None:
        if valid_fix:
            for key, value in update.items():
                if value is not None:
                    self._state[key] = value
            return

        for key in ("timestamp_utc", "satellites", "hdop", "fix_quality"):
            value = update.get(key)
            if value is not None:
                self._state[key] = value

    def _require_open(self) -> None:
        if self._handle is None:
            raise PL2303Error("PL2303 driver is not open")


class PL2303PTYBridge:
    def __init__(
        self,
        driver: Optional[PL2303Driver] = None,
    ) -> None:
        self.driver = driver or PL2303Driver()
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.slave_path: Optional[str] = None

    def __enter__(self) -> "PL2303PTYBridge":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self.master_fd is not None:
            return
        self.driver.open()
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self.slave_fd)

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.slave_fd is not None:
            try:
                os.close(self.slave_fd)
            except OSError:
                pass
            self.slave_fd = None
        self.driver.close()

    def run(self, duration: Optional[float] = None, print_nmea: bool = False) -> None:
        if self.master_fd is None:
            raise PL2303Error("Bridge is not open")

        deadline = time.monotonic() + duration if duration is not None else None
        line_buf = bytearray()

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return

            inbound = self.driver.read_bytes(max_len=1024, timeout_ms=100)
            if inbound:
                os.write(self.master_fd, inbound)

                if print_nmea:
                    line_buf.extend(inbound)
                    while b"\n" in line_buf:
                        idx = line_buf.index(0x0A)
                        raw = line_buf[: idx + 1]
                        del line_buf[: idx + 1]
                        line = raw.decode("ascii", errors="ignore").strip()
                        if line:
                            print(line)

            readable, _, _ = select.select([self.master_fd], [], [], 0)
            if readable:
                outbound = os.read(self.master_fd, 1024)
                if outbound:
                    self.driver.write_bytes(outbound, timeout_ms=100)


def _as_optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _as_optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _build_sirf_switch_to_nmea(baud: int) -> bytes:
    payload = bytes(
        [
            0x81,
            0x02,
            0x01,
            0x01,  # GGA every fix
            0x00,
            0x00,  # suppress GLL
            0x01,
            0x01,  # GSA every fix
            0x05,
            0x01,  # GSV every 5 fixes
            0x01,
            0x01,  # RMC every fix
            0x00,
            0x00,  # suppress VTG
            0x00,
            0x01,
            0x00,
            0x01,
            0x00,
            0x01,
            0x00,
            0x01,
            (baud >> 8) & 0xFF,
            baud & 0xFF,
        ]
    )
    checksum = sum(payload) & 0x7FFF
    return bytes(
        [
            0xA0,
            0xA2,
            0x00,
            len(payload),
        ]
    ) + payload + bytes([(checksum >> 8) & 0xFF, checksum & 0xFF, 0xB0, 0xB3])


def _build_psrf100_nmea_switch(baud: int) -> bytes:
    sentence = f"PSRF100,1,{baud},8,1,0"
    checksum = 0
    for ch in sentence.encode("ascii"):
        checksum ^= ch
    return f"${sentence}*{checksum:02X}\r\n".encode("ascii")


def _build_psrf100_binary_switch(baud: int) -> bytes:
    # PSRF100 protocol select: 0=SiRF binary, 1=NMEA.
    sentence = f"PSRF100,0,{baud},8,1,0"
    checksum = 0
    for ch in sentence.encode("ascii"):
        checksum ^= ch
    return f"${sentence}*{checksum:02X}\r\n".encode("ascii")
