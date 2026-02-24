from __future__ import annotations

import glob
import os
import select
import subprocess
import termios
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Iterable, Optional

from .nmea import parse_gga, parse_nmea_sentence, parse_rmc


_KNOWN_NON_GPS_PORT_MARKERS = (
    "Bluetooth-Incoming-Port",
    "debug-console",
    "Bose",
)

_PREFERRED_PORT_MARKERS = (
    "usbmodem",
    "usbserial",
    "pl2303",
    "SLAB_USBtoUART",
    "wchusbserial",
    "serial",
)

_BAUD_MAP = {
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


@dataclass(frozen=True)
class PositionFix:
    timestamp_utc: Optional[datetime]
    latitude: float
    longitude: float
    altitude_m: Optional[float] = None
    speed_knots: Optional[float] = None
    course_deg: Optional[float] = None
    satellites: Optional[int] = None
    hdop: Optional[float] = None
    fix_quality: Optional[int] = None

    def to_json_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        if self.timestamp_utc is not None:
            payload["timestamp_utc"] = self.timestamp_utc.isoformat()
        return payload


class GPS360Driver:
    def __init__(self, port: Optional[str] = None, baud: int = 4800) -> None:
        if baud not in _BAUD_MAP:
            raise ValueError(
                f"Unsupported baud {baud}; choose one of {sorted(_BAUD_MAP)}"
            )
        self.port = port
        self.baud = baud
        self.fd: Optional[int] = None
        self._buffer = bytearray()
        self._state: Dict[str, object] = {}

    def __enter__(self) -> "GPS360Driver":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self.fd is not None:
            return
        if not self.port:
            self.port = self.auto_detect_port(baud=self.baud)
        if not self.port:
            extra = ""
            if _prolific_usb_attached_without_serial():
                extra = (
                    " Prolific USB hardware is attached, but no matching /dev/cu.* "
                    "serial interface is available. Install an Apple Silicon-compatible "
                    "PL2303/USB-serial driver, or run the app with `--transport usb`."
                )
            raise RuntimeError(
                "No GPS serial port emitting NMEA was found under /dev/cu.*."
                + extra
            )

        self.fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        self._configure_serial(self.fd, self.baud)
        self._buffer.clear()
        self._state.clear()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    @staticmethod
    def list_candidate_ports() -> list[str]:
        ports = sorted(set(glob.glob("/dev/cu.*")))
        if not ports:
            return []
        filtered = [
            p
            for p in ports
            if not any(marker in p for marker in _KNOWN_NON_GPS_PORT_MARKERS)
        ]
        if filtered:
            return _sort_ports(filtered)
        return _sort_ports(ports)

    @classmethod
    def auto_detect_port(
        cls,
        baud: int = 4800,
        probe_timeout: float = 4.0,
    ) -> Optional[str]:
        candidates = cls.list_candidate_ports()
        if not candidates:
            return None

        for port in candidates:
            if _looks_like_gps_stream(port=port, baud=baud, timeout=probe_timeout):
                return port

        return None

    def read_sentence(self, timeout: float = 1.0) -> Optional[str]:
        if self.fd is None:
            raise RuntimeError("Driver is not open")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._pop_line()
            if line is not None:
                return line

            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
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

    def _pop_line(self) -> Optional[str]:
        if b"\n" not in self._buffer:
            return None

        newline_idx = self._buffer.index(0x0A)
        raw = self._buffer[: newline_idx + 1]
        del self._buffer[: newline_idx + 1]
        line = raw.decode("ascii", errors="ignore").strip()
        if not line:
            return None
        return line

    def _merge_update(self, update: Dict[str, object], valid_fix: bool) -> None:
        if valid_fix:
            for key, value in update.items():
                if value is not None:
                    self._state[key] = value
            return

        # Keep metadata from invalid updates without replacing last known position.
        for key in ("timestamp_utc", "satellites", "hdop", "fix_quality"):
            value = update.get(key)
            if value is not None:
                self._state[key] = value

    @staticmethod
    def _configure_serial(fd: int, baud: int) -> None:
        attrs = termios.tcgetattr(fd)
        speed = _BAUD_MAP[baud]

        attrs[0] = termios.IGNPAR
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
        attrs[3] = 0
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1

        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _looks_like_gps_stream(port: str, baud: int, timeout: float) -> bool:
    fd: Optional[int] = None
    try:
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        GPS360Driver._configure_serial(fd, baud)

        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            buffer.extend(chunk)
            while b"\n" in buffer:
                idx = buffer.index(0x0A)
                raw = buffer[: idx + 1]
                del buffer[: idx + 1]
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                parsed = parse_nmea_sentence(line)
                if not parsed or not parsed.checksum_valid:
                    continue
                if parsed.message_type in {"GGA", "RMC", "GLL", "VTG", "GSA", "GSV"}:
                    return True
        return False
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _prolific_usb_attached_without_serial() -> bool:
    try:
        out = subprocess.check_output(
            ["ioreg", "-p", "IOUSB", "-l"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False

    prolific_present = (
        "USB-Serial Controller D" in out
        or '"idVendor" = 1659' in out
        or "Prolific Technology Inc." in out
    )
    if not prolific_present:
        return False

    # If a serial node is already present, normal auto-detection will use it.
    return not any("usbserial" in p.lower() for p in glob.glob("/dev/cu.*"))


def _sort_ports(ports: Iterable[str]) -> list[str]:
    def score(port: str) -> tuple[int, str]:
        lower = port.lower()
        for idx, marker in enumerate(_PREFERRED_PORT_MARKERS):
            if marker.lower() in lower:
                return (idx, lower)
        return (len(_PREFERRED_PORT_MARKERS), lower)

    return sorted(ports, key=score)


def _as_optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _as_optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    return int(value)
