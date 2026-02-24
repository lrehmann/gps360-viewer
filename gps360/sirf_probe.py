from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .pl2303 import PL2303Driver


SIRF_START = b"\xA0\xA2"
SIRF_END = b"\xB0\xB3"


def _int_any_base(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture and decode SiRF binary packets from the GPS-360 "
            "(engineering/raw mode probe)."
        )
    )
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--baud", type=int, default=4800)
    parser.add_argument("--binary-baud", type=int, default=4800)
    parser.add_argument("--usb-vid", type=_int_any_base, default=0x067B)
    parser.add_argument("--usb-pid", type=_int_any_base, default=0xAAA0)
    parser.add_argument("--no-switch", action="store_true")
    parser.add_argument("--leave-binary", action="store_true")
    parser.add_argument(
        "--out",
        help="Raw binary capture path (default: captures/sirf-<timestamp>.bin)",
    )
    parser.add_argument(
        "--summary",
        help="JSON summary path (default: captures/sirf-<timestamp>.json)",
    )
    return parser


@dataclass
class SirfFrame:
    message_id: int
    payload: bytes
    checksum_ok: bool


class SirfStreamParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> List[SirfFrame]:
        if chunk:
            self._buffer.extend(chunk)
        frames: List[SirfFrame] = []

        while True:
            start = self._buffer.find(SIRF_START)
            if start < 0:
                if len(self._buffer) > 1:
                    del self._buffer[:-1]
                break
            if start > 0:
                del self._buffer[:start]

            if len(self._buffer) < 8:
                break

            length = (self._buffer[2] << 8) | self._buffer[3]
            total = length + 8
            if len(self._buffer) < total:
                break

            if self._buffer[total - 2 : total] != SIRF_END:
                # Resync on malformed frame.
                del self._buffer[0]
                continue

            payload = bytes(self._buffer[4 : 4 + length])
            checksum = (self._buffer[4 + length] << 8) | self._buffer[5 + length]
            computed = sum(payload) & 0x7FFF
            mid = payload[0] if payload else -1

            frames.append(
                SirfFrame(
                    message_id=mid,
                    payload=payload,
                    checksum_ok=(computed == checksum),
                )
            )
            del self._buffer[:total]

        return frames


def _u16_be(buf: bytes, off: int) -> int:
    return (buf[off] << 8) | buf[off + 1]


def _u32_be(buf: bytes, off: int) -> int:
    return (buf[off] << 24) | (buf[off + 1] << 16) | (buf[off + 2] << 8) | buf[off + 3]


def _s16_be(buf: bytes, off: int) -> int:
    value = _u16_be(buf, off)
    if value & 0x8000:
        value -= 0x10000
    return value


def _s32_be(buf: bytes, off: int) -> int:
    value = _u32_be(buf, off)
    if value & 0x80000000:
        value -= 0x100000000
    return value


def _mode_status_from_navtype(navtype: int) -> Dict[str, object]:
    nav_code = navtype & 0x07
    if nav_code == 0:
        status = "no_fix"
        mode = "no_fix"
    elif nav_code == 7:
        status = "dead_reckoning"
        mode = "dr"
    elif navtype & 0x80:
        status = "dgps"
        mode = "2d_or_3d"
    else:
        status = "gps"
        mode = "2d_or_3d"

    if nav_code in (4, 6):
        mode = "3d"
    elif nav_code != 0 and nav_code != 7:
        mode = "2d"

    return {"status": status, "mode": mode, "nav_code": nav_code}


def decode_mid_0x02(payload: bytes) -> Dict[str, object]:
    if len(payload) < 41:
        return {"decode_error": "runt"}
    navtype = payload[19]
    week = _u16_be(payload, 22)
    i_tow_10ms = _u32_be(payload, 24)
    tow_ms = i_tow_10ms * 10
    dop = payload[20] / 5.0

    decoded: Dict[str, object] = {
        "message": "nav_solution",
        "week": week,
        "tow_ms": tow_ms,
        "ecef_m": {
            "x": _s32_be(payload, 1),
            "y": _s32_be(payload, 5),
            "z": _s32_be(payload, 9),
        },
        "ecef_vel_mps": {
            "vx": _s16_be(payload, 13) / 8.0,
            "vy": _s16_be(payload, 15) / 8.0,
            "vz": _s16_be(payload, 17) / 8.0,
        },
        "dop_scaled": dop,
        "mode2_raw": payload[21],
    }
    decoded.update(_mode_status_from_navtype(navtype))
    return decoded


def decode_mid_0x04(payload: bytes) -> Dict[str, object]:
    if len(payload) != 188:
        return {"decode_error": f"unexpected_length_{len(payload)}"}

    satellites = []
    used = 0
    for idx in range(12):
        off = 8 + 15 * idx
        prn = payload[off]
        az = (payload[off + 1] * 3.0) / 2.0
        el = payload[off + 2] / 2.0
        stat = _u16_be(payload, off + 3)
        cn = sum(payload[off + 5 : off + 15]) / 10.0
        good = prn != 0 and az != 0 and el != 0
        if not good:
            continue
        used_flag = bool(stat & 0x01)
        if used_flag:
            used += 1
        satellites.append(
            {
                "prn": prn,
                "azimuth_deg": round(az, 1),
                "elevation_deg": round(el, 1),
                "snr_dbhz": round(cn, 1),
                "used": used_flag,
            }
        )

    satellites.sort(key=lambda sat: sat["snr_dbhz"], reverse=True)
    snr_values = [sat["snr_dbhz"] for sat in satellites]

    return {
        "message": "tracker_data",
        "satellites_visible": len(satellites),
        "satellites_used": used,
        "snr_avg_dbhz": round(sum(snr_values) / len(snr_values), 1) if snr_values else None,
        "snr_max_dbhz": max(snr_values) if snr_values else None,
        "top_satellites": satellites[:8],
    }


def decode_mid_0x06(payload: bytes) -> Dict[str, object]:
    text = payload[1:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return {"message": "software_version", "text": text}


def decode_mid_0x29(payload: bytes) -> Dict[str, object]:
    if len(payload) != 91:
        return {"decode_error": f"unexpected_length_{len(payload)}"}

    navtype = _u16_be(payload, 3)
    week = _u16_be(payload, 5)
    tow_ms = _u32_be(payload, 7)
    decoded: Dict[str, object] = {
        "message": "geodetic_nav",
        "week": week,
        "tow_ms": tow_ms,
        "latitude_deg": _s32_be(payload, 23) * 1e-7,
        "longitude_deg": _s32_be(payload, 27) * 1e-7,
        "alt_hae_m": _s32_be(payload, 31) * 1e-2,
        "alt_msl_m": _s32_be(payload, 35) * 1e-2,
        "speed_mps": _u16_be(payload, 40) * 1e-2,
        "track_deg": _u16_be(payload, 42) * 1e-2,
        "climb_mps": _s16_be(payload, 46) * 1e-2,
        "horiz_err_m": _u32_be(payload, 50) * 1e-2,
        "vert_err_m": _u32_be(payload, 54) * 1e-2,
        "speed_err_mps": _u16_be(payload, 62) * 1e-2,
    }
    hdop_byte = payload[89]
    decoded["hdop"] = hdop_byte * 0.2 if hdop_byte else None
    decoded.update(_mode_status_from_navtype(navtype))
    return decoded


DECODERS = {
    0x02: decode_mid_0x02,
    0x04: decode_mid_0x04,
    0x06: decode_mid_0x06,
    0x29: decode_mid_0x29,
}


def summarize(frames: List[SirfFrame], duration: float) -> Dict[str, object]:
    counts = Counter(frame.message_id for frame in frames)
    bad = sum(1 for frame in frames if not frame.checksum_ok)
    per_mid: Dict[str, Dict[str, object]] = {}
    latest_decodes: Dict[str, Dict[str, object]] = {}

    for mid, count in sorted(counts.items(), key=lambda item: item[0]):
        per_mid[f"0x{mid:02x}"] = {
            "count": count,
            "rate_hz": round(count / duration, 3) if duration > 0 else None,
        }

    for frame in frames:
        if not frame.checksum_ok:
            continue
        decoder = DECODERS.get(frame.message_id)
        if decoder is None:
            continue
        try:
            decoded = decoder(frame.payload)
        except Exception as exc:  # pragma: no cover - defensive guard for probe use.
            decoded = {"decode_error": str(exc)}
        latest_decodes[f"0x{frame.message_id:02x}"] = decoded

    rough_location_hint = None
    geo = latest_decodes.get("0x29")
    if geo:
        lat = geo.get("latitude_deg")
        lon = geo.get("longitude_deg")
        mode = geo.get("mode")
        if isinstance(lat, (float, int)) and isinstance(lon, (float, int)):
            if abs(float(lat)) > 1e-9 and abs(float(lon)) > 1e-9:
                rough_location_hint = {
                    "latitude_deg": round(float(lat), 7),
                    "longitude_deg": round(float(lon), 7),
                    "source": "mid_0x29",
                    "mode": mode,
                    "note": (
                        "Values came from SiRF geodetic packet; treat as tentative until fix is 2D/3D."
                    ),
                }

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "duration_sec": duration,
        "frame_count": len(frames),
        "checksum_bad": bad,
        "message_counts": per_mid,
        "latest_decodes": latest_decodes,
        "rough_location_hint": rough_location_hint,
    }


def _default_paths() -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path("captures")
    return (root / f"sirf-{stamp}.bin", root / f"sirf-{stamp}.json")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    default_raw, default_summary = _default_paths()
    raw_path = Path(args.out) if args.out else default_raw
    summary_path = Path(args.summary) if args.summary else default_summary
    _ensure_parent(raw_path)
    _ensure_parent(summary_path)

    parser = SirfStreamParser()
    raw_capture = bytearray()
    frames: List[SirfFrame] = []

    driver = PL2303Driver(
        vendor_id=args.usb_vid,
        product_id=args.usb_pid,
        baud=args.baud,
        auto_switch_to_nmea=False,
    )

    start = time.monotonic()
    switched_to_binary = False
    with driver:
        if not args.no_switch:
            driver.switch_output_to_sirf_binary(baud=args.binary_baud)
            switched_to_binary = True

        deadline = start + args.duration
        while time.monotonic() < deadline:
            chunk = driver.read_bytes(max_len=2048, timeout_ms=250)
            if not chunk:
                continue
            raw_capture.extend(chunk)
            frames.extend(parser.feed(chunk))

        if switched_to_binary and not args.leave_binary:
            try:
                driver.switch_output_to_nmea(baud=args.baud)
            except Exception:
                pass

    duration = max(0.0, time.monotonic() - start)
    raw_path.write_bytes(raw_capture)

    report = summarize(frames=frames, duration=duration)
    nmea_like = raw_capture.count(b"$GP") + raw_capture.count(b"$GN")
    report["wire_observation"] = {
        "saw_sirf_frame_markers": bool(raw_capture.count(b"\xA0\xA2")),
        "nmea_sentence_prefixes": int(nmea_like),
        "likely_wire_protocol": "sirf_binary" if len(frames) > 0 else "nmea_text",
    }
    summary_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Captured {len(raw_capture)} bytes in {duration:.2f}s")
    print(f"Decoded {len(frames)} SiRF frames ({report['checksum_bad']} checksum errors)")
    print(f"Raw: {raw_path}")
    print(f"Summary: {summary_path}")
    if report["rough_location_hint"] is not None:
        rough = report["rough_location_hint"]
        print(
            "Rough location candidate:",
            f"{rough['latitude_deg']:.7f},{rough['longitude_deg']:.7f}",
            f"(mode={rough['mode']})",
        )
    else:
        print("No rough-location candidate present in captured binary packets.")
    if len(frames) == 0 and nmea_like > 0:
        print(
            "Observation: device stayed in NMEA text mode during probe; "
            "SiRF binary frames were not observed."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
