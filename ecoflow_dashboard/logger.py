from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

# Key metrics to log per device type
DELTA_PRO_KEYS = [
    "bmsMaster.soc",
    "bmsMaster.f32ShowSoc",
    "bmsMaster.soh",
    "bmsMaster.vol",
    "bmsMaster.amp",
    "bmsMaster.temp",
    "bmsMaster.remainCap",
    "bmsMaster.fullCap",
    "bmsMaster.cycles",
    "ems.lcdShowSoc",
    "ems.chgRemainTime",
    "ems.dsgRemainTime",
    "inv.inputWatts",
    "inv.outputWatts",
    "mppt.inWatts",
    "mppt.outWatts",
    "pd.wattsInSum",
    "pd.wattsOutSum",
    "pd.chgPowerAc",
    "pd.chgSunPower",
    "pd.dsgPowerAc",
    "pd.dsgPowerDc",
    "pd.typec1Watts",
    "pd.typec2Watts",
    "pd.usb1Watts",
    "pd.usb2Watts",
    "pd.carWatts",
    # Solar / MPPT
    "mppt.inVol",
    "mppt.inAmp",
    "mppt.outVol",
    "mppt.outAmp",
    "mppt.chgType",
    "mppt.mpptTemp",
]

SHP_KEYS = [
    # Grid
    "gridSta",
    "gridInfo.gridVol",
    "gridInfo.gridFreq",
    "gridDayWatth",
    "backupDayWatth",
    # Combined battery
    "backupBatPer",
    "backupFullCap",
    "backupChaTime",
    # Per-battery (x2)
    *[f"energyInfos.{i}.{k}" for i in range(2) for k in [
        "batteryPercentage", "emsBatTemp", "chargeTime", "dischargeTime",
        "lcdInputWatts", "outputPower", "fullCap", "ratePower",
    ]],
    # Per-circuit power + current (x12)
    *[f"infoList.{i}.chWatt" for i in range(12)],
    *[f"loadChCurInfo.cur.{i}" for i in range(12)],
]


class DataLogger:
    def __init__(
        self,
        mqtt_client: EcoFlowMqttClient,
        device_types: dict[str, str],
        db_path: str = "ecoflow_history.db",
        interval: int = 300,
    ) -> None:
        self._mqtt = mqtt_client
        self._device_types = device_types
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._db_path = db_path
        self._init_db()
        # Outage tracking per SHP device
        self._outage_active: dict[str, dict] = {}  # sn → {start_time, soc_start, loads: []}


    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    device_sn TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshots_ts_sn
                ON snapshots (timestamp, device_sn)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshots_key
                ON snapshots (device_sn, key, timestamp)
            """)
            # Power outage log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_sn TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    duration_sec INTEGER,
                    soc_start REAL,
                    soc_end REAL,
                    soc_used REAL,
                    peak_load REAL,
                    avg_load REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outages_sn
                ON outages (device_sn, start_time)
            """)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Data logger started (interval=%ds, db=%s)", self._interval, self._db_path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Wait a bit for initial MQTT data to arrive
        self._stop.wait(10)

        while not self._stop.is_set():
            try:
                self._snapshot()
            except Exception:
                log.exception("Logger snapshot failed")
            self._stop.wait(self._interval)

    def _snapshot(self) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        rows = []

        for sn, dtype in self._device_types.items():
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue

            keys = DELTA_PRO_KEYS if dtype == "delta_pro" else SHP_KEYS

            for key in keys:
                v = data.get(key)
                if v is not None:
                    try:
                        rows.append((ts, sn, dtype, key, float(v)))
                    except (TypeError, ValueError):
                        continue

        # Check for grid outages on SHP devices
        for sn, dtype in self._device_types.items():
            if "panel" not in dtype:
                continue
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue
            grid_sta = data.get("gridSta") or data.get("heartbeat.gridSta")
            combined_soc = float(data.get("backupBatPer", 0) or data.get("heartbeat.backupBatPer", 0) or 0)
            total_load = sum(
                float(data.get(f"infoList.{i}.chWatt", 0) or 0) for i in range(12)
            )

            if not grid_sta and sn not in self._outage_active:
                # Outage started
                self._outage_active[sn] = {
                    "start_time": ts,
                    "soc_start": combined_soc,
                    "loads": [total_load],
                }
                log.info("Grid outage started for %s at %s (SOC: %.0f%%)", sn, ts, combined_soc)
            elif not grid_sta and sn in self._outage_active:
                # Outage ongoing — track loads
                self._outage_active[sn]["loads"].append(total_load)
            elif grid_sta and sn in self._outage_active:
                # Outage ended
                outage = self._outage_active.pop(sn)
                start = outage["start_time"]
                loads = outage["loads"]
                try:
                    from datetime import datetime as _dt
                    dur = int((_dt.fromisoformat(ts) - _dt.fromisoformat(start)).total_seconds())
                except Exception:
                    dur = len(loads) * self._interval
                soc_used = outage["soc_start"] - combined_soc
                peak = max(loads) if loads else 0
                avg = sum(loads) / len(loads) if loads else 0
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        "INSERT INTO outages (device_sn, start_time, end_time, duration_sec, "
                        "soc_start, soc_end, soc_used, peak_load, avg_load) VALUES (?,?,?,?,?,?,?,?,?)",
                        (sn, start, ts, dur, outage["soc_start"], combined_soc, soc_used, peak, avg),
                    )
                log.info("Grid outage ended for %s: %ds, SOC %.0f%%→%.0f%%, peak %.0fW",
                         sn, dur, outage["soc_start"], combined_soc, peak)

        if rows:
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "INSERT INTO snapshots (timestamp, device_sn, device_type, key, value) VALUES (?,?,?,?,?)",
                    rows,
                )
            log.info("Logged %d metrics at %s", len(rows), ts)
