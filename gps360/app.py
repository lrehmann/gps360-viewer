from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .driver import GPS360Driver, PositionFix
from .nmea import parse_nmea_sentence
from .pl2303 import PL2303Driver


def _int_any_base(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read live NMEA fixes from a Pharos Microsoft GPS-360 receiver."
    )
    parser.add_argument("--port", help="Serial port path, e.g. /dev/cu.usbmodemXXXX")
    parser.add_argument("--baud", type=int, default=4800, help="Serial baud rate")
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Seconds to wait per read cycle",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List candidate serial ports and exit",
    )
    parser.add_argument(
        "--jsonl",
        help="Append each fix as JSON Lines to this file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Read one valid fix and exit",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw NMEA sentences instead of parsed fixes",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "serial", "usb"),
        default="usb",
        help="Input transport (default: usb)",
    )
    parser.add_argument(
        "--usb-vid",
        type=_int_any_base,
        default=0x067B,
        help="USB vendor ID for direct mode (default: 0x067B)",
    )
    parser.add_argument(
        "--usb-pid",
        type=_int_any_base,
        default=0xAAA0,
        help="USB product ID for direct mode (default: 0xAAA0)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_ports:
        ports = GPS360Driver.list_candidate_ports()
        if not ports:
            print("No candidate /dev/cu.* serial ports found.", file=sys.stderr)
            return 1
        for port in ports:
            print(port)
        return 0

    logger = _JsonlLogger(args.jsonl) if args.jsonl else None

    try:
        if args.transport == "serial":
            return _run_serial(args=args, logger=logger)
        if args.transport == "usb":
            return _run_usb(args=args, logger=logger)
        return _run_auto(args=args, logger=logger)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - user-facing CLI guard.
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if logger is not None:
            logger.close()


def _run_auto(args: argparse.Namespace, logger: Optional["_JsonlLogger"]) -> int:
    try:
        return _run_serial(args=args, logger=logger)
    except Exception as serial_exc:
        if not _is_missing_serial_error(serial_exc):
            raise
        print(f"{serial_exc}", file=sys.stderr)
        print("Falling back to direct USB mode...", file=sys.stderr)
        return _run_usb(args=args, logger=logger)


def _run_serial(args: argparse.Namespace, logger: Optional["_JsonlLogger"]) -> int:
    driver = GPS360Driver(port=args.port, baud=args.baud)
    with driver:
        source = f"serial port: {driver.port}"
        return _run_loop(
            driver=driver,
            source=source,
            timeout=args.timeout,
            once=args.once,
            raw=args.raw,
            logger=logger,
        )


def _run_usb(args: argparse.Namespace, logger: Optional["_JsonlLogger"]) -> int:
    driver = PL2303Driver(
        vendor_id=args.usb_vid,
        product_id=args.usb_pid,
        baud=args.baud,
    )
    with driver:
        source = f"usb {args.usb_vid:04x}:{args.usb_pid:04x}"
        return _run_loop(
            driver=driver,
            source=source,
            timeout=args.timeout,
            once=args.once,
            raw=args.raw,
            logger=logger,
        )


def _run_loop(
    driver: object,
    source: str,
    timeout: float,
    once: bool,
    raw: bool,
    logger: Optional["_JsonlLogger"],
) -> int:
    print(f"Using {source}", file=sys.stderr)
    if raw:
        return _run_raw_mode(driver=driver, timeout=timeout, once=once)

    wait_notice_at = 0.0
    while True:
        fix = driver.read_fix(timeout=timeout)  # type: ignore[attr-defined]
        if fix is None:
            if once:
                print("No valid GPS fix before timeout.", file=sys.stderr)
                return 1
            now = time.monotonic()
            if now >= wait_notice_at:
                print("Waiting for valid GPS fix...", file=sys.stderr)
                wait_notice_at = now + 5.0
            continue

        print(format_fix(fix))
        if logger is not None:
            logger.write(fix)
        if once:
            return 0


def format_fix(fix: PositionFix) -> str:
    ts = fix.timestamp_utc or datetime.now(timezone.utc)
    parts = [
        ts.isoformat(),
        f"lat={fix.latitude:.6f}",
        f"lon={fix.longitude:.6f}",
    ]
    if fix.altitude_m is not None:
        parts.append(f"alt={fix.altitude_m:.1f}m")
    if fix.speed_knots is not None:
        kmh = fix.speed_knots * 1.852
        parts.append(f"speed={fix.speed_knots:.2f}kn/{kmh:.2f}kmh")
    if fix.course_deg is not None:
        parts.append(f"course={fix.course_deg:.1f}deg")
    if fix.satellites is not None:
        parts.append(f"sats={fix.satellites}")
    if fix.hdop is not None:
        parts.append(f"hdop={fix.hdop:.1f}")
    if fix.fix_quality is not None:
        parts.append(f"q={fix.fix_quality}")
    return "  ".join(parts)


def _run_raw_mode(driver: object, timeout: float, once: bool) -> int:
    wait_notice_at = 0.0
    while True:
        line = driver.read_sentence(timeout=timeout)  # type: ignore[attr-defined]
        if line is None:
            if once:
                print("No NMEA sentence received before timeout.", file=sys.stderr)
                return 1
            now = time.monotonic()
            if now >= wait_notice_at:
                print("Waiting for NMEA data...", file=sys.stderr)
                wait_notice_at = now + 5.0
            continue

        parsed = parse_nmea_sentence(line)
        if parsed is None:
            print(f"RAW: {line}")
            if once:
                continue
        else:
            state = "ok" if parsed.checksum_valid else "bad-checksum"
            print(f"{parsed.talker}{parsed.message_type} [{state}] {line}")
            if once:
                return 0


def _is_missing_serial_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "no gps serial port emitting nmea was found" in text
        or "no gps-like serial ports found" in text
    )


class _JsonlLogger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, fix: PositionFix) -> None:
        self._handle.write(json.dumps(fix.to_json_dict(), sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
