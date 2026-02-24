from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from .pl2303 import PL2303Driver, PL2303PTYBridge


def _int_any_base(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "User-space PL2303 serial driver bridge. "
            "Creates a virtual serial tty from the USB GPS."
        )
    )
    parser.add_argument("--vendor-id", type=_int_any_base, default=0x067B)
    parser.add_argument("--product-id", type=_int_any_base, default=0xAAA0)
    parser.add_argument("--baud", type=int, default=4800)
    parser.add_argument(
        "--no-pty",
        action="store_true",
        help="Read USB directly and print NMEA lines instead of creating a PTY",
    )
    parser.add_argument(
        "--print-nmea",
        action="store_true",
        help="Also print NMEA lines when running PTY bridge mode",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Read one NMEA sentence and exit (implies --no-pty)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="Optional maximum run time in seconds",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.once:
        args.no_pty = True

    driver = PL2303Driver(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        baud=args.baud,
    )

    if args.no_pty:
        return _run_direct(driver=driver, duration=args.duration, once=args.once)
    return _run_bridge(driver=driver, duration=args.duration, print_nmea=args.print_nmea)


def _run_direct(driver: PL2303Driver, duration: Optional[float], once: bool) -> int:
    try:
        with driver:
            print(
                f"Connected to USB {driver.vendor_id:04x}:{driver.product_id:04x}",
                file=sys.stderr,
            )
            deadline = time.monotonic() + duration if duration is not None else None
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return 0
                line = driver.read_sentence(timeout=1.0)
                if line is None:
                    continue
                print(line)
                if once:
                    return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def _run_bridge(driver: PL2303Driver, duration: Optional[float], print_nmea: bool) -> int:
    bridge = PL2303PTYBridge(driver=driver)
    try:
        with bridge:
            assert bridge.slave_path is not None
            print(f"Virtual serial device: {bridge.slave_path}")
            print(
                "Use this with the GPS app, e.g. "
                f"`python3 -m gps360.app --port {bridge.slave_path}`",
                file=sys.stderr,
            )
            bridge.run(duration=duration, print_nmea=print_nmea)
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
