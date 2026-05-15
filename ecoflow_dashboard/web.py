from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, Response, request

from . import __version__
from .controls import DELTA_PRO_COMMANDS, SHP_COMMANDS, DeviceController
from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

_latest_version_cache: str = ""
_latest_version_ts: float = 0


def _get_latest_version() -> str:
    global _latest_version_cache, _latest_version_ts
    now = _time.time()
    if now - _latest_version_ts < 3600:  # cache 1 hour
        return _latest_version_cache
    try:
        import requests
        r = requests.get(
            "https://api.github.com/repos/WIN365ru/ecoflow-dashboard/releases/latest",
            timeout=5,
        )
        if r.ok:
            _latest_version_cache = r.json().get("tag_name", "").lstrip("v")
            _latest_version_ts = now
    except Exception:
        pass
    return _latest_version_cache


# Global references set by run_web()
_mqtt: EcoFlowMqttClient | None = None
_device_types: dict[str, str] = {}
_device_names: dict[str, str] = {}
_controller: DeviceController | None = None
_db_path: str = ""
_alerter: object | None = None
_energy_rate: float = 0.0
_energy_rate_night: float = 0.0
_energy_day_start: int = 7
_energy_day_end: int = 23
_energy_currency: str = "$"


def _get_current_rate() -> float:
    if not _energy_rate_night:
        return _energy_rate
    h = datetime.now().hour
    if _energy_day_start <= _energy_day_end:
        is_day = _energy_day_start <= h < _energy_day_end
    else:
        is_day = h >= _energy_day_start or h < _energy_day_end
    return _energy_rate if is_day else _energy_rate_night
_circuit_names: list[str] | None = None

# Live data ring buffer: {sn: deque of {ts, key: value, ...}}
_live_buffer: dict[str, deque] = {}
_LIVE_MAX = 1800  # 1 hour at 2s intervals

app = Flask(__name__)


@app.route("/")
def index() -> str:
    return HTML_PAGE


@app.route("/manifest.json")
def manifest() -> Response:
    return Response(json.dumps({
        "name": "EcoFlow Dashboard",
        "short_name": "EcoFlow",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }), content_type="application/json")


@app.route("/icon.svg")
def icon_svg() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="192" height="192">'
        '<rect width="100" height="100" rx="20" fill="#0d1117"/>'
        '<text x="50" y="50" text-anchor="middle" dominant-baseline="central" font-size="64">⚡</text>'
        '</svg>'
    )
    return Response(svg, content_type="image/svg+xml")


@app.route("/api/devices")
def api_devices() -> Response:
    if not _mqtt:
        return Response("{}", content_type="application/json")
    result = {}
    for sn, dtype in _device_types.items():
        data = _mqtt.get_device_data(sn)
        # Convert all values to JSON-serializable types
        clean = {}
        for k, v in data.items():
            if isinstance(v, (int, float, str, bool, type(None))):
                clean[k] = v
            else:
                clean[k] = str(v)
        age = _mqtt.last_update_age(sn)
        # JSON can't serialize float('inf') — clamp to a sentinel.
        if age == float("inf"):
            age = -1
        result[sn] = {
            "type": dtype,
            "name": _device_names.get(sn, sn),
            "data": clean,
            "data_age_sec": int(age),
        }
    return Response(
        json.dumps({"connected": _mqtt.connected, "version": __version__,
                    "latest_version": _get_latest_version(),
                    "telegram": {"enabled": _alerter is not None,
                                 "connected": getattr(_alerter, "connected", False)} if _alerter else None,
                    "energy": {
                        "rate": _energy_rate, "rate_night": _energy_rate_night,
                        "day_start": _energy_day_start, "day_end": _energy_day_end,
                        "currency": _energy_currency,
                        "current_rate": _get_current_rate(),
                    } if _energy_rate > 0 else None,
                    "circuit_names": _circuit_names,
                    "devices": result}),
        content_type="application/json",
    )


@app.route("/api/command", methods=["POST"])
def api_command() -> Response:
    if not _mqtt or not _controller:
        return Response(json.dumps({"error": "not ready"}), status=503, content_type="application/json")
    body = request.get_json(silent=True) or {}
    sn = body.get("sn", "")
    key = body.get("key", "")
    if not sn or not key:
        return Response(json.dumps({"error": "sn and key required"}), status=400, content_type="application/json")
    result = _controller.handle_key(key, sn)
    return Response(json.dumps({"result": result or "no action"}), content_type="application/json")


@app.route("/api/circuit-toggle", methods=["POST"])
def api_circuit_toggle() -> Response:
    """Toggle a specific SHP circuit on/off."""
    if not _mqtt:
        return Response(json.dumps({"error": "not ready"}), status=503, content_type="application/json")
    body = request.get_json(silent=True) or {}
    sn = body.get("sn", "")
    ch = body.get("circuit")
    if not sn or ch is None:
        return Response(json.dumps({"error": "sn and circuit required"}), status=400, content_type="application/json")
    ch = int(ch)
    if ch < 0 or ch > 11:
        return Response(json.dumps({"error": "circuit must be 0-11"}), status=400, content_type="application/json")
    data = _mqtt.get_device_data(sn)
    mode = data.get(f"loadCmdChCtrlInfos.{ch}.ctrlMode", 0)
    try:
        mode = int(float(mode))
    except (TypeError, ValueError):
        mode = 0
    if mode == 1:
        # Manual → restore to Auto
        _mqtt.send_command(sn, {"cmdSet": 11, "id": 16, "ch": ch, "ctrlMode": 0, "sta": 0})
        result = f"Circuit {ch+1} → Auto"
    else:
        # Auto → Manual OFF
        _mqtt.send_command(sn, {"cmdSet": 11, "id": 16, "ch": ch, "ctrlMode": 1, "sta": 1})
        result = f"Circuit {ch+1} → OFF"
    return Response(json.dumps({"result": result}), content_type="application/json")


@app.route("/api/live")
def api_live() -> Response:
    """Return live ring buffer data for charts."""
    sn = request.args.get("sn", "")
    if sn and sn in _live_buffer:
        points = list(_live_buffer[sn])
    else:
        points = []
    return Response(json.dumps(points), content_type="application/json")


@app.route("/api/history")
def api_history() -> Response:
    """Query historical data from SQLite."""
    if not _db_path:
        return Response(json.dumps([]), content_type="application/json")
    sn = request.args.get("sn", "")
    key = request.args.get("key", "")
    hours = request.args.get("hours", "")
    start = request.args.get("start", "")  # ISO date: 2026-03-01
    end = request.args.get("end", "")      # ISO date: 2026-03-26
    if not sn:
        return Response(json.dumps({"error": "sn required"}), status=400, content_type="application/json")

    # Build time filter: custom range takes priority over hours
    if start and end:
        time_clause = "AND timestamp >= ? AND timestamp <= ?"
        time_params = (start, end + "T23:59:59")
    elif start:
        time_clause = "AND timestamp >= ?"
        time_params = (start,)
    elif hours:
        cutoff = (datetime.now() - timedelta(hours=int(hours))).isoformat(timespec="seconds")
        time_clause = "AND timestamp >= ?"
        time_params = (cutoff,)
    else:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        time_clause = "AND timestamp >= ?"
        time_params = (cutoff,)

    try:
        with sqlite3.connect(_db_path) as conn:
            if key:
                rows = conn.execute(
                    f"SELECT timestamp, value FROM snapshots WHERE device_sn=? AND key=? "
                    f"{time_clause} ORDER BY timestamp",
                    (sn, key, *time_params),
                ).fetchall()
                return Response(
                    json.dumps([{"ts": r[0], "v": r[1]} for r in rows]),
                    content_type="application/json",
                )
            else:
                # Return all keys for this device in the time range
                rows = conn.execute(
                    f"SELECT timestamp, key, value FROM snapshots WHERE device_sn=? "
                    f"{time_clause} ORDER BY timestamp",
                    (sn, *time_params),
                ).fetchall()
                # Group by timestamp
                result: dict[str, dict] = {}
                for ts, k, v in rows:
                    if ts not in result:
                        result[ts] = {"ts": ts}
                    result[ts][k] = v
                return Response(
                    json.dumps(list(result.values())),
                    content_type="application/json",
                )
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, content_type="application/json")


@app.route("/api/history/range")
def api_history_range() -> Response:
    """Return available data date range per device."""
    if not _db_path:
        return Response(json.dumps({}), content_type="application/json")
    try:
        with sqlite3.connect(_db_path) as conn:
            result = {}
            for sn in _device_types:
                row = conn.execute(
                    "SELECT MIN(timestamp), MAX(timestamp), COUNT(DISTINCT timestamp) "
                    "FROM snapshots WHERE device_sn=?",
                    (sn,),
                ).fetchone()
                if row and row[0]:
                    result[sn] = {"start": row[0][:10], "end": row[1][:10], "snapshots": row[2]}
            return Response(json.dumps(result), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, content_type="application/json")


@app.route("/api/degradation")
def api_degradation() -> Response:
    """SOH% over time for battery degradation tracking."""
    if not _db_path:
        return Response(json.dumps([]), content_type="application/json")
    sn = request.args.get("sn", "")
    if not sn:
        return Response(json.dumps({"error": "sn required"}), status=400, content_type="application/json")
    try:
        with sqlite3.connect(_db_path) as conn:
            # Get daily SOH readings (one per day, averaged)
            rows = conn.execute(
                "SELECT DATE(timestamp) as day, AVG(value) as soh, MIN(value), MAX(value) "
                "FROM snapshots WHERE device_sn=? AND key='bmsMaster.soh' "
                "AND value > 0 GROUP BY day ORDER BY day",
                (sn,),
            ).fetchall()
            # Also get cycle count progression
            cycles = conn.execute(
                "SELECT DATE(timestamp) as day, MAX(value) as cycles "
                "FROM snapshots WHERE device_sn=? AND key='bmsMaster.cycles' "
                "GROUP BY day ORDER BY day",
                (sn,),
            ).fetchall()
            cycle_map = {r[0]: r[1] for r in cycles}

            result = []
            for day, soh_avg, soh_min, soh_max in rows:
                result.append({
                    "date": day,
                    "soh": round(soh_avg, 1),
                    "soh_min": round(soh_min, 1),
                    "soh_max": round(soh_max, 1),
                    "cycles": cycle_map.get(day, 0),
                })

            # Predict replacement: linear regression on SOH
            prediction = None
            if len(result) >= 7:
                soh_values = [r["soh"] for r in result]
                n = len(soh_values)
                if soh_values[0] > soh_values[-1]:  # degrading
                    daily_drop = (soh_values[0] - soh_values[-1]) / n
                    if daily_drop > 0:
                        current = soh_values[-1]
                        days_to_80 = max(0, (current - 80) / daily_drop) if current > 80 else 0
                        days_to_70 = max(0, (current - 70) / daily_drop) if current > 70 else 0
                        prediction = {
                            "daily_drop": round(daily_drop, 4),
                            "days_to_80pct": int(days_to_80),
                            "days_to_70pct": int(days_to_70),
                        }

            return Response(
                json.dumps({"data": result, "prediction": prediction}),
                content_type="application/json",
            )
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, content_type="application/json")


@app.route("/api/outages")
def api_outages() -> Response:
    """Power outage history."""
    if not _db_path:
        return Response(json.dumps([]), content_type="application/json")
    sn = request.args.get("sn", "")
    limit = int(request.args.get("limit", "50"))
    try:
        with sqlite3.connect(_db_path) as conn:
            if sn:
                rows = conn.execute(
                    "SELECT * FROM outages WHERE device_sn=? ORDER BY start_time DESC LIMIT ?",
                    (sn, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outages ORDER BY start_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            cols = ["id", "device_sn", "start_time", "end_time", "duration_sec",
                    "soc_start", "soc_end", "soc_used", "peak_load", "avg_load"]
            result = [dict(zip(cols, r)) for r in rows]
            return Response(json.dumps(result), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, content_type="application/json")


@app.route("/api/export/csv")
def api_export_csv() -> Response:
    """Export historical data as CSV."""
    if not _db_path:
        return Response("No database", status=404)
    sn = request.args.get("sn", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    hours = request.args.get("hours", "24")
    if not sn:
        return Response("sn required", status=400)

    if start and end:
        time_clause = "AND timestamp >= ? AND timestamp <= ?"
        time_params = (start, end + "T23:59:59")
    else:
        cutoff = (datetime.now() - timedelta(hours=int(hours))).isoformat(timespec="seconds")
        time_clause = "AND timestamp >= ?"
        time_params = (cutoff,)

    try:
        with sqlite3.connect(_db_path) as conn:
            rows = conn.execute(
                f"SELECT timestamp, key, value FROM snapshots WHERE device_sn=? "
                f"{time_clause} ORDER BY timestamp",
                (sn, *time_params),
            ).fetchall()

        # Pivot: group by timestamp, keys as columns
        data: dict[str, dict] = {}
        all_keys: set[str] = set()
        for ts, key, val in rows:
            if ts not in data:
                data[ts] = {"timestamp": ts}
            data[ts][key] = val
            all_keys.add(key)

        cols = ["timestamp"] + sorted(all_keys)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for ts in sorted(data):
            writer.writerow(data[ts])

        fname = f"ecoflow_{sn[-6:]}_{start or 'last' + hours + 'h'}.csv"
        return Response(
            output.getvalue(),
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return Response(str(e), status=500)


@app.route("/api/solar")
def api_solar() -> Response:
    """Solar analytics: daily generation, self-consumption, payback."""
    if not _db_path:
        return Response(json.dumps([]), content_type="application/json")
    sn = request.args.get("sn", "")
    days = int(request.args.get("days", "30"))
    if not sn:
        return Response(json.dumps({"error": "sn required"}), status=400, content_type="application/json")

    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(_db_path) as conn:
            # Daily solar generation (average power × samples = energy estimate)
            solar_rows = conn.execute(
                "SELECT DATE(timestamp) as day, AVG(value) as avg_w, COUNT(*) as samples "
                "FROM snapshots WHERE device_sn=? AND key='mppt.inWatts' "
                "AND timestamp >= ? AND value > 0 GROUP BY day ORDER BY day",
                (sn, cutoff),
            ).fetchall()

            # Lifetime solar
            lifetime = conn.execute(
                "SELECT MAX(value) FROM snapshots WHERE device_sn=? AND key='pd.chgSunPower'",
                (sn,),
            ).fetchone()
            lifetime_kwh = (lifetime[0] or 0) / 1000

            # Grid consumption for same period (from SHP if available)
            grid_rows = conn.execute(
                "SELECT DATE(timestamp) as day, MAX(value) as grid_wh "
                "FROM snapshots WHERE key='gridDayWatth' "
                "AND timestamp >= ? GROUP BY day ORDER BY day",
                (cutoff,),
            ).fetchall()
            grid_map = {r[0]: r[1] / 1000 for r in grid_rows}  # kWh

            # Calculate daily data
            log_interval = 300  # default 5 min
            daily = []
            total_solar_kwh = 0
            total_grid_kwh = 0
            for day, avg_w_raw, samples in solar_rows:
                # avg_w_raw is in deciWatts, convert to watts
                avg_w = avg_w_raw / 10
                # Energy = avg_watts × hours_of_samples
                solar_kwh = avg_w * samples * log_interval / 3_600_000  # W × s → kWh
                grid_kwh = grid_map.get(day, 0)
                total_solar_kwh += solar_kwh
                total_grid_kwh += grid_kwh
                self_ratio = solar_kwh / (solar_kwh + grid_kwh) * 100 if (solar_kwh + grid_kwh) > 0 else 0
                daily.append({
                    "date": day,
                    "solar_kwh": round(solar_kwh, 2),
                    "grid_kwh": round(grid_kwh, 2),
                    "self_consumption": round(self_ratio, 1),
                })

            money_saved = total_solar_kwh * _energy_rate if _energy_rate > 0 else 0

            return Response(json.dumps({
                "daily": daily,
                "total_solar_kwh": round(total_solar_kwh, 2),
                "total_grid_kwh": round(total_grid_kwh, 2),
                "lifetime_solar_kwh": round(lifetime_kwh, 2),
                "money_saved": round(money_saved, 2),
                "currency": _energy_currency,
                "self_consumption_avg": round(
                    total_solar_kwh / (total_solar_kwh + total_grid_kwh) * 100
                    if (total_solar_kwh + total_grid_kwh) > 0 else 0, 1
                ),
            }), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, content_type="application/json")


def _live_collector() -> None:
    """Background thread collecting live data points every 2 seconds."""
    while True:
        if _mqtt:
            for sn in _device_types:
                data = _mqtt.get_device_data(sn)
                point = {"ts": int(datetime.now().timestamp() * 1000)}
                # Collect key metrics
                for k in [
                    "ems.lcdShowSoc", "bmsMaster.soc", "bmsMaster.soh",
                    "pd.wattsInSum", "pd.wattsOutSum", "mppt.inWatts",
                    "inv.inputWatts", "inv.outputWatts", "pd.carWatts",
                    "bmsMaster.temp", "bmsMaster.vol", "bmsMaster.amp",
                    "backupBatPer", "gridDayWatth", "backupDayWatth",
                    *[f"infoList.{i}.chWatt" for i in range(12)],
                ]:
                    v = data.get(k)
                    if v is not None:
                        try:
                            point[k] = round(float(v), 2)
                        except (TypeError, ValueError):
                            pass
                if sn not in _live_buffer:
                    _live_buffer[sn] = deque(maxlen=_LIVE_MAX)
                _live_buffer[sn].append(point)
        _time.sleep(2)


def run_web(
    mqtt_client: EcoFlowMqttClient,
    device_types: dict[str, str],
    device_names: dict[str, str],
    port: int = 5000,
    db_path: str = "",
    alerter: object | None = None,
    energy_rate: float = 0.0,
    energy_rate_night: float = 0.0,
    energy_day_start: int = 7,
    energy_day_end: int = 23,
    energy_currency: str = "$",
    circuit_names: list[str] | None = None,
) -> None:
    global _mqtt, _device_types, _device_names, _controller, _db_path, _alerter
    global _energy_rate, _energy_rate_night, _energy_day_start, _energy_day_end, _energy_currency, _circuit_names
    _mqtt = mqtt_client
    _device_types = device_types
    _device_names = device_names
    _controller = DeviceController(mqtt_client, device_types)
    _db_path = db_path
    _alerter = alerter
    _energy_rate = energy_rate
    _energy_rate_night = energy_rate_night
    _energy_day_start = energy_day_start
    _energy_day_end = energy_day_end
    _energy_currency = energy_currency
    _circuit_names = circuit_names

    # Start live data collector
    collector = threading.Thread(target=_live_collector, daemon=True)
    collector.start()

    log.info("Starting web dashboard on http://0.0.0.0:%d", port)

    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("0.0.0.0", port, app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ---------------------------------------------------------------------------
# Inline HTML — self-contained dashboard page
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>EcoFlow Dashboard</title>
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-title" content="EcoFlow">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="manifest" href="/manifest.json">
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922;
          --cyan: #58a6ff; --blue: #1f6feb; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
         font-size: 13px; background: var(--bg); color: var(--text);
         padding: 8px; min-height: 100vh; }
  .header { display: flex; align-items: center; gap: 12px; padding: 8px 0;
            flex-wrap: wrap; }
  .header h1 { font-size: 16px; font-weight: 700; }
  .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge-green { background: #238636; color: #fff; }
  .badge-red { background: #da3633; color: #fff; }
  .badge-dim { background: var(--border); color: var(--dim); }
  .update-banner { background: #3d2e00; border: 1px solid var(--yellow);
                   padding: 6px 12px; border-radius: 6px; margin: 6px 0;
                   color: var(--yellow); font-size: 12px; }
  .grid { display: grid; gap: 8px; margin-top: 8px; }
  @media (min-width: 768px) { .grid-2 { grid-template-columns: 1fr 1fr; } }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 12px; }
  .card-title { font-size: 14px; font-weight: 700; margin-bottom: 8px;
                display: flex; align-items: center; gap: 8px; }
  .soc { font-size: 28px; font-weight: 800; }
  .soc-green { color: var(--green); }
  .soc-yellow { color: var(--yellow); }
  .soc-red { color: var(--red); }
  .bar-bg { background: var(--border); border-radius: 4px; height: 8px;
            margin: 6px 0; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .bar-green { background: var(--green); }
  .bar-yellow { background: var(--yellow); }
  .bar-red { background: var(--red); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px;
           font-size: 12px; margin-top: 8px; }
  .stat-row { display: contents; }
  .stat-row:nth-child(even) .stat-label,
  .stat-row:nth-child(even) .stat-value { background: rgba(255,255,255,0.03); }
  .stat-label { color: var(--dim); padding: 3px 4px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .stat-value { text-align: right; padding: 3px 4px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .stat-green { color: var(--green); }
  .stat-red { color: var(--red); }
  .circuits { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  .circuits th { text-align: left; color: var(--dim); font-weight: 400;
                 padding: 4px 6px; border-bottom: 1px solid var(--border); }
  .circuits td { padding: 4px 6px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .circuits tr:nth-child(even) { background: rgba(255,255,255,0.03); }
  .circuits .power { text-align: right; font-weight: 600; }
  .circuits .dp-row { color: var(--cyan); }
  .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .btn { padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border);
         background: var(--card); color: var(--text); font-family: inherit;
         font-size: 12px; cursor: pointer; transition: all 0.15s; }
  .btn:hover { background: var(--border); }
  .btn:active { transform: scale(0.95); }
  .btn-on { border-color: var(--green); color: var(--green); }
  .btn-off { border-color: var(--dim); color: var(--dim); }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: var(--green); color: #000; padding: 8px 20px;
           border-radius: 8px; font-weight: 600; opacity: 0;
           transition: opacity 0.3s; z-index: 100; }
  .toast.show { opacity: 1; }
  .tab-btn.active { border-color: var(--cyan); color: var(--cyan); }
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  @media (max-width: 600px) { .chart-grid { grid-template-columns: 1fr; } }
  .subtitle { color: var(--dim); font-size: 11px; margin-top: 4px; }
  .health { font-size: 11px; margin-top: 4px; }
  .health-green { color: var(--green); }
  .health-yellow { color: var(--yellow); }
  .health-red { color: var(--red); }
  .section-title { color: var(--dim); font-size: 11px; text-transform: uppercase;
                   letter-spacing: 1px; margin-top: 10px; margin-bottom: 4px; }
</style>
</head>
<body>
<div class="header">
  <h1>EcoFlow Dashboard</h1>
  <span class="badge badge-dim" id="version"></span>
  <span class="badge" id="mqtt-badge">--</span>
  <span class="badge" id="tg-badge" style="display:none">--</span>
  <span style="color:var(--dim);font-size:12px" id="clock"></span>
</div>
<div id="update-banner"></div>
<div id="dashboard"></div>

<div class="card" style="margin-top:8px">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
    <span class="card-title" style="margin:0">Charts</span>
    <button class="btn tab-btn active" onclick="setChartMode('live')">Live (1h)</button>
    <button class="btn tab-btn" onclick="setChartMode('history')">History</button>
    <select id="hist-hours" class="btn" style="display:none" onchange="histRangeChanged()">
      <option value="1">1 hour</option><option value="6">6 hours</option>
      <option value="24" selected>24 hours</option><option value="72">3 days</option>
      <option value="168">7 days</option><option value="336">14 days</option>
      <option value="720">30 days</option><option value="2160">90 days</option>
      <option value="8760">1 year</option><option value="custom">Custom range...</option>
    </select>
    <span id="custom-range" style="display:none">
      <input type="date" id="hist-start" class="btn" style="color:#e6edf3;background:#21262d;border:1px solid #30363d">
      <span style="color:#8b949e">to</span>
      <input type="date" id="hist-end" class="btn" style="color:#e6edf3;background:#21262d;border:1px solid #30363d">
      <button class="btn" onclick="loadHistory()" style="background:#238636">Go</button>
    </span>
    <button id="csv-btn" class="btn" style="display:none" onclick="exportCSV()">📥 CSV</button>
    <span id="hist-info" style="display:none;color:#8b949e;font-size:0.8em"></span>
    <select id="chart-device" class="btn" onchange="chartDeviceChanged()"></select>
  </div>
  <div class="chart-grid">
    <div><canvas id="chart-soc" height="150"></canvas></div>
    <div><canvas id="chart-power" height="150"></canvas></div>
  </div>
  <div style="margin-top:8px">
    <canvas id="chart-circuits" height="120"></canvas>
  </div>
</div>

<!-- Battery Degradation -->
<div class="card" style="margin-top:8px">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
    <span class="card-title" style="margin:0">Battery Health</span>
    <select id="deg-device" class="btn" onchange="loadDegradation()"></select>
  </div>
  <div id="deg-prediction" style="font-size:12px;color:var(--dim);margin-bottom:8px"></div>
  <canvas id="chart-degradation" height="150"></canvas>
</div>

<!-- Power Outage Log -->
<div class="card" style="margin-top:8px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
    <span class="card-title" style="margin:0">Power Outage Log</span>
  </div>
  <div id="outage-table" style="font-size:12px"></div>
</div>

<!-- Energy Flow -->
<div class="card" style="margin-top:8px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
    <span class="card-title" style="margin:0">Energy Flow</span>
  </div>
  <canvas id="chart-flow" height="200"></canvas>
</div>

<!-- Solar Analytics -->
<div class="card" style="margin-top:8px" id="solar-card">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
    <span class="card-title" style="margin:0">☀ Solar Analytics</span>
    <select id="solar-device" class="btn" onchange="loadSolar()"></select>
    <select id="solar-days" class="btn" onchange="loadSolar()">
      <option value="7">7 days</option><option value="30" selected>30 days</option>
      <option value="90">90 days</option><option value="365">1 year</option>
    </select>
  </div>
  <div id="solar-summary" style="font-size:12px;margin-bottom:8px"></div>
  <canvas id="chart-solar" height="150"></canvas>
</div>

<div class="toast" id="toast"></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const $ = s => document.querySelector(s);

function socColor(v) { return v >= 60 ? 'green' : v >= 20 ? 'yellow' : 'red'; }
function fmtW(w) { return Math.abs(w) >= 1000 ? (w/1000).toFixed(1)+' kW' : Math.round(w)+' W'; }
function fmtWh(wh) { return wh >= 1000 ? (wh/1000).toFixed(2)+' kWh' : Math.round(wh)+' Wh'; }
function fmtTime(m) {
  m = Math.round(m);
  if (m <= 0) return '--';
  if (m >= 1440) return Math.floor(m/1440)+'d '+Math.floor((m%1440)/60)+'h';
  return Math.floor(m/60)+'h '+m%60+'m';
}
function fmtVer(n) {
  n = Math.round(n);
  if (n <= 0) return '--';
  return ((n>>24)&0xFF)+'.'+((n>>16)&0xFF)+'.'+((n>>8)&0xFF)+'.'+(n&0xFF);
}
function g(d, ...keys) {
  for (const k of keys) { const v = d[k]; if (v !== undefined && v !== null && v !== 0) return Number(v); }
  for (const k of keys) { const v = d[k]; if (v !== undefined && v !== null) return Number(v); }
  return 0;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

async function sendCmd(sn, key) {
  try {
    const r = await fetch('/api/command', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({sn, key})
    });
    const j = await r.json();
    toast(j.result || 'sent');
  } catch(e) { toast('error: '+e.message); }
}

async function toggleCircuit(sn, ch) {
  try {
    const r = await fetch('/api/circuit-toggle', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({sn, circuit: ch})
    });
    const j = await r.json();
    toast(j.result || 'toggled');
  } catch(e) { toast('error: '+e.message); }
}

function buildDeltaPro(sn, name, d) {
  const soc = g(d,'ems.lcdShowSoc','ems.f32LcdShowSoc','bmsMaster.f32ShowSoc','bmsMaster.soc');
  const soh = g(d,'bmsMaster.soh');
  const c = socColor(soc);
  const totalIn = g(d,'pd.wattsInSum');
  const totalOut = g(d,'pd.wattsOutSum');
  const acOut = g(d,'inv.outputWatts');
  const solarIn = g(d,'mppt.inWatts') / 10; // raw is deciWatts
  const acIn = g(d,'inv.inputWatts');
  const car = g(d,'pd.carWatts');
  const volts = g(d,'bmsMaster.vol')/1000;
  const amps = g(d,'bmsMaster.amp');
  let current = Math.abs(amps) > 100 ? amps/1000 : amps;
  current = Math.max(-35, Math.min(35, current)); // clamp to realistic range
  const cycles = Math.round(g(d,'bmsMaster.cycles'));
  const chg = g(d,'ems.chgRemainTime');
  const dsg = g(d,'ems.dsgRemainTime');
  const isChg = totalIn > totalOut && totalIn > 0;
  const timeLabel = isChg ? 'Charge' : totalOut > 0 ? 'Discharge' : 'Idle';
  const timeVal = isChg ? fmtTime(chg) : totalOut > 0 ? fmtTime(dsg) : '--';
  const battTemp = Math.round(g(d,'bmsMaster.temp'));
  const invTemp = Math.round(g(d,'inv.outTemp'));
  const dcBus = g(d,'mppt.outWatts') / 10; // deciWatts
  const acEnabled = g(d,'inv.cfgAcEnabled');
  const dcEnabled = g(d,'mppt.carState');
  const minCell = g(d,'bmsMaster.minCellVol'); const maxCell = g(d,'bmsMaster.maxCellVol');
  const minV = minCell > 100 ? minCell/1000 : minCell;
  const maxV = maxCell > 100 ? maxCell/1000 : maxCell;
  const delta = ((maxV - minV)*1000).toFixed(0);

  const sohC = soh >= 80 ? 'green' : soh >= 60 ? 'yellow' : 'red';
  const sohLabel = soh >= 90 ? 'Excellent' : soh >= 80 ? 'Good' : soh >= 60 ? 'Fair' : 'Poor';

  // Additional data matching CLI richness
  const usbC1 = g(d,'pd.typec1Watts'); const usbC2 = g(d,'pd.typec2Watts');
  const usb1 = g(d,'pd.usb1Watts'); const usb2 = g(d,'pd.usb2Watts');
  const usbTotal = usbC1+usbC2+usb1+usb2;
  const mpptTemp = Math.round(g(d,'mppt.mpptTemp'));
  const mosTemp = Math.round(g(d,'bmsMaster.maxMosTemp','bmsMaster.minMosTemp'));
  const minCellT = Math.round(g(d,'bmsMaster.minCellTemp'));
  const maxCellT = Math.round(g(d,'bmsMaster.maxCellTemp'));
  const fanLvl = g(d,'inv.fanState'); const fanMode = g(d,'pd.iconFanMode');
  const beep = g(d,'pd.beepState');
  const xboost = g(d,'inv.cfgAcXboost');
  const remainMah = g(d,'bmsMaster.remainCap');
  const fullMah = g(d,'bmsMaster.fullCap');
  const remainWh = remainMah * volts * 1000 / 1e6;
  const fullWh = fullMah * volts * 1000 / 1e6;
  const chgAc = g(d,'pd.chgPowerAc'); const chgSun = g(d,'pd.chgSunPower');
  const dsgAc = g(d,'pd.dsgPowerAc'); const dsgDc = g(d,'pd.dsgPowerDc');
  const pdFw = d['pd.sysVer'] || ''; const bmsFw = d['bmsMaster.bmsHeartbeatVer'] || '';
  const invFw = d['inv.sysVer'] || ''; const mpptFw = d['mppt.swVer'] || '';
  const acFreq = g(d,'inv.cfgAcOutFreq') ? '50Hz' : '60Hz';
  const acVolt = g(d,'inv.cfgAcOutVoltage') / 1000;
  // Solar / MPPT — volts=÷10, inAmp=÷100 (centiamps), outAmp=÷10
  const pvVol = g(d,'mppt.inVol') / 10;
  const pvAmp = g(d,'mppt.inAmp') / 100;
  const mpptOutV = g(d,'mppt.outVol') / 10;
  const mpptOutA = g(d,'mppt.outAmp') / 10;
  const chgTypes = {0:'Off',1:'Solar',2:'AC',3:'AC+Solar'};
  const mpptChgType = chgTypes[g(d,'mppt.chgType')] || '--';
  const mpptFault = g(d,'mppt.faultCode');
  const mpptUsed = g(d,'pd.mpptUsedTime');
  const mpptUsedH = mpptUsed > 0 ? Math.round(mpptUsed/3600) : 0;
  const hasSolar = solarIn > 0 || pvVol > 1 || chgSun > 0;

  return `<div class="card">
    <div class="card-title">Delta Pro <span style="color:var(--dim);font-size:11px">(${sn.slice(-6)})</span>${solarIn>0?' <span style="color:var(--yellow)">☀</span>':''}</div>
    <div class="soc soc-${c}">${Math.round(soc)}%</div>
    <div class="bar-bg"><div class="bar-fill bar-${c}" style="width:${Math.min(100,Math.max(0,soc))}%"></div></div>
    <div class="health health-${sohC}">Health: ${Math.round(soh)}% (${sohLabel}) &nbsp; ${remainWh>0?(remainWh/1000).toFixed(2)+' / '+(fullWh/1000).toFixed(2)+' kWh':''}</div>
    <div class="stats">
      <span class="stat-label">Solar In</span><span class="stat-value stat-green">${fmtW(solarIn)}${pvVol>1?' <span style="color:var(--dim)">('+pvVol.toFixed(1)+'V '+pvAmp.toFixed(1)+'A)</span>':''}</span>
      <span class="stat-label">AC In</span><span class="stat-value stat-green">${fmtW(acIn)}${acIn>0?' ('+Math.round(acVolt)+'V '+acFreq+')':''}</span>
      <span class="stat-label">AC Out</span><span class="stat-value stat-red">${fmtW(acOut)}</span>
      <span class="stat-label">12V/Car</span><span class="stat-value stat-red">${fmtW(car)}</span>
      ${usbTotal>0?`<span class="stat-label">USB</span><span class="stat-value stat-red">${fmtW(usbTotal)}</span>`:''}
      <span class="stat-label">Total In</span><span class="stat-value stat-green">${fmtW(totalIn)}</span>
      <span class="stat-label">Total Out</span><span class="stat-value stat-red">${fmtW(totalOut)}</span>
      <span class="stat-label">${timeLabel}</span><span class="stat-value">${timeVal}</span>
      <span class="stat-label">DC Converter</span><span class="stat-value">${fmtW(dcBus)}</span>
      <span class="stat-label">Voltage</span><span class="stat-value">${volts.toFixed(1)} V</span>
      <span class="stat-label">Current</span><span class="stat-value" style="color:var(${current>0?'--green':'--red'})">${current.toFixed(1)} A</span>
      <span class="stat-label">Cell V</span><span class="stat-value">${minV.toFixed(2)}-${maxV.toFixed(2)}V <span style="color:var(${delta<=20?'--green':delta<=50?'--yellow':'--red'});">\u0394${delta}mV</span></span>
      <span class="stat-label">Cell T</span><span class="stat-value">${minCellT}-${maxCellT}\u00b0C</span>
      <span class="stat-label">Batt / Inv</span><span class="stat-value">${battTemp}\u00b0 / ${invTemp}\u00b0C</span>
      <span class="stat-label">MPPT / MOS</span><span class="stat-value">${mpptTemp}\u00b0 / ${mosTemp}\u00b0C</span>
      <span class="stat-label">Cycles</span><span class="stat-value">${cycles}</span>
      <span class="stat-label">Limits</span><span class="stat-value">${Math.round(g(d,'ems.minDsgSoc'))}%-${Math.round(g(d,'ems.maxChargeSoc'))}%</span>
      <span class="stat-label">Fan</span><span class="stat-value">${fanLvl?'ON (Lvl'+fanLvl+')':'Off'}</span>
      <span class="stat-label">Beep</span><span class="stat-value">${beep?'OFF':'ON'}</span>
    </div>
    ${hasSolar?`<div class="section-title">Solar / MPPT</div><div class="stats">
      <span class="stat-label">PV Input</span><span class="stat-value stat-green">${fmtW(solarIn)}</span>
      <span class="stat-label">PV Voltage</span><span class="stat-value">${pvVol>0?pvVol.toFixed(1)+' V':'--'}</span>
      <span class="stat-label">PV Current</span><span class="stat-value">${pvAmp>0?pvAmp.toFixed(2)+' A':'--'}</span>
      <span class="stat-label">PV Power (V×A)</span><span class="stat-value">${pvVol*pvAmp>0?(pvVol*pvAmp).toFixed(1)+' W':'--'}</span>
      ${solarIn > 1 && !acIn ? `<span class="stat-label">MPPT Efficiency</span><span class="stat-value" style="color:var(${Math.min(100,dcBus/solarIn*100)>=95?'--green':'--yellow'})">${Math.min(100,dcBus/solarIn*100).toFixed(0)}%</span>
      <span class="stat-label">Solar → Battery</span><span class="stat-value" style="color:var(${Math.abs(current)*volts/solarIn>=0.7?'--green':'--yellow'})">${current>0?(Math.abs(current)*volts/solarIn*100).toFixed(0)+'% ('+Math.round(current*volts)+'W)':'--'}</span>` : ''}
      <span class="stat-label">Source</span><span class="stat-value">${mpptChgType}</span>
      <span class="stat-label">MPPT Hours</span><span class="stat-value">${mpptUsedH>0?mpptUsedH+'h':'--'}</span>
      ${mpptFault?'<span class="stat-label">Fault</span><span class="stat-value" style="color:var(--red)">Code '+mpptFault+'</span>':''}
    </div>`:''}
    ${chgAc||chgSun||dsgAc||dsgDc?`<div class="section-title">Lifetime Energy</div><div class="stats">
      <span class="stat-label">AC Charged</span><span class="stat-value stat-green">${fmtWh(chgAc)}</span>
      <span class="stat-label">Solar Charged</span><span class="stat-value stat-green">${fmtWh(chgSun)}</span>
      <span class="stat-label">AC Discharged</span><span class="stat-value stat-red">${fmtWh(dsgAc)}</span>
      <span class="stat-label">DC Discharged</span><span class="stat-value stat-red">${fmtWh(dsgDc)}</span>
    </div>`:''}
    <div class="controls">
      <button class="btn ${acEnabled?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','a')">AC ${acEnabled?'ON':'OFF'}</button>
      <button class="btn ${dcEnabled?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','d')">DC ${dcEnabled?'ON':'OFF'}</button>
      <button class="btn" onclick="sendCmd('${sn}','x')">XBoost</button>
      <button class="btn" onclick="sendCmd('${sn}','c')">Chg ${g(d,'inv.cfgSlowChgWatts')>0?fmtW(g(d,'inv.cfgSlowChgWatts')):'PAUSED'}</button>
      <button class="btn" onclick="sendCmd('${sn}','=')">Chg+5%</button>
      <button class="btn" onclick="sendCmd('${sn}','-')">Chg-5%</button>
    </div>
  </div>`;
}

function buildSHP(sn, name, d, allDevices) {
  const gridSta = g(d,'gridSta','heartbeat.gridSta');
  const gridVol = g(d,'gridInfo.gridVol','gridVol');
  const gridFreq = g(d,'gridInfo.gridFreq','gridFreq');
  const gridDay = g(d,'gridDayWatth','heartbeat.gridDayWatth');
  const backupDay = g(d,'backupDayWatth','heartbeat.backupDayWatth');
  const combinedSoc = g(d,'backupBatPer','heartbeat.backupBatPer');
  const c = socColor(combinedSoc);
  const eps = d['eps'];

  let circuitHTML = '';
  let totalLoad = 0;
  for (let i = 0; i < 12; i++) {
    const w = g(d,`infoList.${i}.chWatt`,`loadCmdChCtrlInfos.${i}.ctrlWatt`);
    totalLoad += w;
    const isDp = i >= 10;
    const cls = isDp ? ' class="dp-row"' : '';
    let label = '';
    // Use configured circuit names first, then auto-detect DP
    const cNames = window.circuitNames;
    if (cNames && cNames[i]) {
      label = cNames[i];
    } else if (isDp) {
      const dpList = Object.entries(allDevices).filter(([s,v]) => v.type.includes('delta'));
      const dpIdx = i - 10;
      if (dpIdx < dpList.length) label = 'DP.'+dpList[dpIdx][0].slice(-4);
    }
    const cMode = g(d,`loadCmdChCtrlInfos.${i}.ctrlMode`);
    const isOff = cMode === 1;
    const offStyle = isOff ? ' style="opacity:0.4;text-decoration:line-through"' : '';
    circuitHTML += `<tr${cls}${offStyle}><td>${i+1}</td><td>${label}</td><td class="power">${fmtW(w)}</td><td><button class="btn" style="padding:2px 6px;font-size:10px" onclick="toggleCircuit('${sn}',${i})">${isOff?'ON':'OFF'}</button></td></tr>`;
  }

  // Battery details
  let battHTML = '';
  for (let i = 0; i < 2; i++) {
    const bSoc = g(d,`energyInfos.${i}.batteryPercentage`);
    const bConn = g(d,`energyInfos.${i}.stateBean.isConnect`);
    const bTemp = Math.round(g(d,`energyInfos.${i}.emsBatTemp`));
    const bChgT = g(d,`energyInfos.${i}.chargeTime`);
    const bDsgT = g(d,`energyInfos.${i}.dischargeTime`);
    const bIn = g(d,`energyInfos.${i}.lcdInputWatts`);
    const bOut = g(d,`energyInfos.${i}.outputPower`);
    const bGridChg = g(d,`energyInfos.${i}.stateBean.isGridCharge`);
    const bOutput = g(d,`energyInfos.${i}.stateBean.isPowerOutput`);
    const bFullMah = g(d,`energyInfos.${i}.fullCap`);
    const bRemainWh = (bSoc/100 * bFullMah * 45 / 1e6);
    if (!bConn) { battHTML += `<div style="color:var(--dim)">Batt ${i+1}: Not connected</div>`; continue; }
    const bc = socColor(bSoc);
    let status = bGridChg ? '<span style="color:var(--green)">GridChg</span>' : bOutput ? '<span style="color:var(--red)">Output</span>' : '<span style="color:var(--dim)">Standby</span>';
    let power = '';
    if (bIn > 0) power += ` <span style="color:var(--green)">+${fmtW(bIn)}</span>`;
    if (bOut > 0) power += ` <span style="color:var(--red)">-${fmtW(bOut)}</span>`;
    let time = bChgT > 0 ? `Chg:${fmtTime(bChgT)}` : bDsgT > 0 ? `Dsg:${fmtTime(bDsgT)}` : '';
    battHTML += `<div>Batt ${i+1}: <b style="color:var(--${bc})">${Math.round(bSoc)}%</b> ${bRemainWh.toFixed(1)}kWh ${bTemp}\u00b0C ${status}${power} ${time}</div>`;
  }

  // Uptime
  const workTime = g(d,'workTime');
  const uptimeS = workTime > 0 ? Math.round(workTime / 1000) : 0;
  const uptimeD = Math.floor(uptimeS / 86400);
  const uptimeH = Math.floor((uptimeS % 86400) / 3600);
  const uptimeStr = uptimeS > 0 ? `${uptimeD}d ${uptimeH}h` : '--';

  return `<div class="card">
    <div class="card-title">Smart Home Panel <span style="color:var(--dim);font-size:11px">(${sn.slice(-6)})</span></div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span>Grid: <b style="color:var(${gridSta?'--green':'--red'})">${gridSta?'ON':'OFF'}</b></span>
      ${gridVol ? `<span style="color:var(--dim)">${Math.round(gridVol)}V ${Math.round(gridFreq)}Hz</span>` : ''}
      <span>EPS: <b style="color:var(${eps?'--yellow':'--dim'})">${eps?'ON':'OFF'}</b></span>
    </div>
    <div class="stats" style="margin-top:6px">
      <span class="stat-label">Combined</span><span class="stat-value soc-${c}">${Math.round(combinedSoc)}%</span>
      <span class="stat-label">Grid Today</span><span class="stat-value">${fmtWh(gridDay)}${energyCfg ? ' <span style="color:var(--green)">'+energyCfg.currency+(gridDay/1000*energyCfg.current_rate).toFixed(2)+'</span>'+(energyCfg.rate_night?' <span style="color:var(--dim)">('+( new Date().getHours()>=energyCfg.day_start && new Date().getHours()<energyCfg.day_end?'Day':'Night')+': '+energyCfg.currency+energyCfg.current_rate+')</span>':''):''}</span>
      <span class="stat-label">Backup Today</span><span class="stat-value">${fmtWh(backupDay)}</span>
      <span class="stat-label">Total Load</span><span class="stat-value" style="font-weight:700">${fmtW(totalLoad)}</span>
      <span class="stat-label">Uptime</span><span class="stat-value">${uptimeStr}</span>
    </div>
    ${battHTML}
    <div class="section-title" style="margin-top:8px">Circuits</div>
    <table class="circuits"><tr><th>#</th><th>Name</th><th style="text-align:right">Power</th><th></th></tr>${circuitHTML}</table>
    <div class="controls">
      <button class="btn ${eps?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','e')">EPS ${eps?'ON':'OFF'}</button>
      <button class="btn" onclick="sendCmd('${sn}','g')">Grid Chg B1</button>
      <button class="btn" onclick="sendCmd('${sn}','h')">Grid Chg B2</button>
      <button class="btn" onclick="sendCmd('${sn}','p')">⚡ Power Save</button>
    </div>
  </div>`;
}

const BLADE_STATES = {
  0x500: ['Idle', '#888'],
  0x501: ['Charging', '#3b82f6'],
  0x502: ['Mowing', '#10b981'],
  0x503: ['Returning', '#eab308'],
  0x504: ['Charging', '#3b82f6'],
  0x505: ['Mapping', '#a855f7'],
  0x506: ['Paused', '#eab308'],
  0x507: ['Error', '#ef4444'],
  0x801: ['Charging', '#3b82f6'],
};
// Real errors come from normalBleHeartBeat.errorCode.0..N. App shows each
// as 4-digit hex (e.g. 0x700 → "0700"). robotLowerr mirrors robotState and
// is NOT an error code.
const BLADE_ERRORS = {
  0x700: 'Low battery — charge to 90% before working',
  0x701: 'Work suspended — rain detected',
  0x503: 'Out of bounds',
  2062: 'RTK signal lost (cleared)',
  2001: 'Motor overload', 2002: 'Bumper triggered',
  2003: 'Lifted from ground', 2004: 'Stuck', 2005: 'Battery overheat',
  2006: 'Rain detected', 2007: 'GPS lost', 2008: 'Out of mowing zone',
};
const RTK_STATES = {0:'no fix',1:'single',2:'DGPS',3:'RTK float',4:'RTK fixed'};

function buildBlade(sn, name, d) {
  const battery = g(d, 'normalBleHeartBeat.batteryRemainPercent');
  const stateCode = g(d, 'normalBleHeartBeat.robotState') | 0;
  const [stateLabel, stateColor] = BLADE_STATES[stateCode] || [`Unknown(0x${stateCode.toString(16)})`, '#fff'];
  const battColor = battery >= 60 ? '#10b981' : battery >= 20 ? '#eab308' : '#ef4444';

  const wifi = g(d, 'signalInfo.wifiSignal');
  const sig4g = g(d, 'signalInfo.4gSignal');
  let connStr = '<span style="color:#888">Offline</span>';
  if (wifi) {
    const dbm = -Math.abs(wifi|0);
    const c = dbm > -65 ? '#10b981' : dbm > -80 ? '#eab308' : '#ef4444';
    connStr = `<span style="color:${c}">📶 WiFi ${dbm}dBm</span>`;
  } else if (sig4g) {
    connStr = `<span style="color:#06b6d4">📡 4G ${-Math.abs(sig4g|0)}dBm</span>`;
  }

  const rtkScore = g(d, 'normalBleHeartBeat.robotRtkScore');
  const rtkBaseScore = g(d, 'normalBleHeartBeat.baseRtkScore');
  const rtkState = g(d, 'normalBleHeartBeat.rtkState') | 0;
  const sats = g(d, 'signalInfo.trackedSatellites') | 0;
  const poseX = g(d, 'normalBleHeartBeat.poseX') | 0;
  const poseY = g(d, 'normalBleHeartBeat.poseY') | 0;
  const angle = g(d, 'normalBleHeartBeat.angle') | 0;

  const robotLat = g(d, 'signalInfo.robotLat');
  const robotLng = g(d, 'signalInfo.robotLng');
  let baseLat = g(d, 'signalInfo.baseLat');
  let baseLng = g(d, 'signalInfo.baseLng');
  // Discard bogus base coords (e.g. ~3.04, 3.05) when the base hasn't
  // published a real fix. We only trust them if within ~1 km of the robot.
  if (robotLat && robotLng && baseLat && baseLng) {
    const R = 6371000;
    const toRad = x => x * Math.PI / 180;
    const dp = toRad(baseLat - robotLat);
    const dl = toRad(baseLng - robotLng);
    const a = Math.sin(dp/2)**2 + Math.cos(toRad(robotLat))*Math.cos(toRad(baseLat))*Math.sin(dl/2)**2;
    const distM = 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    if (distM > 1000) { baseLat = 0; baseLng = 0; }
  }
  const hasGps = robotLat && robotLng;

  const workArea = g(d, 'normalBleHeartBeat.currentWorkArea');
  const workTime = g(d, 'normalBleHeartBeat.currentWorkTime') | 0;
  const workProg = g(d, 'normalBleHeartBeat.currentWorkProgress') | 0;
  const edgeTotal = g(d, 'normalBleHeartBeat.edgeTotal') | 0;
  const edgeCur = g(d, 'normalBleHeartBeat.edgeCurrent') | 0;
  const mapArea = g(d, 'normalBleHeartBeat.mappingArea');
  const mapDist = g(d, 'normalBleHeartBeat.mappingDistance');
  const rainCd = g(d, 'normalBleHeartBeat.rainCountdown') | 0;
  const errCount = g(d, 'normalBleHeartBeat.errorCount') | 0;
  // Real errors come from errorCode.0..N. robotLowerr just mirrors robotState.
  const errCodes = [];
  for (let i = 0; i < 8; i++) {
    const c = g(d, 'normalBleHeartBeat.errorCode.' + i) | 0;
    if (c) errCodes.push(c);
  }

  const showJob = workArea > 0 || workTime > 0 || edgeTotal > 0 || mapArea > 0;
  const rtkColor = rtkState === 4 ? '#10b981' : rtkState >= 2 ? '#eab308' : '#ef4444';

  let flags = '';
  if (rainCd > 0) flags += `<div style="color:#eab308">🌧️ Rain delay: ${rainCd}s</div>`;
  if (errCodes.length) {
    const parts = errCodes.map(c => {
      const hex = c.toString(16).toUpperCase().padStart(4, '0');
      return `${hex} (${BLADE_ERRORS[c] || 'unknown'})`;
    }).join(', ');
    flags += `<div style="color:#ef4444"><b>⚠️ ${errCount} active:</b> ${parts}</div>`;
  } else if (errCount > 0) {
    flags += `<div style="color:#ef4444"><b>⚠️ ${errCount} active error(s)</b></div>`;
  }

  const mapId = `bladeMap_${sn.replace(/[^a-zA-Z0-9]/g,'')}`;

  return `<div class="card">
    <div class="card-header">
      <h3>🤖 Blade <span class="dim">(${name})</span></h3>
      <div class="status-line">
        <span style="color:${stateColor};font-weight:600">${stateLabel}</span>
        <span class="dim">·</span>
        <span style="color:${battColor};font-weight:600">${battery|0}%</span>
        <span class="dim">·</span>
        ${connStr}
      </div>
    </div>
    <div class="battery-bar"><div class="battery-fill" style="width:${battery|0}%;background:${battColor}"></div></div>
    <table class="kv">
      <tr><td>RTK Status</td><td><span style="color:${rtkColor}">${RTK_STATES[rtkState]||'?'}</span></td>
          <td>Satellites</td><td>${sats}</td></tr>
      <tr><td>RTK Score</td><td>Robot ${rtkScore|0} / Base ${rtkBaseScore|0}</td>
          <td>Position</td><td>(${poseX}, ${poseY}) ${angle}°</td></tr>
      ${hasGps ? `<tr><td>📍 Robot GPS</td><td>${robotLat.toFixed(5)}, ${robotLng.toFixed(5)}</td>
          <td>📍 Base GPS</td><td>${baseLat.toFixed(5)}, ${baseLng.toFixed(5)}</td></tr>` : ''}
    </table>
    ${showJob ? `<table class="kv" style="margin-top:8px">
      <tr><td>Work Area</td><td>${workArea.toFixed(1)} m²</td>
          <td>Progress</td><td>${workProg}%</td></tr>
      <tr><td>Work Time</td><td>${workTime} min</td>
          <td>Edge Cut</td><td>${edgeCur} / ${edgeTotal}</td></tr>
      ${mapArea > 0 ? `<tr><td>Mapped Area</td><td>${mapArea.toFixed(1)} m²</td>
          <td>Map Distance</td><td>${mapDist.toFixed(1)} m</td></tr>` : ''}
    </table>` : ''}
    ${flags ? `<div style="margin-top:8px">${flags}</div>` : ''}
    ${hasGps ? `<div id="${mapId}" data-lat="${robotLat}" data-lng="${robotLng}" data-blat="${baseLat}" data-blng="${baseLng}"
                    style="height:280px;margin-top:12px;border-radius:8px;background:#1a1a1a"></div>` : ''}
  </div>`;
}

function initBladeMaps(blades) {
  // Lazy-load Leaflet from CDN
  if (!window.L) {
    if (document.getElementById('leaflet-css')) return setTimeout(() => initBladeMaps(blades), 200);
    const css = document.createElement('link');
    css.id = 'leaflet-css';
    css.rel = 'stylesheet';
    css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(css);
    const js = document.createElement('script');
    js.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    js.onload = () => initBladeMaps(blades);
    document.head.appendChild(js);
    return;
  }
  if (!window.bladeMaps) window.bladeMaps = {};
  for (const [sn, info] of blades) {
    const id = `bladeMap_${sn.replace(/[^a-zA-Z0-9]/g,'')}`;
    const el = document.getElementById(id);
    if (!el) continue;
    const lat = parseFloat(el.dataset.lat);
    const lng = parseFloat(el.dataset.lng);
    const blat = parseFloat(el.dataset.blat);
    const blng = parseFloat(el.dataset.blng);
    if (!lat || !lng) continue;

    if (window.bladeMaps[sn]) {
      // The dashboard innerHTML rewrite on each refresh detaches the old map
      // div — drop the stale Leaflet instance and rebuild on the new element.
      const oldContainer = window.bladeMaps[sn].map.getContainer();
      if (oldContainer !== el || !document.body.contains(oldContainer)) {
        try { window.bladeMaps[sn].map.remove(); } catch(e){}
        delete window.bladeMaps[sn];
      } else {
        window.bladeMaps[sn].robot.setLatLng([lat, lng]);
        if (blat && blng && window.bladeMaps[sn].base) window.bladeMaps[sn].base.setLatLng([blat, blng]);
        continue;
      }
    }
    {
      const map = L.map(id, {zoomControl: true, attributionControl: false}).setView([lat, lng], 18);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19}).addTo(map);
      const robotIcon = L.divIcon({
        html: '<div style="background:#10b981;color:#fff;border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:18px;border:2px solid #000;box-shadow:0 0 8px #10b981">🤖</div>',
        iconSize: [32, 32], iconAnchor: [16, 16], className: ''
      });
      const baseIcon = L.divIcon({
        html: '<div style="background:#3b82f6;color:#fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;border:2px solid #000;box-shadow:0 0 8px #3b82f6">🏠</div>',
        iconSize: [28, 28], iconAnchor: [14, 14], className: ''
      });
      const robot = L.marker([lat, lng], {icon: robotIcon, title: 'Robot'}).addTo(map);
      const base = (blat && blng) ? L.marker([blat, blng], {icon: baseIcon, title: 'Base Station'}).addTo(map) : null;
      window.bladeMaps[sn] = {map, robot, base};
    }
  }
}

async function refresh() {
  try {
    const r = await fetch('/api/devices');
    const j = await r.json();

    const verEl = $('#version');
    const isDocker = navigator.userAgent.includes('docker') || window.location.port === '5000';
    if (j.latest_version && j.latest_version !== j.version) {
      verEl.innerHTML = 'v' + j.version + ' <span title="v' + j.latest_version + ' available' +
        (isDocker ? ' — docker pull ghcr.io/win365ru/ecoflow-dashboard:latest' : ' — pip install --upgrade') +
        '" style="color:var(--yellow);cursor:help">\u26a0\ufe0f</span>';
    } else {
      verEl.textContent = 'v' + j.version;
    }
    const mb = $('#mqtt-badge');
    mb.textContent = j.connected ? 'MQTT Connected' : 'MQTT Disconnected';
    mb.className = 'badge ' + (j.connected ? 'badge-green' : 'badge-red');
    const tb = $('#tg-badge');
    if (j.telegram) {
      tb.textContent = j.telegram.connected ? 'TG ✓' : 'TG ✗';
      tb.className = 'badge ' + (j.telegram.connected ? 'badge-green' : 'badge-red');
      tb.style.display = '';
    } else { tb.style.display = 'none'; }
    $('#clock').textContent = new Date().toLocaleTimeString();

    window.energyCfg = j.energy || null;
    window.circuitNames = j.circuit_names || null;
    const devs = j.devices || {};
    const deltas = [], shps = [], blades = [];
    for (const [sn, info] of Object.entries(devs)) {
      if (info.type.includes('delta')) deltas.push([sn, info]);
      else if (info.type.includes('panel')) shps.push([sn, info]);
      else if (info.type.includes('blade')) blades.push([sn, info]);
    }

    // Stale-data badge — color-coded last-update age, overlaid on each card.
    function staleBadge(ageSec) {
      if (ageSec === undefined || ageSec === null || ageSec < 0)
        return '<span style="color:#888;font-size:11px">no data</span>';
      let txt, c;
      if (ageSec < 30) { txt = 'live'; c = '#10b981'; }
      else if (ageSec < 120) { txt = ageSec + 's'; c = '#10b981'; }
      else if (ageSec < 600) { txt = Math.floor(ageSec/60) + 'm'; c = '#eab308'; }
      else { txt = 'stale ' + Math.floor(ageSec/60) + 'm'; c = '#ef4444'; }
      return '<span title="Last MQTT update" style="color:' + c +
             ';font-size:11px;font-weight:600;background:#0d1117cc;padding:2px 6px;border-radius:4px">🕒 ' + txt + '</span>';
    }
    function wrapCard(inner, ageSec) {
      return '<div style="position:relative">' + inner +
             '<div style="position:absolute;top:10px;right:14px;z-index:5">' + staleBadge(ageSec) + '</div></div>';
    }

    let html = '<div class="grid grid-2">';
    for (const [sn, info] of deltas) {
      html += wrapCard(buildDeltaPro(sn, info.name, info.data), info.data_age_sec);
    }
    html += '</div>';
    for (const [sn, info] of shps) {
      html += '<div class="grid">' + wrapCard(buildSHP(sn, info.name, info.data, devs), info.data_age_sec) + '</div>';
    }
    for (const [sn, info] of blades) {
      html += '<div class="grid">' + wrapCard(buildBlade(sn, info.name, info.data), info.data_age_sec) + '</div>';
    }

    // Detach live blade map containers before innerHTML rewrite so Leaflet
    // keeps its state (zoom, center, tiles) — otherwise the map flashes and
    // resets zoom on every 2s refresh.
    const liveMaps = {};
    if (window.bladeMaps) {
      for (const [sn, m] of Object.entries(window.bladeMaps)) {
        const c = m.map.getContainer();
        if (c && document.body.contains(c)) {
          liveMaps[sn] = c;
          c.remove();
        }
      }
    }

    $('#dashboard').innerHTML = html;

    // Swap fresh empty map placeholders with the preserved live containers.
    for (const [sn, liveEl] of Object.entries(liveMaps)) {
      const id = `bladeMap_${sn.replace(/[^a-zA-Z0-9]/g,'')}`;
      const placeholder = document.getElementById(id);
      if (placeholder) {
        // Copy fresh GPS coords from the new placeholder so initBladeMaps
        // reads up-to-date marker positions from data-* attrs.
        liveEl.dataset.lat = placeholder.dataset.lat;
        liveEl.dataset.lng = placeholder.dataset.lng;
        liveEl.dataset.blat = placeholder.dataset.blat;
        liveEl.dataset.blng = placeholder.dataset.blng;
        placeholder.replaceWith(liveEl);
      } else {
        // Blade no longer in dashboard — dispose of orphan map.
        try { window.bladeMaps[sn].map.remove(); } catch(e){}
        delete window.bladeMaps[sn];
      }
    }

    // Initialize maps after DOM update (existing maps just get marker updates).
    if (blades.length) initBladeMaps(blades);
    updateDeviceSelector(devs);
  } catch(e) {
    console.error('refresh error', e);
  }
}

refresh();
setInterval(refresh, 2000);

// ── Charts ──
const chartOpts = {
  responsive: true, animation: false,
  scales: {
    x: { ticks: { color: '#8b949e', maxTicksLimit: 10, font: {size:10} }, grid: { color: '#21262d' } },
    y: { ticks: { color: '#8b949e', font: {size:10} }, grid: { color: '#21262d' } }
  },
  plugins: { legend: { labels: { color: '#e6edf3', font: {size:11} } } }
};

let chartSoc, chartPower, chartCircuits;
let chartMode = 'live';
let chartSn = '';
let allDevicesList = {};

function initCharts() {
  if (chartSoc) return;
  const mk = (id, cfg) => new Chart(document.getElementById(id), cfg);

  chartSoc = mk('chart-soc', { type:'line', data:{labels:[],datasets:[]},
    options:{...chartOpts, plugins:{...chartOpts.plugins, title:{display:true,text:'Battery SOC %',color:'#e6edf3'}},
    scales:{...chartOpts.scales, y:{...chartOpts.scales.y, min:0,max:100}}} });

  chartPower = mk('chart-power', { type:'line', data:{labels:[],datasets:[]},
    options:{...chartOpts, plugins:{...chartOpts.plugins, title:{display:true,text:'Power (W)',color:'#e6edf3'}}} });

  chartCircuits = mk('chart-bar', { type:'bar', data:{labels:[],datasets:[]},
    options:{...chartOpts, plugins:{...chartOpts.plugins, title:{display:true,text:'Circuit Loads (W)',color:'#e6edf3'}}} });
  // Actually use line for circuits over time too
  chartCircuits.destroy();
  chartCircuits = mk('chart-circuits', { type:'line', data:{labels:[],datasets:[]},
    options:{...chartOpts, plugins:{...chartOpts.plugins, title:{display:true,text:'Circuit Loads (W)',color:'#e6edf3'}}} });
}

function setChartMode(mode) {
  chartMode = mode;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => { if(b.textContent.toLowerCase().includes(mode)) b.classList.add('active'); });
  document.getElementById('hist-hours').style.display = mode === 'history' ? '' : 'none';
  document.getElementById('custom-range').style.display = 'none';
  document.getElementById('hist-info').style.display = mode === 'history' ? '' : 'none';
  document.getElementById('csv-btn').style.display = mode === 'history' ? '' : 'none';
  if (mode === 'history') { fetchHistRange(); loadHistory(); }
}

function chartDeviceChanged() {
  chartSn = document.getElementById('chart-device').value;
  if (chartMode === 'history') { fetchHistRange(); loadHistory(); }
}

function updateDeviceSelector(devs) {
  const sel = document.getElementById('chart-device');
  const prev = sel.value;
  const sns = Object.keys(devs);
  if (sel.options.length !== sns.length) {
    sel.innerHTML = sns.map(sn => {
      const t = devs[sn].type || '';
      const label = t.includes('delta') ? 'Delta Pro' : t.includes('panel') ? 'Smart Panel' : t;
      return `<option value="${sn}">${label} (${sn.slice(-6)})</option>`;
    }).join('');
  }
  if (!chartSn && sns.length) chartSn = sns[0];
  if (prev) sel.value = prev;
  allDevicesList = devs;
}

const COLORS = ['#3fb950','#f85149','#58a6ff','#d29922','#bc8cff','#ff7b72','#79c0ff','#56d364','#e3b341','#db61a2','#7ee787','#ffa657'];

async function updateLiveCharts() {
  if (!chartSn) return;
  try {
    const r = await fetch(`/api/live?sn=${chartSn}`);
    const points = await r.json();
    if (!points.length) return;

    const labels = points.map(p => typeof p.ts === 'number' ? new Date(p.ts).toLocaleTimeString() : p.ts);
    const dtype = allDevicesList[chartSn]?.type || '';

    // SOC chart
    if (dtype.includes('delta')) {
      chartSoc.data = { labels, datasets: [
        { label: 'SOC %', data: points.map(p => p['ems.lcdShowSoc'] ?? p['bmsMaster.soc'] ?? null),
          borderColor: '#3fb950', borderWidth: 1.5, pointRadius: 0, fill: false }
      ]};
    } else {
      chartSoc.data = { labels, datasets: [
        { label: 'Combined %', data: points.map(p => p['backupBatPer'] ?? null),
          borderColor: '#3fb950', borderWidth: 1.5, pointRadius: 0, fill: false }
      ]};
    }
    chartSoc.update();

    // Power chart
    if (dtype.includes('delta')) {
      chartPower.data = { labels, datasets: [
        { label: 'Total In', data: points.map(p => p['pd.wattsInSum'] ?? null),
          borderColor: '#3fb950', borderWidth: 1.5, pointRadius: 0 },
        { label: 'Total Out', data: points.map(p => p['pd.wattsOutSum'] ?? null),
          borderColor: '#f85149', borderWidth: 1.5, pointRadius: 0 },
        { label: 'Solar', data: points.map(p => p['mppt.inWatts'] != null ? p['mppt.inWatts']/10 : null),
          borderColor: '#d29922', borderWidth: 1.5, pointRadius: 0 },
      ]};
    } else {
      // SHP: show total circuit load
      chartPower.data = { labels, datasets: [
        { label: 'Total Load', data: points.map(p => {
            let sum = 0; for(let i=0;i<12;i++) sum += (p[`infoList.${i}.chWatt`]||0); return sum;
          }), borderColor: '#f85149', borderWidth: 1.5, pointRadius: 0 },
      ]};
    }
    chartPower.update();

    // Circuits chart (SHP only, or skip for Delta)
    if (dtype.includes('panel')) {
      const ds = [];
      for (let i = 0; i < 12; i++) {
        const vals = points.map(p => p[`infoList.${i}.chWatt`] ?? 0);
        if (vals.some(v => v > 0)) {
          const cLabel = window.circuitNames && window.circuitNames[i] ? window.circuitNames[i] : `#${i+1}`;
          ds.push({ label: cLabel, data: vals, borderColor: COLORS[i%COLORS.length],
                    borderWidth: 1, pointRadius: 0 });
        }
      }
      chartCircuits.data = { labels, datasets: ds };
    } else {
      chartCircuits.data = { labels: [], datasets: [] };
    }
    chartCircuits.update();
  } catch(e) { console.error('chart error', e); }
}

function histRangeChanged() {
  const sel = document.getElementById('hist-hours').value;
  document.getElementById('custom-range').style.display = sel === 'custom' ? '' : 'none';
  if (sel !== 'custom') loadHistory();
}

async function fetchHistRange() {
  try {
    const r = await fetch('/api/history/range');
    const ranges = await r.json();
    if (chartSn && ranges[chartSn]) {
      const info = ranges[chartSn];
      document.getElementById('hist-info').textContent =
        `Data: ${info.start} to ${info.end} (${info.snapshots} snapshots)`;
      document.getElementById('hist-start').value = info.start;
      document.getElementById('hist-end').value = info.end;
    }
  } catch(e) {}
}

async function loadHistory() {
  if (!chartSn) return;
  const sel = document.getElementById('hist-hours').value;
  let url;
  if (sel === 'custom') {
    const start = document.getElementById('hist-start').value;
    const end = document.getElementById('hist-end').value;
    if (!start || !end) return;
    url = `/api/history?sn=${chartSn}&start=${start}&end=${end}`;
  } else {
    url = `/api/history?sn=${chartSn}&hours=${sel}`;
  }
  try {
    const r = await fetch(url);
    const points = await r.json();
    if (!points.length || points.error) return;

    const labels = points.map(p => {
      if (!p.ts) return '';
      const d = new Date(p.ts);
      return isNaN(d) ? p.ts.slice(5,16) : d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    });
    const dtype = allDevicesList[chartSn]?.type || '';

    // SOC
    const socKey = dtype.includes('delta') ? 'ems.lcdShowSoc' : 'backupBatPer';
    const socAlts = dtype.includes('delta') ? ['bmsMaster.soc','bmsMaster.f32ShowSoc'] : [];
    chartSoc.data = { labels, datasets: [
      { label: 'SOC %', data: points.map(p => p[socKey] ?? p[socAlts[0]] ?? null),
        borderColor: '#3fb950', borderWidth: 1.5, pointRadius: 0, fill: false }
    ]};
    chartSoc.update();

    // Power
    if (dtype.includes('delta')) {
      chartPower.data = { labels, datasets: [
        { label: 'Total In', data: points.map(p => p['pd.wattsInSum'] ?? null),
          borderColor: '#3fb950', borderWidth: 1.5, pointRadius: 0 },
        { label: 'Total Out', data: points.map(p => p['pd.wattsOutSum'] ?? null),
          borderColor: '#f85149', borderWidth: 1.5, pointRadius: 0 },
        { label: 'Solar', data: points.map(p => p['mppt.inWatts'] != null ? p['mppt.inWatts']/10 : null),
          borderColor: '#d29922', borderWidth: 1.5, pointRadius: 0 },
      ]};
    } else {
      const ds = [];
      for (let i = 0; i < 12; i++) {
        const k = `infoList.${i}.chWatt`;
        const vals = points.map(p => p[k] ?? null);
        if (vals.some(v => v !== null && v > 0)) {
          const cLabel = window.circuitNames && window.circuitNames[i] ? window.circuitNames[i] : `#${i+1}`;
          ds.push({ label: cLabel, data: vals, borderColor: COLORS[i%COLORS.length],
                    borderWidth: 1, pointRadius: 0 });
        }
      }
      chartCircuits.data = { labels, datasets: ds };
      chartCircuits.update();

      // Total load for power chart
      chartPower.data = { labels, datasets: [
        { label: 'Total Load', data: points.map(p => {
            let s=0; for(let i=0;i<12;i++) s+=(p[`infoList.${i}.chWatt`]||0); return s||null;
          }), borderColor: '#f85149', borderWidth: 1.5, pointRadius: 0 },
      ]};
    }
    chartPower.update();
  } catch(e) { console.error('history error', e); }
}

// ── Battery Degradation ──
let chartDeg = null;

function initDegChart() {
  const ctx = document.getElementById('chart-degradation');
  if (!ctx) return;
  chartDeg = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      ...chartOpts,
      scales: {
        ...chartOpts.scales,
        y: { ...chartOpts.scales.y, min: 60, max: 100, title: { display: true, text: 'SOH %', color: '#8b949e' } },
        y1: { position: 'right', ticks: { color: '#8b949e' }, grid: { display: false },
              title: { display: true, text: 'Cycles', color: '#8b949e' } }
      }
    }
  });
  // Populate device selector
  const sel = document.getElementById('deg-device');
  const deltas = Object.entries(allDevicesList).filter(([s,v]) => v.type.includes('delta'));
  sel.innerHTML = deltas.map(([sn,v]) => `<option value="${sn}">Delta Pro (${sn.slice(-6)})</option>`).join('');
  if (deltas.length) loadDegradation();
}

async function loadDegradation() {
  const sn = document.getElementById('deg-device').value;
  if (!sn || !chartDeg) return;
  try {
    const r = await fetch(`/api/degradation?sn=${sn}`);
    const j = await r.json();
    const data = j.data || [];
    if (!data.length) {
      document.getElementById('deg-prediction').textContent = 'No degradation data yet. SOH is logged every 5 minutes.';
      return;
    }
    chartDeg.data = {
      labels: data.map(d => d.date),
      datasets: [
        { label: 'SOH %', data: data.map(d => d.soh), borderColor: '#3fb950', borderWidth: 2, pointRadius: 2,
          fill: false, yAxisID: 'y' },
        { label: 'Cycles', data: data.map(d => d.cycles), borderColor: '#58a6ff', borderWidth: 1, pointRadius: 1,
          fill: false, yAxisID: 'y1' }
      ]
    };
    chartDeg.update();

    // Prediction
    const pred = j.prediction;
    if (pred) {
      const parts = [`Daily drop: ${pred.daily_drop}%`];
      if (pred.days_to_80pct > 0) parts.push(`80% SOH in ~${Math.round(pred.days_to_80pct/30)} months`);
      if (pred.days_to_70pct > 0) parts.push(`70% SOH in ~${Math.round(pred.days_to_70pct/30)} months`);
      document.getElementById('deg-prediction').innerHTML =
        '🔮 <b>Prediction:</b> ' + parts.join(' · ');
    } else {
      document.getElementById('deg-prediction').textContent = `${data.length} days of data. Need 7+ days for prediction.`;
    }
  } catch(e) { console.error('degradation error', e); }
}

// ── Power Outage Log ──
async function loadOutages() {
  try {
    const r = await fetch('/api/outages?limit=20');
    const outages = await r.json();
    if (!outages.length) {
      document.getElementById('outage-table').innerHTML = '<span style="color:var(--dim)">No outages recorded yet.</span>';
      return;
    }
    let html = '<table class="circuits"><tr><th>Date</th><th>Duration</th><th>SOC</th><th>Used</th><th>Peak</th><th>Avg</th></tr>';
    for (const o of outages) {
      const start = new Date(o.start_time);
      const dur = o.duration_sec;
      const durStr = dur >= 3600 ? `${Math.floor(dur/3600)}h ${Math.floor((dur%3600)/60)}m` : `${Math.floor(dur/60)}m ${dur%60}s`;
      html += `<tr>
        <td>${start.toLocaleDateString()} ${start.toLocaleTimeString()}</td>
        <td>${durStr}</td>
        <td>${o.soc_start?.toFixed(0)}% → ${o.soc_end?.toFixed(0)}%</td>
        <td style="color:var(--red)">${o.soc_used?.toFixed(1)}%</td>
        <td>${o.peak_load?.toFixed(0)} W</td>
        <td>${o.avg_load?.toFixed(0)} W</td>
      </tr>`;
    }
    html += '</table>';
    document.getElementById('outage-table').innerHTML = html;
  } catch(e) { console.error('outages error', e); }
}

// ── Energy Flow Diagram (Sankey-style bar chart) ──
let chartFlow = null;

function initFlowChart() {
  const ctx = document.getElementById('chart-flow');
  if (!ctx) return;
  chartFlow = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      ...chartOpts, indexAxis: 'y',
      scales: {
        x: { ...chartOpts.scales.x, title: { display: true, text: 'Watts', color: '#8b949e' } },
        y: { ...chartOpts.scales.y }
      },
      plugins: { legend: { display: false } }
    }
  });
}

function updateFlowChart() {
  if (!chartFlow || !allDevicesList) return;
  const labels = [], values = [], colors = [];

  // Grid input
  for (const [sn, info] of Object.entries(allDevicesList)) {
    if (!info.type.includes('panel')) continue;
    const d = info.data;
    const totalLoad = (() => { let s=0; for(let i=0;i<12;i++) s+=g(d,`infoList.${i}.chWatt`); return s; })();

    labels.push('Grid → SHP'); values.push(totalLoad); colors.push('#3fb950');

    // Per circuit
    const cNames = window.circuitNames;
    for (let i = 0; i < 12; i++) {
      const w = g(d, `infoList.${i}.chWatt`);
      if (w > 5) {
        const name = cNames && cNames[i] ? cNames[i] : `Circuit ${i+1}`;
        labels.push(`  → ${name}`); values.push(w); colors.push(i >= 10 ? '#58a6ff' : '#8b949e');
      }
    }
  }

  // Delta Pro power flow
  for (const [sn, info] of Object.entries(allDevicesList)) {
    if (!info.type.includes('delta')) continue;
    const d = info.data;
    const totalIn = g(d, 'pd.wattsInSum');
    const totalOut = g(d, 'pd.wattsOutSum');
    const short = sn.slice(-6);
    if (totalIn > 5) { labels.push(`Grid → DP ${short}`); values.push(totalIn); colors.push('#3fb950'); }
    if (totalOut > 5) { labels.push(`DP ${short} → Load`); values.push(totalOut); colors.push('#f85149'); }
  }

  chartFlow.data = {
    labels,
    datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }]
  };
  chartFlow.update();
}

// ── CSV Export ──
function exportCSV() {
  if (!chartSn) return;
  const sel = document.getElementById('hist-hours').value;
  let url;
  if (sel === 'custom') {
    const start = document.getElementById('hist-start').value;
    const end = document.getElementById('hist-end').value;
    url = `/api/export/csv?sn=${chartSn}&start=${start}&end=${end}`;
  } else {
    url = `/api/export/csv?sn=${chartSn}&hours=${sel}`;
  }
  window.location.href = url;
}

// ── Solar Analytics ──
let chartSolar = null;

function initSolarChart() {
  const ctx = document.getElementById('chart-solar');
  if (!ctx) return;
  chartSolar = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      ...chartOpts,
      scales: {
        ...chartOpts.scales,
        y: { ...chartOpts.scales.y, title: { display: true, text: 'kWh', color: '#8b949e' } }
      }
    }
  });
  // Populate device selector (Delta Pros only)
  const sel = document.getElementById('solar-device');
  const deltas = Object.entries(allDevicesList).filter(([s,v]) => v.type.includes('delta'));
  sel.innerHTML = deltas.map(([sn,v]) => `<option value="${sn}">Delta Pro (${sn.slice(-6)})</option>`).join('');
  if (deltas.length) loadSolar();
}

async function loadSolar() {
  const sn = document.getElementById('solar-device').value;
  const days = document.getElementById('solar-days').value;
  if (!sn || !chartSolar) return;
  try {
    const r = await fetch(`/api/solar?sn=${sn}&days=${days}`);
    const j = await r.json();
    const daily = j.daily || [];
    if (!daily.length) {
      document.getElementById('solar-summary').innerHTML = '<span style="color:var(--dim)">No solar data yet.</span>';
      chartSolar.data = { labels: [], datasets: [] };
      chartSolar.update();
      return;
    }
    chartSolar.data = {
      labels: daily.map(d => d.date),
      datasets: [
        { label: 'Solar (kWh)', data: daily.map(d => d.solar_kwh), backgroundColor: '#d29922' },
        { label: 'Grid (kWh)', data: daily.map(d => d.grid_kwh), backgroundColor: '#30363d' },
      ]
    };
    chartSolar.update();

    const cur = j.currency || '$';
    let html = `<b>Total Solar:</b> ${j.total_solar_kwh} kWh · `;
    html += `<b>Lifetime:</b> ${j.lifetime_solar_kwh} kWh · `;
    html += `<b>Self-consumption:</b> ${j.self_consumption_avg}%`;
    if (j.money_saved > 0) html += ` · <b style="color:var(--green)">Saved: ${cur}${j.money_saved.toFixed(2)}</b>`;
    document.getElementById('solar-summary').innerHTML = html;
  } catch(e) { console.error('solar error', e); }
}

// Init charts after Chart.js loads
setTimeout(() => {
  initCharts();
  initDegChart();
  initFlowChart();
  initSolarChart();
  loadOutages();
  setInterval(() => {
    if (chartMode === 'live') updateLiveCharts();
    updateFlowChart();
  }, 3000);
  setInterval(loadOutages, 60000);
}, 500);
</script>
</body>
</html>"""
