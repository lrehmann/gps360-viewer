from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class NMEASentence:
    raw: str
    talker: str
    message_type: str
    fields: list[str]
    checksum_valid: bool


def parse_nmea_sentence(line: str) -> Optional[NMEASentence]:
    line = line.strip()
    if not line.startswith("$"):
        return None

    payload = line[1:]
    checksum_valid = True
    if "*" in payload:
        body, given_checksum = payload.split("*", 1)
        given_checksum = given_checksum[:2]
        checksum_valid = _checksum(body) == given_checksum.upper()
    else:
        body = payload

    parts = body.split(",")
    if not parts or len(parts[0]) < 5:
        return None

    sentence_id = parts[0]
    talker = sentence_id[:2]
    message_type = sentence_id[2:]
    return NMEASentence(
        raw=line,
        talker=talker,
        message_type=message_type,
        fields=parts[1:],
        checksum_valid=checksum_valid,
    )


def parse_gga(fields: list[str]) -> Dict[str, Any]:
    if len(fields) < 9:
        return {}

    fix_quality = _as_int(fields[5])
    satellites = _as_int(fields[6])
    hdop = _as_float(fields[7])
    altitude_m = _as_float(fields[8])
    latitude = parse_lat_lon(fields[1], fields[2])
    longitude = parse_lat_lon(fields[3], fields[4])

    return {
        "timestamp_utc": parse_utc(fields[0], None),
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": altitude_m,
        "fix_quality": fix_quality,
        "satellites": satellites,
        "hdop": hdop,
        "is_valid_fix": bool(fix_quality and fix_quality > 0),
    }


def parse_rmc(fields: list[str]) -> Dict[str, Any]:
    if len(fields) < 9:
        return {}

    status = fields[1].upper() if fields[1] else "V"
    latitude = parse_lat_lon(fields[2], fields[3])
    longitude = parse_lat_lon(fields[4], fields[5])
    speed_knots = _as_float(fields[6])
    course_deg = _as_float(fields[7])

    # When status is invalid ("V"), many receivers emit stale/garbage date
    # tokens. Ignore the date in that case to avoid timestamp jumps.
    date_token = fields[8] if status == "A" else None

    return {
        "timestamp_utc": parse_utc(fields[0], date_token),
        "latitude": latitude,
        "longitude": longitude,
        "speed_knots": speed_knots,
        "course_deg": course_deg,
        "is_valid_fix": status == "A",
    }


def parse_gsa(fields: list[str]) -> Dict[str, Any]:
    if len(fields) < 2:
        return {}

    mode = fields[0].upper() if fields[0] else None
    fix_type = _as_int(fields[1])
    used_prns = [prn for prn in (_as_int(v) for v in fields[2:14]) if prn is not None]
    pdop = _as_float(fields[14]) if len(fields) > 14 else None
    hdop = _as_float(fields[15]) if len(fields) > 15 else None
    vdop = _as_float(fields[16]) if len(fields) > 16 else None

    return {
        "gsa_mode": mode,
        "fix_type": fix_type,
        "used_prns": used_prns,
        "sats_used": len(used_prns),
        "pdop": pdop,
        "hdop_gsa": hdop,
        "vdop": vdop,
        "is_valid_fix": bool(fix_type and fix_type > 1),
    }


def parse_gsv(fields: list[str]) -> Dict[str, Any]:
    if len(fields) < 3:
        return {}

    total_messages = _as_int(fields[0])
    message_number = _as_int(fields[1])
    sats_in_view = _as_int(fields[2])

    satellites = []
    idx = 3
    while idx + 3 < len(fields):
        satellites.append(
            {
                "prn": _as_int(fields[idx]),
                "elevation_deg": _as_int(fields[idx + 1]),
                "azimuth_deg": _as_int(fields[idx + 2]),
                "snr_db": _as_int(fields[idx + 3]),
            }
        )
        idx += 4

    return {
        "gsv_total_messages": total_messages,
        "gsv_message_number": message_number,
        "sats_in_view": sats_in_view,
        "gsv_satellites": satellites,
    }


def parse_lat_lon(value: str, hemisphere: str) -> Optional[float]:
    if not value or not hemisphere:
        return None
    if "." not in value:
        return None

    whole, frac = value.split(".", 1)
    if len(whole) < 3:
        return None

    deg_part = whole[:-2]
    min_part = whole[-2:] + "." + frac
    try:
        degrees = float(deg_part)
        minutes = float(min_part)
    except ValueError:
        return None

    decimal = degrees + (minutes / 60.0)
    hemisphere = hemisphere.upper()
    if hemisphere in ("S", "W"):
        decimal = -decimal
    return decimal


def parse_utc(time_token: str, date_token: Optional[str]) -> Optional[datetime]:
    if len(time_token) < 6:
        return None
    try:
        hour = int(time_token[0:2])
        minute = int(time_token[2:4])
        second_float = float(time_token[4:])
        second = int(second_float)
        microsecond = int(round((second_float - second) * 1_000_000))
    except ValueError:
        return None

    now_utc = datetime.now(timezone.utc)

    if date_token and len(date_token) == 6:
        try:
            day = int(date_token[0:2])
            month = int(date_token[2:4])
            year_2 = int(date_token[4:6])
        except ValueError:
            return None
        year = 2000 + year_2 if year_2 < 80 else 1900 + year_2
        # Some legacy SiRF receivers emit stale dates (e.g. 2006) even while
        # producing fresh time-of-day and valid-looking fixes. If the date is
        # clearly implausible relative to "now", ignore it to keep timestamps
        # stable in live views.
        try:
            candidate = datetime(year, month, day, tzinfo=timezone.utc)
            if abs((candidate - now_utc).days) > 366:
                year = now_utc.year
                month = now_utc.month
                day = now_utc.day
        except ValueError:
            return None
    else:
        year = now_utc.year
        month = now_utc.month
        day = now_utc.day

    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            microsecond,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _checksum(payload: str) -> str:
    value = 0
    for ch in payload.encode("ascii", "ignore"):
        value ^= ch
    return f"{value:02X}"


def _as_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
