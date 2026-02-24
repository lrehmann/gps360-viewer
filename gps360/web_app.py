from __future__ import annotations

import argparse
import json
import queue
import socket
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from .driver import GPS360Driver
from .nmea import parse_gga, parse_gsa, parse_gsv, parse_nmea_sentence, parse_rmc
from .pl2303 import PL2303Driver


def _int_any_base(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the GPS-360 local GUI in a browser window "
            "(raw NMEA panel + OpenStreetMap panel)."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--transport",
        choices=("auto", "serial", "usb"),
        default="usb",
        help="Input transport (default: usb)",
    )
    parser.add_argument("--serial-port")
    parser.add_argument("--baud", type=int, default=4800)
    parser.add_argument("--usb-vid", type=_int_any_base, default=0x067B)
    parser.add_argument("--usb-pid", type=_int_any_base, default=0xAAA0)
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the GUI URL in the default browser",
    )
    return parser


class _EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[queue.Queue[str]] = set()
        self._last: Dict[str, Dict[str, object]] = {}

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=512)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._queues.discard(q)

    def snapshot(self) -> list[str]:
        with self._lock:
            items = list(self._last.items())
        return [self._format_event(event=event, payload=payload) for event, payload in items]

    def publish(self, event: str, payload: Dict[str, object]) -> None:
        payload["event_time"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        encoded = self._format_event(event=event, payload=payload)
        with self._lock:
            self._last[event] = dict(payload)
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(encoded)
            except queue.Full:
                try:
                    _ = q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(encoded)
                except queue.Full:
                    pass

    @staticmethod
    def _format_event(event: str, payload: Dict[str, object]) -> str:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return f"event: {event}\ndata: {data}\n\n"


class _GPSReader(threading.Thread):
    def __init__(
        self,
        events: _EventHub,
        stop_event: threading.Event,
        transport: str,
        baud: int,
        serial_port: Optional[str],
        usb_vid: int,
        usb_pid: int,
    ) -> None:
        super().__init__(name="gps360-reader", daemon=True)
        self.events = events
        self.stop_event = stop_event
        self.transport = transport
        self.baud = baud
        self.serial_port = serial_port
        self.usb_vid = usb_vid
        self.usb_pid = usb_pid
        self._state: Dict[str, object] = {}
        self._gsv_expected_parts: Optional[int] = None
        self._gsv_parts: Dict[int, list[dict[str, object]]] = {}

    def run(self) -> None:
        while not self.stop_event.is_set():
            driver = None
            source = ""
            try:
                driver, source = self._open_driver()
                self.events.publish(
                    "status",
                    {
                        "state": "connected",
                        "source": source,
                        "transport": self.transport,
                        "message": f"Connected ({source})",
                    },
                )

                while not self.stop_event.is_set():
                    line = driver.read_sentence(timeout=1.0)  # type: ignore[attr-defined]
                    if line is None:
                        continue
                    self._publish_sentence(line)
            except Exception as exc:
                self.events.publish(
                    "status",
                    {
                        "state": "error",
                        "source": source,
                        "transport": self.transport,
                        "message": str(exc),
                    },
                )
                if self.stop_event.wait(1.0):
                    break
            finally:
                if driver is not None:
                    try:
                        driver.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _open_driver(self) -> tuple[object, str]:
        if self.transport == "usb":
            driver = PL2303Driver(
                vendor_id=self.usb_vid,
                product_id=self.usb_pid,
                baud=self.baud,
            )
            driver.open()
            return driver, f"usb {self.usb_vid:04x}:{self.usb_pid:04x}"

        if self.transport == "serial":
            driver = GPS360Driver(port=self.serial_port, baud=self.baud)
            driver.open()
            return driver, f"serial {driver.port}"

        try:
            serial_driver = GPS360Driver(port=self.serial_port, baud=self.baud)
            serial_driver.open()
            return serial_driver, f"serial {serial_driver.port}"
        except Exception as serial_exc:
            self.events.publish(
                "status",
                {
                    "state": "notice",
                    "transport": self.transport,
                    "message": f"Serial unavailable: {serial_exc}. Falling back to USB.",
                },
            )
            usb_driver = PL2303Driver(
                vendor_id=self.usb_vid,
                product_id=self.usb_pid,
                baud=self.baud,
            )
            usb_driver.open()
            return usb_driver, f"usb {self.usb_vid:04x}:{self.usb_pid:04x}"

    def _publish_sentence(self, line: str) -> None:
        parsed = parse_nmea_sentence(line)
        sentence_payload: Dict[str, object] = {"line": line}
        if parsed is not None:
            sentence_payload["talker"] = parsed.talker
            sentence_payload["type"] = parsed.message_type
            sentence_payload["checksum_valid"] = parsed.checksum_valid
        self.events.publish("sentence", sentence_payload)

        if parsed is None or not parsed.checksum_valid:
            return

        update: Optional[Dict[str, object]] = None
        valid_fix: Optional[bool] = None

        if parsed.message_type == "GGA":
            update = parse_gga(parsed.fields)
            if update:
                valid_fix = bool(update.pop("is_valid_fix", False))
        elif parsed.message_type == "RMC":
            update = parse_rmc(parsed.fields)
            if update:
                valid_fix = bool(update.pop("is_valid_fix", False))
        elif parsed.message_type == "GSA":
            update = parse_gsa(parsed.fields)
            if update:
                valid_fix = bool(update.pop("is_valid_fix", False))
        elif parsed.message_type == "GSV":
            update = parse_gsv(parsed.fields)

        if not update:
            return

        if parsed.message_type in {"GGA", "RMC"}:
            assert valid_fix is not None
            self._merge_fix_update(update=update, valid_fix=valid_fix)
        elif parsed.message_type == "GSA":
            assert valid_fix is not None
            self._merge_gsa_update(update=update, valid_fix=valid_fix)
        elif parsed.message_type == "GSV":
            self._merge_gsv_update(update=update)

        payload = self._build_payload()
        self.events.publish("fix", payload)

    def _merge_fix_update(self, update: Dict[str, object], valid_fix: bool) -> None:
        self._state["valid_fix"] = valid_fix
        if valid_fix:
            for key, value in update.items():
                if value is not None:
                    self._state[key] = value
            return

        for key in ("timestamp_utc", "satellites", "hdop", "fix_quality"):
            value = update.get(key)
            if value is not None:
                self._state[key] = value

    def _merge_gsa_update(self, update: Dict[str, object], valid_fix: bool) -> None:
        self._state["valid_fix"] = valid_fix
        for key, value in update.items():
            if value is not None:
                self._state[key] = value

    def _merge_gsv_update(self, update: Dict[str, object]) -> None:
        total = _as_int(update.get("gsv_total_messages"))
        number = _as_int(update.get("gsv_message_number"))
        satellites = update.get("gsv_satellites")
        if not isinstance(satellites, list):
            satellites = []

        if total is None or number is None or number < 1:
            self._update_satellite_stats(satellites=satellites, sats_in_view=update.get("sats_in_view"))
            return

        if number == 1 or self._gsv_expected_parts != total:
            self._gsv_expected_parts = total
            self._gsv_parts = {}

        self._gsv_parts[number] = satellites  # type: ignore[assignment]

        merged: list[dict[str, object]] = []
        for idx in sorted(self._gsv_parts):
            merged.extend(self._gsv_parts[idx])

        self._update_satellite_stats(satellites=merged, sats_in_view=update.get("sats_in_view"))

    def _update_satellite_stats(self, satellites: list[dict[str, object]], sats_in_view: object) -> None:
        self._state["satellites_view"] = satellites
        if sats_in_view is not None:
            self._state["sats_in_view"] = _as_int(sats_in_view)
        else:
            self._state["sats_in_view"] = len(satellites)

        snrs = [
            _as_int(sat.get("snr_db"))
            for sat in satellites
            if isinstance(sat, dict) and _as_int(sat.get("snr_db")) is not None
        ]
        snr_values = [s for s in snrs if s is not None]

        if snr_values:
            self._state["snr_avg"] = round(sum(snr_values) / len(snr_values), 1)
            self._state["snr_max"] = max(snr_values)
            self._state["snr_tracked"] = len(snr_values)
        else:
            self._state["snr_avg"] = None
            self._state["snr_max"] = None
            self._state["snr_tracked"] = 0

    def _build_payload(self) -> Dict[str, object]:
        top_satellites = self._top_satellites(limit=6)
        return {
            "valid_fix": bool(self._state.get("valid_fix")),
            "latitude": self._state.get("latitude"),
            "longitude": self._state.get("longitude"),
            "altitude_m": self._state.get("altitude_m"),
            "speed_knots": self._state.get("speed_knots"),
            "course_deg": self._state.get("course_deg"),
            "satellites": self._state.get("satellites"),
            "hdop": self._state.get("hdop"),
            "fix_quality": self._state.get("fix_quality"),
            "timestamp_utc": self._format_timestamp(self._state.get("timestamp_utc")),
            "gsa_mode": self._state.get("gsa_mode"),
            "fix_type": self._state.get("fix_type"),
            "fix_type_label": _fix_type_label(_as_int(self._state.get("fix_type"))),
            "sats_in_view": self._state.get("sats_in_view"),
            "sats_used": self._state.get("sats_used"),
            "used_prns": self._state.get("used_prns"),
            "pdop": self._state.get("pdop"),
            "hdop_gsa": self._state.get("hdop_gsa"),
            "vdop": self._state.get("vdop"),
            "snr_avg": self._state.get("snr_avg"),
            "snr_max": self._state.get("snr_max"),
            "snr_tracked": self._state.get("snr_tracked"),
            "top_satellites": top_satellites,
            "satellites_view": self._satellite_view(limit=24),
        }

    def _top_satellites(self, limit: int = 6) -> list[dict[str, object]]:
        satellites = self._state.get("satellites_view")
        if not isinstance(satellites, list):
            return []

        ranked = []
        for sat in satellites:
            if not isinstance(sat, dict):
                continue
            snr = _as_int(sat.get("snr_db"))
            if snr is None:
                continue
            ranked.append(
                {
                    "prn": _as_int(sat.get("prn")),
                    "snr_db": snr,
                    "elevation_deg": _as_int(sat.get("elevation_deg")),
                    "azimuth_deg": _as_int(sat.get("azimuth_deg")),
                }
            )
        ranked.sort(key=lambda item: int(item["snr_db"]), reverse=True)
        return ranked[:limit]

    def _satellite_view(self, limit: int = 24) -> list[dict[str, object]]:
        satellites = self._state.get("satellites_view")
        if not isinstance(satellites, list):
            return []

        points = []
        for sat in satellites[:limit]:
            if not isinstance(sat, dict):
                continue
            points.append(
                {
                    "prn": _as_int(sat.get("prn")),
                    "elevation_deg": _as_int(sat.get("elevation_deg")),
                    "azimuth_deg": _as_int(sat.get("azimuth_deg")),
                    "snr_db": _as_int(sat.get("snr_db")),
                }
            )
        return points

    @staticmethod
    def _format_timestamp(value: object) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        return None


class _GuiServer:
    def __init__(
        self,
        host: str,
        port: int,
        transport: str,
        serial_port: Optional[str],
        baud: int,
        usb_vid: int,
        usb_pid: int,
    ) -> None:
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self.events = _EventHub()
        self.stop_event = threading.Event()
        self.reader = _GPSReader(
            events=self.events,
            stop_event=self.stop_event,
            transport=transport,
            baud=baud,
            serial_port=serial_port,
            usb_vid=usb_vid,
            usb_pid=usb_pid,
        )
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/":
                    self._serve_index()
                    return
                if self.path == "/events":
                    self._serve_events()
                    return
                if self.path == "/healthz":
                    self._send_text(HTTPStatus.OK, "ok\n")
                    return
                self._send_text(HTTPStatus.NOT_FOUND, "not found\n")

            def log_message(self, fmt: str, *args: object) -> None:
                _ = (fmt, args)

            def _serve_index(self) -> None:
                body = _index_html(outer.url)
                data = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_events(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                q = outer.events.subscribe()
                try:
                    for item in outer.events.snapshot():
                        self.wfile.write(item.encode("utf-8"))
                    self.wfile.flush()

                    while not outer.stop_event.is_set():
                        try:
                            item = q.get(timeout=1.0)
                            self.wfile.write(item.encode("utf-8"))
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    outer.events.unsubscribe(q)

            def _send_text(self, status: HTTPStatus, text: str) -> None:
                data = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def run(self) -> None:
        self.reader.start()
        self.httpd.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self.stop_event.set()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def _index_html(app_url: str) -> str:
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GPS-360 Viewer</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    crossorigin=""
  />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    :root {
      --bg: #f7f4ee;
      --panel: #fffefb;
      --ink: #1e1f24;
      --muted: #5d6773;
      --accent: #006f63;
      --border: #d6d0c5;
      --bad: #9e2a2b;
      --ok: #2d6a4f;
    }
    html, body {
      margin: 0;
      height: 100%;
      background: radial-gradient(circle at 20% 0%, #fff6dd 0%, var(--bg) 40%, #ece8df 100%);
      color: var(--ink);
      font-family: "SF Pro Display", "Segoe UI", -apple-system, sans-serif;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(360px, 38%) 1fr;
      gap: 14px;
      height: 100vh;
      padding: 14px;
      box-sizing: border-box;
    }
    .panel {
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
      background: var(--panel);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
    }
    .left {
      display: grid;
      grid-template-rows: auto auto auto 1fr;
    }
    .head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, #fffefb 0%, #f4efe4 100%);
    }
    .title {
      margin: 0 0 4px 0;
      font-size: 20px;
      letter-spacing: 0.2px;
    }
    .sub {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }
    .status-grid {
      padding: 12px 16px;
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 6px 10px;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .k {
      color: var(--muted);
      font-weight: 600;
    }
    .v {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      word-break: break-word;
    }
    .raw-wrap {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 0;
    }
    .sky-wrap {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 0;
      border-bottom: 1px solid var(--border);
    }
    #skyplot {
      width: 100%;
      height: 220px;
      display: block;
      background: #fdfbf7;
    }
    .raw-title {
      margin: 0;
      padding: 10px 16px;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.9px;
    }
    #raw {
      margin: 0;
      padding: 12px 16px;
      overflow: auto;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      line-height: 1.35;
      background: #fcfaf6;
      user-select: text;
    }
    .raw-line { margin: 0; }
    .map-wrap {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 0;
    }
    .map-head {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
    }
    .pill {
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #fff;
      font-weight: 600;
    }
    .pill.ok { color: var(--ok); border-color: #9ecab6; }
    .pill.bad { color: var(--bad); border-color: #d7a2a3; }
    #map {
      border: 0;
      width: 100%;
      height: 100%;
      min-height: 300px;
      background: #f5f5f5;
    }
    .hint {
      color: var(--muted);
      font-size: 12px;
    }
    @media (max-width: 1000px) {
      .shell {
        grid-template-columns: 1fr;
        grid-template-rows: 56% 44%;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel left">
      <div class="head">
        <h1 class="title">GPS-360 Monitor</h1>
        <p class="sub">Live NMEA on the left, OpenStreetMap view on the right.</p>
      </div>
      <div class="status-grid">
        <div class="k">Connection</div><div class="v" id="connection">starting...</div>
        <div class="k">Source</div><div class="v" id="source">n/a</div>
        <div class="k">UTC</div><div class="v" id="utc">n/a</div>
        <div class="k">Fix type</div><div class="v" id="fixtype">n/a</div>
        <div class="k">Fix readiness</div><div class="v" id="readiness">n/a</div>
        <div class="k">GSA mode</div><div class="v" id="gsamode">n/a</div>
        <div class="k">Latitude</div><div class="v" id="lat">n/a</div>
        <div class="k">Longitude</div><div class="v" id="lon">n/a</div>
        <div class="k">Altitude</div><div class="v" id="alt">n/a</div>
        <div class="k">Satellites (fix)</div><div class="v" id="sats">n/a</div>
        <div class="k">Satellites (view)</div><div class="v" id="satsview">n/a</div>
        <div class="k">Satellites used</div><div class="v" id="satsused">n/a</div>
        <div class="k">HDOP</div><div class="v" id="hdop">n/a</div>
        <div class="k">DOP (P/H/V)</div><div class="v" id="dop">n/a</div>
        <div class="k">Fix quality</div><div class="v" id="fixq">n/a</div>
        <div class="k">Signal (SNR)</div><div class="v" id="signal">n/a</div>
        <div class="k">Used PRNs</div><div class="v" id="prns">n/a</div>
        <div class="k">Top satellites</div><div class="v" id="topsnr">n/a</div>
        <div class="k">Speed</div><div class="v" id="speed">n/a</div>
        <div class="k">Course</div><div class="v" id="course">n/a</div>
      </div>
      <div class="sky-wrap">
        <p class="raw-title">Satellite Sky Plot</p>
        <canvas id="skyplot" width="640" height="220"></canvas>
      </div>
      <div class="raw-wrap">
        <p class="raw-title">Raw NMEA Stream</p>
        <div id="raw"></div>
      </div>
    </section>

    <section class="panel map-wrap">
      <div class="map-head">
        <div>
          <strong>OpenStreetMap</strong>
          <span class="hint" id="map-hint">Waiting for a valid GPS fix...</span>
        </div>
        <span class="pill bad" id="pill">NO FIX</span>
      </div>
      <div id="map" title="OpenStreetMap"></div>
    </section>
  </div>

  <script>
    const MAX_RAW_LINES = 300;
    let map = null;
    let marker = null;
    let trail = null;
    let hasCenteredMap = false;
    let mapUserInteracting = false;
    let lastPanAtMs = 0;
    let lastPanPosition = null;
    let uiPauseUntilMs = 0;
    let pendingStatus = null;
    let pendingFix = null;

    const el = (id) => document.getElementById(id);
    const setText = (id, value) => { el(id).textContent = value; };

    function leftPanelHasSelection() {
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return false;
      const left = document.querySelector(".left");
      if (!left) return false;
      const anchor = selection.anchorNode;
      const focus = selection.focusNode;
      return Boolean(
        (anchor && left.contains(anchor)) ||
        (focus && left.contains(focus))
      );
    }

    function shouldPauseUi() {
      if (Date.now() < uiPauseUntilMs) return true;
      return leftPanelHasSelection();
    }

    document.addEventListener("selectionchange", () => {
      if (leftPanelHasSelection()) {
        uiPauseUntilMs = Date.now() + 1800;
      }
    });

    document.addEventListener("mousedown", (event) => {
      const left = document.querySelector(".left");
      if (left && left.contains(event.target)) {
        uiPauseUntilMs = Date.now() + 400;
      }
    });

    function appendRaw(line) {
      const raw = el("raw");
      if (!raw) return;

      const row = document.createElement("div");
      row.className = "raw-line";
      row.textContent = line;
      raw.insertBefore(row, raw.firstChild);

      while (raw.childNodes.length > MAX_RAW_LINES) {
        raw.removeChild(raw.lastChild);
      }
    }

    function toKmh(knots) {
      return knots * 1.852;
    }

    function metersBetween(lat1, lon1, lat2, lon2) {
      const R = 6371000;
      const toRad = (deg) => (deg * Math.PI) / 180;
      const dLat = toRad(lat2 - lat1);
      const dLon = toRad(lon2 - lon1);
      const a =
        Math.sin(dLat / 2) * Math.sin(dLat / 2) +
        Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
        Math.sin(dLon / 2) * Math.sin(dLon / 2);
      const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      return R * c;
    }

    function initMap() {
      if (map) return true;
      if (!window.L) {
        setText("map-hint", "Map library not loaded.");
        return false;
      }

      map = L.map("map", { zoomControl: true });
      map.setView([47.6205, -122.3493], 13);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
      }).addTo(map);

      marker = L.circleMarker([47.6205, -122.3493], {
        radius: 6,
        color: "#1f6aa5",
        weight: 2,
        fillColor: "#4d8ec8",
        fillOpacity: 0.8,
      }).addTo(map);
      trail = L.polyline([], { color: "#1f6aa5", weight: 2, opacity: 0.65 }).addTo(map);

      map.on("movestart zoomstart", () => { mapUserInteracting = true; });
      map.on("moveend zoomend", () => { mapUserInteracting = false; });
      return true;
    }

    function drawSkyplot(satellites) {
      const canvas = el("skyplot");
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const w = canvas.width;
      const h = canvas.height;
      const cx = w / 2;
      const cy = h / 2;
      const r = Math.min(w, h) * 0.42;

      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = "#d2cabd";
      ctx.lineWidth = 1;
      for (const ring of [1, 2, 3]) {
        ctx.beginPath();
        ctx.arc(cx, cy, (r * ring) / 3, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.moveTo(cx - r, cy);
      ctx.lineTo(cx + r, cy);
      ctx.moveTo(cx, cy - r);
      ctx.lineTo(cx, cy + r);
      ctx.stroke();

      ctx.fillStyle = "#8b7f6e";
      ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillText("N", cx - 4, cy - r - 6);
      ctx.fillText("S", cx - 4, cy + r + 14);
      ctx.fillText("W", cx - r - 14, cy + 4);
      ctx.fillText("E", cx + r + 8, cy + 4);

      const points = Array.isArray(satellites) ? satellites : [];
      for (const sat of points) {
        const elev = Number.isFinite(sat.elevation_deg) ? sat.elevation_deg : null;
        const az = Number.isFinite(sat.azimuth_deg) ? sat.azimuth_deg : null;
        if (elev === null || az === null) continue;
        if (elev < 0 || elev > 90 || az < 0 || az > 360) continue;

        const rr = r * (1 - elev / 90);
        const theta = ((az - 90) * Math.PI) / 180;
        const x = cx + rr * Math.cos(theta);
        const y = cy + rr * Math.sin(theta);
        const snr = Number.isFinite(sat.snr_db) ? sat.snr_db : null;
        const color = snr === null ? "#7d8b99" : (snr >= 30 ? "#2d6a4f" : "#b36a2e");

        ctx.beginPath();
        ctx.fillStyle = color;
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
        const prn = sat.prn ?? "?";
        ctx.fillStyle = "#2a2f36";
        ctx.fillText(String(prn), x + 6, y - 5);
      }
    }

    function fixReadiness(payload) {
      if (payload.valid_fix) return "Position locked";
      const inView = Number(payload.sats_in_view ?? 0);
      const used = Number(payload.sats_used ?? 0);
      const tracked = Number(payload.snr_tracked ?? 0);
      if (used >= 3) return "Close: satellites selected, waiting for stable geometry";
      if (inView >= 4 && tracked > 0) return "Acquiring: visible satellites but not yet solved";
      if (inView > 0) return "Searching: satellites in view, low usable signal";
      return "Searching: no satellites decoded yet";
    }

    function updateMap(lat, lon) {
      if (!initMap()) return;
      const pos = [lat, lon];
      marker.setLatLng(pos);

      const points = trail.getLatLngs();
      points.push(pos);
      if (points.length > 1200) points.shift();
      trail.setLatLngs(points);

      const now = Date.now();
      const movedEnough = !lastPanPosition || metersBetween(
        lastPanPosition[0],
        lastPanPosition[1],
        lat,
        lon
      ) > 10;

      if (!hasCenteredMap) {
        map.setView(pos, 16);
        hasCenteredMap = true;
        lastPanAtMs = now;
      } else if (!mapUserInteracting && movedEnough && now - lastPanAtMs > 1200) {
        map.panTo(pos, { animate: true, duration: 0.4 });
        lastPanAtMs = now;
      }

      lastPanPosition = pos;
      setText("map-hint", `Tracking ${lat.toFixed(6)},${lon.toFixed(6)}`);
    }

    function applyStatus(payload) {
      setText("connection", payload.message || payload.state || "unknown");
      if (payload.source) setText("source", payload.source);
    }

    function onStatus(payload) {
      if (shouldPauseUi()) {
        pendingStatus = payload;
        return;
      }
      applyStatus(payload);
    }

    function onSentence(payload) {
      if (shouldPauseUi()) return;
      const label = payload.type ? `${payload.talker || ""}${payload.type}` : "RAW";
      const state = payload.checksum_valid === false ? "bad" : "ok";
      appendRaw(`[${label}:${state}] ${payload.line || ""}`);
    }

    function applyFix(payload) {
      const hasLat = typeof payload.latitude === "number";
      const hasLon = typeof payload.longitude === "number";
      const valid = Boolean(payload.valid_fix) && hasLat && hasLon;

      setText("utc", payload.timestamp_utc || "n/a");
      setText("fixtype", payload.fix_type_label || "n/a");
      setText("readiness", fixReadiness(payload));
      setText("gsamode", payload.gsa_mode || "n/a");
      setText("lat", hasLat ? payload.latitude.toFixed(6) : "n/a");
      setText("lon", hasLon ? payload.longitude.toFixed(6) : "n/a");
      setText("alt", typeof payload.altitude_m === "number" ? `${payload.altitude_m.toFixed(1)} m` : "n/a");
      setText("sats", payload.satellites ?? "n/a");
      setText("satsview", payload.sats_in_view ?? "n/a");
      setText("satsused", payload.sats_used ?? "n/a");
      setText("hdop", typeof payload.hdop === "number" ? payload.hdop.toFixed(1) : "n/a");
      const dopParts = [];
      if (typeof payload.pdop === "number") dopParts.push(`P:${payload.pdop.toFixed(1)}`);
      if (typeof payload.hdop_gsa === "number") dopParts.push(`H:${payload.hdop_gsa.toFixed(1)}`);
      if (typeof payload.vdop === "number") dopParts.push(`V:${payload.vdop.toFixed(1)}`);
      setText("dop", dopParts.length ? dopParts.join(" / ") : "n/a");
      setText("fixq", payload.fix_quality ?? "n/a");
      if (typeof payload.snr_avg === "number" || typeof payload.snr_max === "number") {
        const avg = typeof payload.snr_avg === "number" ? payload.snr_avg.toFixed(1) : "n/a";
        const max = typeof payload.snr_max === "number" ? payload.snr_max : "n/a";
        const tracked = payload.snr_tracked ?? 0;
        setText("signal", `avg ${avg} dB / max ${max} dB (${tracked} tracked)`);
      } else {
        setText("signal", "n/a");
      }
      const prns = Array.isArray(payload.used_prns) && payload.used_prns.length
        ? payload.used_prns.join(", ")
        : "n/a";
      setText("prns", prns);
      const top = Array.isArray(payload.top_satellites)
        ? payload.top_satellites
            .map((sat) => {
              const prn = sat.prn ?? "?";
              const snr = sat.snr_db ?? "?";
              return `PRN${prn}:${snr}dB`;
            })
            .join("  ")
        : "";
      setText("topsnr", top || "n/a");
      drawSkyplot(payload.satellites_view);
      setText("speed", typeof payload.speed_knots === "number" ? `${payload.speed_knots.toFixed(2)} kn / ${toKmh(payload.speed_knots).toFixed(2)} km/h` : "n/a");
      setText("course", typeof payload.course_deg === "number" ? `${payload.course_deg.toFixed(1)} deg` : "n/a");

      const pill = el("pill");
      if (valid) {
        pill.textContent = "FIX";
        pill.classList.remove("bad");
        pill.classList.add("ok");
        updateMap(payload.latitude, payload.longitude);
      } else {
        pill.textContent = "NO FIX";
        pill.classList.remove("ok");
        pill.classList.add("bad");
      }
    }

    function onFix(payload) {
      if (shouldPauseUi()) {
        pendingFix = payload;
        return;
      }
      applyFix(payload);
    }

    function flushPending() {
      if (shouldPauseUi()) return;
      if (pendingStatus) {
        applyStatus(pendingStatus);
        pendingStatus = null;
      }
      if (pendingFix) {
        applyFix(pendingFix);
        pendingFix = null;
      }
    }

    function start() {
      setText("source", "__APP_URL__");
      initMap();
      const events = new EventSource("/events");
      events.addEventListener("status", (ev) => onStatus(JSON.parse(ev.data)));
      events.addEventListener("sentence", (ev) => onSentence(JSON.parse(ev.data)));
      events.addEventListener("fix", (ev) => onFix(JSON.parse(ev.data)));
      events.onerror = () => {
        setText("connection", "disconnected, reconnecting...");
      };
      setInterval(flushPending, 200);
    }

    start();
  </script>
</body>
</html>
"""
    return template.replace("__APP_URL__", app_url)


def _port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def _as_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fix_type_label(fix_type: Optional[int]) -> str:
    if fix_type == 3:
        return "3D"
    if fix_type == 2:
        return "2D"
    if fix_type == 1:
        return "No Fix"
    return "n/a"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    url = f"http://{args.host}:{args.port}"

    if _port_in_use(args.host, args.port):
        if args.open_browser:
            webbrowser.open(url, new=1, autoraise=True)
        print(f"GPS GUI already running at {url}")
        return 0

    server = _GuiServer(
        host=args.host,
        port=args.port,
        transport=args.transport,
        serial_port=args.serial_port,
        baud=args.baud,
        usb_vid=args.usb_vid,
        usb_pid=args.usb_pid,
    )

    if args.open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url, new=1, autoraise=True)).start()

    print(f"GPS GUI running at {url}")
    try:
        server.run()
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
