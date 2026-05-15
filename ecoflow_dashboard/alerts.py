from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

import requests

from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

# Default alert thresholds (overridable via env vars)
DEFAULTS = {
    "ALERT_GRID_OUTAGE": "1",
    "ALERT_GRID_RESTORE": "1",
    "ALERT_BATTERY_LOW": "20",       # SOC % threshold
    "ALERT_BATTERY_FULL": "1",
    "ALERT_CHARGE_COMPLETE": "1",
    "ALERT_HIGH_TEMP_BATTERY": "45",   # °C — battery cells
    "ALERT_HIGH_TEMP_INVERTER": "85",  # °C — inverter (normal up to ~80)
    "ALERT_HIGH_TEMP_MPPT": "70",      # °C — MPPT charge controller
    "ALERT_OFFLINE_TIMEOUT": "300",  # seconds
    "ALERT_COOLDOWN": "1800",        # 30 min between repeated alerts
    "ALERT_DAILY_SUMMARY": "20",     # Hour (0-23) to send daily summary, empty to disable
    "ALERT_MONTHLY_REPORT": "1",     # Send monthly energy/cost report on 1st of month
    "ALERT_WEEKLY_DIGEST": "1",      # Anomaly digest on Monday morning
    # Blade thresholds
    "ALERT_BLADE_GEOFENCE_M": "100", # Robot-from-base distance (m) before alerting
    "ALERT_BLADE_STUCK_MIN": "10",   # Mins without progress while mowing → alert
    "ALERT_BLADE_EDGE_CUT_DAYS": "21",  # Days since last edge cut before reminder
    "ALERT_BLADE_RAIN_PROB": "60",   # % precipitation probability that triggers warning
}


def _env_int(key: str) -> int:
    return int(os.environ.get(key, DEFAULTS.get(key, "0")))


def _env_bool(key: str) -> bool:
    return os.environ.get(key, DEFAULTS.get(key, "0")) not in ("0", "false", "no", "")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class AlertManager:
    def __init__(
        self,
        mqtt_client: EcoFlowMqttClient,
        device_types: dict[str, str],
        device_names: dict[str, str],
        telegram_token: str,
        telegram_chat_id: str,
        energy_rate: float = 0.0,
        energy_rate_night: float = 0.0,
        energy_currency: str = "$",
        db_path: str = "",
    ) -> None:
        self._mqtt = mqtt_client
        self._device_types = device_types
        self._device_names = device_names
        self._token = telegram_token
        self._chat_id = telegram_chat_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Previous state per device for edge detection
        self._prev: dict[str, dict] = {}
        # Cooldown timestamps: "event_type:sn" → last alert time
        self._cooldowns: dict[str, float] = {}
        self._cooldown_secs = _env_int("ALERT_COOLDOWN")

        # Thresholds
        self._battery_low = _env_int("ALERT_BATTERY_LOW")
        self._high_temp_battery = _env_int("ALERT_HIGH_TEMP_BATTERY")
        self._high_temp_inverter = _env_int("ALERT_HIGH_TEMP_INVERTER")
        self._high_temp_mppt = _env_int("ALERT_HIGH_TEMP_MPPT")
        self._offline_timeout = _env_int("ALERT_OFFLINE_TIMEOUT")

        # Telegram connection status
        self._telegram_ok: bool = False
        self._telegram_error: str = ""

        # Energy cost
        self._energy_rate = energy_rate
        self._energy_rate_night = energy_rate_night
        self._energy_currency = energy_currency
        self._db_path = db_path

        # Daily summary
        self._summary_hour = os.environ.get("ALERT_DAILY_SUMMARY", DEFAULTS["ALERT_DAILY_SUMMARY"])
        self._last_summary_date: str = ""
        # Monthly report
        self._monthly_enabled = _env_bool("ALERT_MONTHLY_REPORT")
        self._last_monthly_date: str = ""

        # Discharge milestone tracking: {sn: last_notified_milestone}
        # Above 20%: notify every 20% (80, 60, 40, 20)
        # Below 20%: notify every 5% (15, 10, 5)
        self._discharge_milestones: dict[str, int] = {}
        # Solar charge milestone tracking: {sn: last_notified_milestone}
        # Notify at 50%, 80%, 90%, 100%
        self._solar_charge_milestones: dict[str, int] = {}

        # Outage tracking: {sn: {start_ts, start_soc1, start_soc2, ...}}
        self._outage_state: dict[str, dict] = {}

        # Active mower run tracking: {sn: {start_ts, battery_start}}
        self._mower_run_active: dict[str, dict] = {}
        # Stuck detection: {sn: {progress, since_ts}}
        self._mower_progress_seen: dict[str, dict] = {}
        # Geofence cool-down: {sn: timestamp_last_alert}
        self._mower_geofence_alerted: dict[str, float] = {}
        # Last weekly digest date (sent at most once/week)
        self._last_weekly_date: str = ""
        # Last rain warning timestamp
        self._mower_rain_alerted: dict[str, float] = {}
        # Optional solar_forecast handle — set externally so we can read precipitation
        self._solar_forecast = None

        # Blade thresholds
        self._blade_geofence_m = _env_int("ALERT_BLADE_GEOFENCE_M")
        self._blade_stuck_min = _env_int("ALERT_BLADE_STUCK_MIN")
        self._blade_edge_cut_days = _env_int("ALERT_BLADE_EDGE_CUT_DAYS")
        self._blade_rain_prob = _env_int("ALERT_BLADE_RAIN_PROB")
        self._weekly_enabled = _env_bool("ALERT_WEEKLY_DIGEST")

        # Arbitrage: track current tariff period to alert on transitions
        self._current_tariff: str = ""  # "day" or "night"

    def start(self) -> None:
        # Send startup notification with device summary and current status
        from . import __version__

        # Wait briefly for MQTT data to arrive
        _time_module = __import__('time')
        _time_module.sleep(5)

        dev_list = []
        for sn, dtype in self._device_types.items():
            label = self._device_label(sn)
            data = self._mqtt.get_device_data(sn)
            if "delta" in dtype:
                soc = self._get_float(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
                chg = self._get_float(data, "ems.chgRemainTime")
                dsg = self._get_float(data, "ems.dsgRemainTime")
                total_in = self._get_float(data, "pd.wattsInSum")
                total_out = self._get_float(data, "pd.wattsOutSum")
                if total_in > total_out and total_in > 0:
                    time_str = f"⚡Chg {self._fmt_time(chg)}" if chg > 0 else ""
                elif total_out > 0:
                    time_str = f"🔋Dsg {self._fmt_time(dsg)}" if dsg > 0 else ""
                else:
                    time_str = "💤Idle"
                dev_list.append(f"  • {label}: *{soc:.0f}%* {time_str}")
            elif "panel" in dtype:
                combined = self._get_float(data, "backupBatPer", "heartbeat.backupBatPer")
                grid = self._get_float(data, "gridSta", "heartbeat.gridSta")
                grid_str = "Grid ✅" if grid else "Grid ❌"
                dev_list.append(f"  • {label}: *{combined:.0f}%* {grid_str}")
            elif "blade" in dtype:
                BLADE_STATES = {0x500: "Idle", 0x501: "Charging", 0x502: "Mowing",
                                0x503: "Returning", 0x504: "Charging", 0x505: "Mapping",
                                0x506: "Paused", 0x507: "Error", 0x801: "Charging"}
                battery = self._get_float(data, "normalBleHeartBeat.batteryRemainPercent")
                state_code = int(self._get_float(data, "normalBleHeartBeat.robotState"))
                state = BLADE_STATES.get(state_code, f"0x{state_code:X}")
                dev_list.append(f"  • {label}: *{battery:.0f}%* 🤖 {state}")
            else:
                dev_list.append(f"  • {label}")

        devices_str = "\n".join(dev_list) if dev_list else "  No devices"
        self._send(
            f"🟢 *EcoFlow Dashboard v{__version__}*\n"
            f"Alerts active — monitoring:\n{devices_str}"
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("AlertManager started (Telegram chat=%s)", self._chat_id)

    def stop(self) -> None:
        self._send("🔴 *EcoFlow Dashboard Stopped*\nAlerts deactivated.")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def connected(self) -> bool:
        """Check if Telegram is reachable (cached last send result)."""
        return self._telegram_ok

    @property
    def last_error(self) -> str:
        return self._telegram_error

    def _run(self) -> None:
        # Wait for initial data
        self._stop.wait(15)

        # Initialize milestones to current SOC so we don't fire on startup
        for sn, dtype in self._device_types.items():
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue
            if "delta" in dtype:
                soc = self._get_float(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
                # Set solar milestones to current level
                for m in [50, 80, 90, 100]:
                    if soc >= m:
                        self._solar_charge_milestones[sn] = m
                # Set discharge milestones to current level
                for m in [80, 60, 40, 20, 15, 10, 5]:
                    if soc <= m:
                        self._discharge_milestones[sn] = m

        while not self._stop.is_set():
            try:
                self._check_all()
                self._check_tariff_change()
                self._check_daily_summary()
                self._check_monthly_report()
            except Exception:
                log.exception("Alert check failed")
            self._stop.wait(10)

    def _can_alert(self, key: str) -> bool:
        """Check if enough time has passed since last alert of this type."""
        last = self._cooldowns.get(key, 0)
        if time.time() - last < self._cooldown_secs:
            return False
        self._cooldowns[key] = time.time()
        return True

    def _get_session(self) -> requests.Session:
        """Get a requests session, optionally with SOCKS5 proxy for Telegram."""
        if not hasattr(self, "_tg_session"):
            self._tg_session = requests.Session()
            proxy = os.environ.get("TELEGRAM_PROXY", "")
            if proxy:
                self._tg_session.proxies = {"https": proxy, "http": proxy}
                log.info("Telegram using proxy: %s", proxy.split("@")[-1] if "@" in proxy else proxy)
        return self._tg_session

    def _send(self, text: str, retries: int = 3) -> None:
        """Send a Telegram message (Markdown format) with retry."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        session = self._get_session()
        for attempt in range(retries):
            try:
                r = session.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                }, timeout=15)
                if r.ok:
                    self._telegram_ok = True
                    self._telegram_error = ""
                    log.info("Telegram alert sent: %s", text[:80])
                    return
                self._telegram_error = f"HTTP {r.status_code}"
                log.warning("Telegram API error: %s %s", r.status_code, r.text[:100])
            except Exception as e:
                self._telegram_error = str(e)[:50]
                log.warning("Telegram send attempt %d/%d failed: %s", attempt + 1, retries, e)
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
        self._telegram_ok = False
        log.error("Failed to send Telegram alert after %d attempts", retries)

    def _device_label(self, sn: str) -> str:
        dtype = self._device_types.get(sn, "")
        if "delta" in dtype:
            label = "Delta Pro"
        elif "panel" in dtype:
            label = "Smart Home Panel"
        elif "blade" in dtype:
            label = "Blade"
        else:
            label = dtype
        return f"{label} ({sn[-6:]})"

    def _check_all(self) -> None:
        now = time.time()
        ts = datetime.now().strftime("%H:%M:%S")

        for sn, dtype in self._device_types.items():
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue

            prev = self._prev.get(sn, {})
            label = self._device_label(sn)

            # ── Device offline detection (use MQTT heartbeat, not data comparison) ──
            age = self._mqtt.last_update_age(sn)
            # Skip if never received data yet (startup) or MQTT disconnected
            if age != float("inf") and self._mqtt.connected and age > self._offline_timeout:
                if self._can_alert(f"offline:{sn}"):
                    mins = int(age / 60)
                    self._send(f"🔴 *DEVICE OFFLINE*\n{label}\nNo data for {mins} min")

            if "panel" in dtype:
                self._check_shp(sn, data, prev, label, ts)
            elif "delta" in dtype:
                self._check_delta(sn, data, prev, label, ts)
            elif "blade" in dtype:
                self._check_blade(sn, data, prev, label, ts)

            # Save current state
            self._prev[sn] = dict(data)

    def _check_shp(self, sn: str, data: dict, prev: dict, label: str, ts: str) -> None:
        # ── Grid outage / restore with detailed reporting ──
        grid_now = data.get("gridSta") or data.get("heartbeat.gridSta")
        grid_prev = prev.get("gridSta") or prev.get("heartbeat.gridSta")

        if grid_prev is not None and grid_now != grid_prev:
            if not grid_now and _env_bool("ALERT_GRID_OUTAGE"):
                # Grid just went DOWN — start tracking outage
                b1 = self._get_float(data, "energyInfos.0.batteryPercentage")
                b2 = self._get_float(data, "energyInfos.1.batteryPercentage")
                b2_conn = self._get_float(data, "energyInfos.1.stateBean.isConnect")
                total_load = sum(self._get_float(data, f"infoList.{i}.chWatt") for i in range(12))

                self._outage_state[sn] = {
                    "start": time.time(),
                    "start_ts": ts,
                    "b1_start": b1,
                    "b2_start": b2 if b2_conn else None,
                    "load_at_start": total_load,
                }

                batt_info = f"Batt 1: {b1:.0f}%"
                if b2_conn:
                    batt_info += f" | Batt 2: {b2:.0f}%"
                self._send(
                    f"⚡ *GRID OUTAGE*\n{label}\n"
                    f"Power lost at {ts}\n"
                    f"{batt_info}\n"
                    f"Load: {total_load:.0f}W"
                )

            elif grid_now and _env_bool("ALERT_GRID_RESTORE"):
                # Grid RESTORED — generate outage report
                outage = self._outage_state.pop(sn, None)
                b1_now = self._get_float(data, "energyInfos.0.batteryPercentage")
                b2_now = self._get_float(data, "energyInfos.1.batteryPercentage")

                if outage:
                    duration_s = time.time() - outage["start"]
                    duration = self._fmt_duration(duration_s)
                    b1_drain = outage["b1_start"] - b1_now

                    report = (
                        f"✅ *GRID RESTORED*\n{label}\n\n"
                        f"⏱ Duration: *{duration}*\n"
                        f"Started: {outage['start_ts']} → Restored: {ts}\n\n"
                        f"🔋 Battery drain:\n"
                        f"  Batt 1: {outage['b1_start']:.0f}% → {b1_now:.0f}% (*-{b1_drain:.0f}%*)"
                    )
                    if outage.get("b2_start") is not None:
                        b2_drain = outage["b2_start"] - b2_now
                        report += f"\n  Batt 2: {outage['b2_start']:.0f}% → {b2_now:.0f}% (*-{b2_drain:.0f}%*)"

                    report += f"\n\nLoad at outage start: {outage['load_at_start']:.0f}W"

                    # Log to SQLite
                    self._log_outage(sn, outage, duration_s, b1_now, b2_now)

                    self._send(report)
                else:
                    self._send(f"✅ *GRID RESTORED*\n{label}\nPower back at {ts}")

        # ── Battery low (per-battery) ──
        if self._battery_low > 0:
            for i in range(2):
                conn = self._get_float(data, f"energyInfos.{i}.stateBean.isConnect")
                if not conn:
                    continue
                soc = self._get_float(data, f"energyInfos.{i}.batteryPercentage")
                prev_soc = self._get_float(prev, f"energyInfos.{i}.batteryPercentage")
                if soc < self._battery_low and (prev_soc >= self._battery_low or not prev):
                    if self._can_alert(f"batt_low:{sn}:{i}"):
                        self._send(f"🪫 *BATTERY LOW*\n{label}\nBattery {i+1}: {soc:.0f}%")

    def _check_delta(self, sn: str, data: dict, prev: dict, label: str, ts: str) -> None:
        soc = self._get_float(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
        prev_soc = self._get_float(prev, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")

        # ── Discharge milestones ──
        total_in = self._get_float(data, "pd.wattsInSum")
        total_out = self._get_float(data, "pd.wattsOutSum")
        dsg_time = self._get_float(data, "ems.dsgRemainTime")
        chg_time = self._get_float(data, "ems.chgRemainTime")
        is_discharging = total_out > total_in and total_out > 0

        if is_discharging:
            milestone = self._get_discharge_milestone(sn, soc)
            if milestone is not None:
                time_str = f"\nRemaining: {self._fmt_time(dsg_time)}" if dsg_time > 0 else ""
                emoji = "🪫" if milestone <= 10 else "🔋"
                self._send(
                    f"{emoji} *BATTERY {int(soc)}%*\n{label}"
                    f"\nDischarging at {total_out:.0f}W{time_str}"
                )
        else:
            # Reset milestones when not discharging
            if sn in self._discharge_milestones and self._discharge_milestones[sn] < 100:
                self._discharge_milestones[sn] = 100

        # ── Solar charge milestones (50%, 80%, 90%, 100%) ──
        solar_watts = self._get_float(data, "mppt.inWatts") / 10
        is_solar_charging = solar_watts > 5 and total_in > total_out

        if is_solar_charging:
            milestone = self._get_solar_charge_milestone(sn, soc)
            if milestone is not None:
                time_str = f"\nFull in: {self._fmt_time(chg_time)}" if chg_time > 0 else ""
                self._send(
                    f"☀️ *SOLAR CHARGED TO {milestone}%*\n{label}"
                    f"\nSOC: {int(soc)}% — Solar: {solar_watts:.0f}W{time_str}"
                )
        else:
            # Reset when not solar charging
            if sn in self._solar_charge_milestones:
                last = self._solar_charge_milestones[sn]
                if soc < last - 5:  # SOC dropped, reset for next solar session
                    self._solar_charge_milestones[sn] = 0

        # ── Battery full ──
        if _env_bool("ALERT_BATTERY_FULL") and soc >= 100 and prev_soc < 100 and prev:
            if self._can_alert(f"batt_full:{sn}"):
                self._send(f"🔋 *BATTERY FULL*\n{label}\nFully charged at {ts}")

        # ── Charging complete ──
        if _env_bool("ALERT_CHARGE_COMPLETE"):
            chg_time = self._get_float(data, "ems.chgRemainTime")
            prev_chg = self._get_float(prev, "ems.chgRemainTime")
            total_in = self._get_float(data, "pd.wattsInSum")
            if prev_chg > 0 and chg_time == 0 and total_in == 0 and prev:
                if self._can_alert(f"chg_done:{sn}"):
                    self._send(f"⚡ *CHARGING COMPLETE*\n{label}\nSOC: {soc:.0f}% at {ts}")

        # ── High temperature (per-sensor thresholds) ──
        temp_checks = [
            ("Battery", self._get_float(data, "bmsMaster.temp"), self._high_temp_battery),
            ("Inverter", self._get_float(data, "inv.outTemp"), self._high_temp_inverter),
            ("MPPT", self._get_float(data, "mppt.mpptTemp"), self._high_temp_mppt),
        ]
        for name, t, threshold in temp_checks:
            if threshold > 0 and t > threshold:
                if self._can_alert(f"high_temp:{sn}:{name}"):
                    self._send(
                        f"🌡️ *HIGH TEMPERATURE*\n{label}\n{name}: {t:.0f}°C (threshold: {threshold}°C)"
                    )

    def _check_blade(self, sn: str, data: dict, prev: dict, label: str, ts: str) -> None:
        """Alerts for the EcoFlow Blade robotic mower."""
        BLADE_STATES = {
            0x500: "Idle", 0x501: "Charging", 0x502: "Mowing",
            0x503: "Returning", 0x504: "Charging", 0x505: "Mapping",
            0x506: "Paused", 0x507: "Error", 0x801: "Charging",
        }
        # Active errors live in errorCode.0..N — robotLowerr mirrors robotState.
        BLADE_ERRORS = {
            0x700: "Low battery — charge to 90% before working",  # 1792, app 0700
            0x701: "Work suspended — rain detected",               # 1793, app 0701
            0x503: "Out of bounds",                                # 1283, app 0503
            2001: "Motor overload", 2002: "Bumper triggered",
            2003: "Lifted from ground", 2004: "Stuck",
            2005: "Battery overheat", 2006: "Rain detected",
            2007: "GPS lost", 2008: "Out of mowing zone",
            2062: "RTK signal lost (cleared)",
        }

        # ── State change ──
        state_now = int(self._get_float(data, "normalBleHeartBeat.robotState"))
        state_prev = int(self._get_float(prev, "normalBleHeartBeat.robotState"))
        battery_now = self._get_float(data, "normalBleHeartBeat.batteryRemainPercent")
        if state_now != state_prev and state_prev != 0 and prev:
            now_label = BLADE_STATES.get(state_now, f"0x{state_now:X}")
            prev_label = BLADE_STATES.get(state_prev, f"0x{state_prev:X}")
            # Only notify on meaningful transitions
            interesting = state_now in (0x501, 0x502, 0x503, 0x504, 0x507, 0x801) or state_prev == 0x502
            if interesting and self._can_alert(f"blade_state:{sn}:{state_now}"):
                emoji = {0x501: "🔌", 0x502: "🌱", 0x503: "🏠", 0x504: "🔌", 0x507: "⚠️", 0x801: "🔌"}.get(state_now, "🤖")
                self._send(f"{emoji} *BLADE {now_label.upper()}*\n{label}\n{prev_label} → {now_label}")

            # Mower run tracking — entering/leaving 0x502 (Mowing).
            if state_now == 0x502 and state_prev != 0x502:
                self._mower_run_active[sn] = {
                    "start_ts": datetime.now().isoformat(timespec="seconds"),
                    "battery_start": battery_now,
                }
            elif state_prev == 0x502 and state_now != 0x502 and sn in self._mower_run_active:
                self._record_mower_run_end(sn, data, state_now)

        # ── Battery low (mower-specific threshold, more urgent) ──
        battery = self._get_float(data, "normalBleHeartBeat.batteryRemainPercent")
        battery_prev = self._get_float(prev, "normalBleHeartBeat.batteryRemainPercent")
        for threshold in (30, 15, 5):
            if battery <= threshold < battery_prev and prev:
                if self._can_alert(f"blade_batt:{sn}:{threshold}"):
                    emoji = "🪫" if threshold <= 15 else "🔋"
                    self._send(f"{emoji} *BLADE BATTERY {int(battery)}%*\n{label}\nNeeds to charge soon")
                break

        # ── Errors ──
        # Read the real error array. errorCode.0..N holds active codes; the
        # app shows each as 4-digit hex (0x700 → "0700"). robotLowerr just
        # mirrors robotState — never use it for errors.
        err_count = int(self._get_float(data, "normalBleHeartBeat.errorCount"))
        err_count_prev = int(self._get_float(prev, "normalBleHeartBeat.errorCount"))
        if err_count > err_count_prev and prev:
            active = []
            for i in range(8):
                c = int(self._get_float(data, f"normalBleHeartBeat.errorCode.{i}"))
                if c:
                    active.append(c)
            lines = []
            for c in active:
                msg = BLADE_ERRORS.get(c, "unknown code")
                lines.append(f"  • `{c:04X}` — {msg}")
                if c not in BLADE_ERRORS:
                    log.warning("Blade unknown error code on %s: %d (0x%X)", sn, c, c)
            key = active[0] if active else err_count
            if active and self._can_alert(f"blade_err:{sn}:{key}"):
                self._send(f"⚠️ *BLADE ERROR*\n{label}\n" + "\n".join(lines) +
                           f"\nActive errors: {err_count}")

        # ── Rain delay started ──
        rain_now = self._get_float(data, "normalBleHeartBeat.rainCountdown")
        rain_prev = self._get_float(prev, "normalBleHeartBeat.rainCountdown")
        if rain_now > 0 and rain_prev == 0 and prev:
            if self._can_alert(f"blade_rain:{sn}"):
                self._send(f"🌧️ *BLADE RAIN DELAY*\n{label}\nWaiting {int(rain_now)}s for rain to clear")

        # ── RTK signal lost (rtkState dropped from 4) ──
        rtk_now = int(self._get_float(data, "normalBleHeartBeat.rtkState"))
        rtk_prev = int(self._get_float(prev, "normalBleHeartBeat.rtkState"))
        if rtk_prev == 4 and rtk_now < 2 and prev:
            if self._can_alert(f"blade_rtk:{sn}"):
                self._send(f"🛰️ *BLADE RTK LOST*\n{label}\nGPS quality degraded — mowing may pause")

        # ── Stuck detection — no work-progress while in 0x502 Mowing ──
        if state_now == 0x502 and self._blade_stuck_min > 0:
            progress = int(self._get_float(data, "normalBleHeartBeat.currentWorkProgress"))
            seen = self._mower_progress_seen.get(sn)
            now_t = time.time()
            if seen is None or seen.get("progress") != progress:
                self._mower_progress_seen[sn] = {"progress": progress, "since_ts": now_t}
            else:
                idle_min = (now_t - seen["since_ts"]) / 60
                if idle_min >= self._blade_stuck_min and self._can_alert(f"blade_stuck:{sn}:{progress}"):
                    self._send(
                        f"🪦 *BLADE STUCK?*\n{label}\n"
                        f"Mowing for {int(idle_min)} min with no progress (still {progress}%). "
                        f"May be wedged or off-track."
                    )
        else:
            # Clear the timer when not mowing
            self._mower_progress_seen.pop(sn, None)

        # ── Geofence — alert if robot is far from base ──
        if self._blade_geofence_m > 0:
            r_lat = self._get_float(data, "signalInfo.robotLat")
            r_lng = self._get_float(data, "signalInfo.robotLng")
            b_lat = self._get_float(data, "signalInfo.baseLat")
            b_lng = self._get_float(data, "signalInfo.baseLng")
            if r_lat and r_lng and b_lat and b_lng:
                dist_m = _haversine_m(r_lat, r_lng, b_lat, b_lng)
                if dist_m > self._blade_geofence_m:
                    last = self._mower_geofence_alerted.get(sn, 0)
                    if time.time() - last > self._cooldown_secs:
                        self._mower_geofence_alerted[sn] = time.time()
                        self._send(
                            f"🛰️ *BLADE OUT OF RANGE*\n{label}\n"
                            f"{int(dist_m)} m from dock (limit {self._blade_geofence_m} m)"
                        )

        # ── Rain forecast — warn if mowing and rain probable in next few hours ──
        if state_now == 0x502 and self._solar_forecast and self._blade_rain_prob > 0:
            try:
                prob = self._solar_forecast.upcoming_rain_probability()
            except Exception:
                prob = 0
            if prob >= self._blade_rain_prob and self._can_alert(f"blade_rain_forecast:{sn}"):
                self._send(
                    f"🌧️ *BLADE — RAIN INCOMING*\n{label}\n"
                    f"{prob}% precipitation probability in the next 3h while mowing"
                )

    def _check_blade_edge_cut(self) -> None:
        """Once-a-day check: alert if edge cut hasn't been performed in N days.
        Called from _check_daily_summary so it runs at most once per day."""
        if self._blade_edge_cut_days <= 0 or not self._db_path:
            return
        import sqlite3
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=self._blade_edge_cut_days)).isoformat(timespec="seconds")
        for sn, dtype in self._device_types.items():
            if "blade" not in dtype:
                continue
            try:
                with sqlite3.connect(self._db_path) as conn:
                    # Did edgeCurrent change in the last N days? Compare max-min.
                    row = conn.execute(
                        "SELECT MIN(value), MAX(value) FROM snapshots "
                        "WHERE device_sn=? AND key='normalBleHeartBeat.edgeCurrent' "
                        "AND timestamp >= ?",
                        (sn, cutoff),
                    ).fetchone()
                    if row and row[0] is not None and row[0] == row[1]:
                        if self._can_alert(f"blade_edge_cut:{sn}"):
                            label = self._device_label(sn)
                            self._send(
                                f"✂️ *BLADE — EDGE CUT REMINDER*\n{label}\n"
                                f"No edge-cutting in the last {self._blade_edge_cut_days} days. "
                                f"Consider running a perimeter pass."
                            )
            except Exception:
                log.exception("Edge cut check failed for %s", sn)

    def _record_mower_run_end(self, sn: str, data: dict, end_state: int) -> None:
        """Persist a completed mowing run (0x502 → other state) to mower_runs."""
        run = self._mower_run_active.pop(sn, None)
        if not run or not self._db_path:
            return
        try:
            import sqlite3
            end_ts = datetime.now().isoformat(timespec="seconds")
            try:
                dur = int((datetime.fromisoformat(end_ts) -
                           datetime.fromisoformat(run["start_ts"])).total_seconds())
            except Exception:
                dur = 0
            battery_end = self._get_float(data, "normalBleHeartBeat.batteryRemainPercent")
            battery_used = max(0.0, run["battery_start"] - battery_end)
            area = self._get_float(data, "normalBleHeartBeat.currentWorkArea")
            err_count = int(self._get_float(data, "normalBleHeartBeat.errorCount"))
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO mower_runs (device_sn, start_time, end_time, duration_sec, "
                    "area_m2, battery_start, battery_end, battery_used, end_state, error_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sn, run["start_ts"], end_ts, dur, area,
                     run["battery_start"], battery_end, battery_used, end_state, err_count),
                )
            log.info("Mower run logged for %s: %ds, %.1fm², %.0f%% used, end=0x%X",
                     sn, dur, area, battery_used, end_state)
        except Exception:
            log.exception("Failed to record mower run for %s", sn)

    def _check_daily_summary(self) -> None:
        """Send daily summary at configured hour."""
        if not self._summary_hour:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today == self._last_summary_date:
            return
        if now.hour != int(self._summary_hour):
            return

        self._last_summary_date = today
        self._send(self._build_summary())
        # Piggy-back the once-a-day edge-cut reminder
        try:
            self._check_blade_edge_cut()
        except Exception:
            log.exception("edge-cut reminder failed")
        # Weekly anomaly digest on Mondays
        try:
            self._check_weekly_digest()
        except Exception:
            log.exception("weekly digest failed")

    def _check_monthly_report(self) -> None:
        """Send monthly energy/cost report on the 1st of each month."""
        if not self._monthly_enabled or not self._db_path:
            return
        now = datetime.now()
        month_key = now.strftime("%Y-%m")
        if month_key == self._last_monthly_date:
            return
        if now.day != 1:
            return
        # Send at the same hour as daily summary
        if self._summary_hour and now.hour != int(self._summary_hour):
            return

        self._last_monthly_date = month_key
        self._send(self._build_monthly_report())

    def _check_weekly_digest(self) -> None:
        """Send anomaly digest once a week (Mondays at the daily summary hour)."""
        if not self._weekly_enabled or not self._db_path:
            return
        now = datetime.now()
        if now.weekday() != 0:  # 0=Monday
            return
        wk = now.strftime("%G-W%V")
        if wk == self._last_weekly_date:
            return
        digest = self._build_weekly_digest()
        if digest:
            self._last_weekly_date = wk
            self._send(digest)

    def _build_weekly_digest(self) -> str:
        """Detect anomalies over the last 7 days and produce a digest."""
        import sqlite3
        from datetime import timedelta
        now = datetime.now()
        week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
        prev_start = (now - timedelta(days=14)).isoformat(timespec="seconds")
        lines = [f"📰 *Weekly Digest* — {now.strftime('%Y-%m-%d')}\n"]

        try:
            with sqlite3.connect(self._db_path) as conn:
                # Outages last week
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(duration_sec),0) FROM outages "
                    "WHERE start_time >= ?", (week_ago,)).fetchone()
                if row and row[0]:
                    n, secs = row
                    lines.append(f"⚡ *Grid outages*: {n} event(s), total {secs//60} min")

                # Mower runs last week vs previous week
                for sn, dtype in self._device_types.items():
                    if "blade" not in dtype:
                        continue
                    label = self._device_label(sn)
                    cur = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(area_m2),0), COALESCE(SUM(duration_sec),0), "
                        "COALESCE(SUM(error_count),0) FROM mower_runs "
                        "WHERE device_sn=? AND start_time >= ? AND end_time IS NOT NULL",
                        (sn, week_ago)).fetchone()
                    pcur = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(area_m2),0) FROM mower_runs "
                        "WHERE device_sn=? AND start_time >= ? AND start_time < ? "
                        "AND end_time IS NOT NULL", (sn, prev_start, week_ago)).fetchone()
                    if cur and cur[0]:
                        n, area, dur, errs = cur
                        prev_n, prev_area = (pcur or (0, 0))
                        delta = ""
                        if prev_n:
                            pct = ((area - prev_area) / prev_area) * 100
                            arrow = "📈" if pct > 0 else "📉"
                            delta = f"  ({arrow} {pct:+.0f}% vs prior week)"
                        lines.append(f"\n🤖 *{label}*")
                        lines.append(f"  {n} run(s), {area:.0f} m², {dur//60} min{delta}")
                        if n:
                            lines.append(f"  Avg: {area/n:.1f} m²/run, {dur//n//60} min/run")
                        # Battery efficiency: m² per % battery
                        eff_row = conn.execute(
                            "SELECT SUM(area_m2)/NULLIF(SUM(battery_used),0) "
                            "FROM mower_runs WHERE device_sn=? AND start_time >= ? "
                            "AND end_time IS NOT NULL AND battery_used > 0",
                            (sn, week_ago)).fetchone()
                        if eff_row and eff_row[0]:
                            lines.append(f"  Efficiency: {eff_row[0]:.1f} m² per % battery")
                        if errs:
                            lines.append(f"  ⚠️ {errs} error event(s)")

                    # Edge-cut reminder snapshot (info, not alert)
                    edge_row = conn.execute(
                        "SELECT MAX(timestamp) FROM snapshots WHERE device_sn=? "
                        "AND key='normalBleHeartBeat.edgeCurrent' AND value > 0",
                        (sn,)).fetchone()
                    if edge_row and edge_row[0]:
                        try:
                            last = datetime.fromisoformat(edge_row[0])
                            days = (now - last).days
                            if days > self._blade_edge_cut_days:
                                lines.append(f"  ✂️ Edge cut: last {days}d ago — overdue")
                        except Exception:
                            pass

                # Delta Pro battery cycle deltas
                for sn, dtype in self._device_types.items():
                    if "delta" not in dtype:
                        continue
                    cur = conn.execute(
                        "SELECT MIN(value), MAX(value) FROM snapshots "
                        "WHERE device_sn=? AND key='bmsMaster.cycles' AND timestamp >= ?",
                        (sn, week_ago)).fetchone()
                    if cur and cur[0] is not None:
                        delta = (cur[1] or 0) - (cur[0] or 0)
                        if delta >= 7:  # more than ~1/day is unusual
                            label = self._device_label(sn)
                            lines.append(f"\n🔋 *{label}*: +{delta} battery cycles this week (heavy use)")
        except Exception:
            log.exception("weekly digest query failed")
            return ""

        if len(lines) == 1:
            return ""  # Nothing notable
        return "\n".join(lines)

    def _build_monthly_report(self) -> str:
        """Build monthly energy consumption and cost report from SQLite."""
        import sqlite3
        now = datetime.now()
        # Previous month
        if now.month == 1:
            prev_year, prev_month = now.year - 1, 12
        else:
            prev_year, prev_month = now.year, now.month - 1
        month_start = f"{prev_year}-{prev_month:02d}-01"
        month_end = f"{now.year}-{now.month:02d}-01"
        month_name = datetime(prev_year, prev_month, 1).strftime("%B %Y")

        lines = [f"📅 *Monthly Report — {month_name}*\n"]

        try:
            with sqlite3.connect(self._db_path) as conn:
                for sn, dtype in self._device_types.items():
                    label = self._device_label(sn)

                    if "panel" in dtype:
                        # Get daily grid consumption snapshots
                        rows = conn.execute(
                            "SELECT DATE(timestamp) as day, MAX(value) - MIN(value) as daily_wh "
                            "FROM snapshots WHERE device_sn=? AND key='gridDayWatth' "
                            "AND timestamp >= ? AND timestamp < ? "
                            "GROUP BY day ORDER BY day",
                            (sn, month_start, month_end),
                        ).fetchall()

                        if not rows:
                            # Fallback: use max gridDayWatth per day
                            rows = conn.execute(
                                "SELECT DATE(timestamp) as day, MAX(value) as daily_wh "
                                "FROM snapshots WHERE device_sn=? AND key='gridDayWatth' "
                                "AND timestamp >= ? AND timestamp < ? "
                                "GROUP BY day ORDER BY day",
                                (sn, month_start, month_end),
                            ).fetchall()

                        total_kwh = sum(r[1] for r in rows) / 1000 if rows else 0
                        days = len(rows)
                        avg_kwh = total_kwh / days if days > 0 else 0

                        lines.append(f"*{label}*")
                        lines.append(f"  Grid: *{total_kwh:.1f} kWh* ({days} days)")
                        lines.append(f"  Daily avg: {avg_kwh:.1f} kWh")

                        if self._energy_rate > 0:
                            total_cost = total_kwh * self._energy_rate
                            avg_cost = avg_kwh * self._energy_rate
                            lines.append(
                                f"  Cost: *{self._energy_currency}{total_cost:.2f}* "
                                f"(avg {self._energy_currency}{avg_cost:.2f}/day)"
                            )
                        lines.append("")

                    elif "delta" in dtype:
                        # Get lifetime energy change over the month
                        for metric, lbl in [("pd.chgPowerAc", "AC Charged"), ("pd.chgSunPower", "Solar Charged")]:
                            row = conn.execute(
                                "SELECT MIN(value), MAX(value) FROM snapshots "
                                "WHERE device_sn=? AND key=? AND timestamp >= ? AND timestamp < ?",
                                (sn, metric, month_start, month_end),
                            ).fetchone()
                            if row and row[0] is not None and row[1] is not None:
                                delta_kwh = (row[1] - row[0]) / 1000
                                if delta_kwh > 0:
                                    lines.append(f"  {lbl}: {delta_kwh:.1f} kWh")

        except Exception as e:
            lines.append(f"  _Error reading history: {e}_")

        if len(lines) == 1:
            lines.append("  No historical data available for last month.")

        return "\n".join(lines)

    def _build_summary(self) -> str:
        """Build a daily summary message with all device stats."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"📊 *Daily Summary* — {now}\n"]

        for sn, dtype in self._device_types.items():
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue
            label = self._device_label(sn)

            if "delta" in dtype:
                soc = self._get_float(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
                soh = self._get_float(data, "bmsMaster.soh")
                volts = self._get_float(data, "bmsMaster.vol") / 1000
                cycles = int(self._get_float(data, "bmsMaster.cycles"))
                total_in = self._get_float(data, "pd.wattsInSum")
                total_out = self._get_float(data, "pd.wattsOutSum")
                temp = self._get_float(data, "bmsMaster.temp")
                chg_ac = self._get_float(data, "pd.chgPowerAc") / 1000
                chg_sun = self._get_float(data, "pd.chgSunPower") / 1000

                status = "⚡ Charging" if total_in > total_out else "🔋 Discharging" if total_out > 0 else "💤 Idle"

                lines.append(f"*{label}*")
                lines.append(f"  {status} — SOC: *{soc:.0f}%* — Health: {soh:.0f}%")
                lines.append(f"  {volts:.1f}V | {temp:.0f}°C | {cycles} cycles")
                lines.append(f"  In: {total_in:.0f}W | Out: {total_out:.0f}W")
                lines.append(f"  Lifetime: AC {chg_ac:.0f} kWh | Solar {chg_sun:.0f} kWh")
                lines.append("")

            elif "panel" in dtype:
                grid = self._get_float(data, "gridSta", "heartbeat.gridSta")
                grid_day = self._get_float(data, "gridDayWatth", "heartbeat.gridDayWatth")
                backup_day = self._get_float(data, "backupDayWatth", "heartbeat.backupDayWatth")
                combined = self._get_float(data, "backupBatPer", "heartbeat.backupBatPer")
                total_load = sum(
                    self._get_float(data, f"infoList.{i}.chWatt")
                    for i in range(12)
                )

                lines.append(f"*{label}*")
                lines.append(f"  Grid: {'✅ ON' if grid else '❌ OFF'} — Combined: *{combined:.0f}%*")
                cost_str = ""
                if self._energy_rate > 0 and grid_day > 0:
                    cost = (grid_day / 1000) * self._energy_rate
                    cost_str = f" ({self._energy_currency}{cost:.2f})"
                lines.append(f"  Grid today: {grid_day/1000:.2f} kWh{cost_str} | Backup: {backup_day/1000:.2f} kWh")
                lines.append(f"  Current load: {total_load:.0f} W")

                for i in range(2):
                    conn = self._get_float(data, f"energyInfos.{i}.stateBean.isConnect")
                    if conn:
                        bsoc = self._get_float(data, f"energyInfos.{i}.batteryPercentage")
                        btemp = self._get_float(data, f"energyInfos.{i}.emsBatTemp")
                        lines.append(f"  Batt {i+1}: {bsoc:.0f}% | {btemp:.0f}°C")
                lines.append("")

            elif "blade" in dtype:
                BLADE_STATES = {
                    0x500: "Idle", 0x501: "Charging", 0x502: "Mowing",
                    0x503: "Returning", 0x504: "Charging", 0x505: "Mapping",
                    0x506: "Paused", 0x507: "Error", 0x801: "Charging",
                }
                battery = self._get_float(data, "normalBleHeartBeat.batteryRemainPercent")
                state_code = int(self._get_float(data, "normalBleHeartBeat.robotState"))
                state_label = BLADE_STATES.get(state_code, f"0x{state_code:X}")
                err_count = int(self._get_float(data, "normalBleHeartBeat.errorCount"))
                rtk_state = int(self._get_float(data, "normalBleHeartBeat.rtkState"))
                rtk_label = {0:"no fix",1:"single",2:"DGPS",3:"RTK float",4:"RTK fixed"}.get(rtk_state, "?")

                lines.append(f"*{label}*")
                lines.append(f"  🤖 {state_label} — Battery: *{battery:.0f}%* — RTK: {rtk_label}")
                if err_count > 0:
                    lines.append(f"  ⚠️ {err_count} active error(s)")

                # Today's run history (from mower_runs table)
                today_runs, today_area, today_dur, today_batt = 0, 0.0, 0, 0.0
                week_runs, week_area = 0, 0.0
                lifetime_runs, lifetime_area = 0, 0.0
                if self._db_path:
                    try:
                        import sqlite3
                        from datetime import timedelta
                        today = datetime.now().strftime("%Y-%m-%d")
                        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                        with sqlite3.connect(self._db_path) as conn:
                            row = conn.execute(
                                "SELECT COUNT(*), COALESCE(SUM(area_m2),0), COALESCE(SUM(duration_sec),0), "
                                "COALESCE(SUM(battery_used),0) FROM mower_runs "
                                "WHERE device_sn=? AND start_time >= ? AND end_time IS NOT NULL",
                                (sn, today),
                            ).fetchone()
                            if row:
                                today_runs, today_area, today_dur, today_batt = row
                            row = conn.execute(
                                "SELECT COUNT(*), COALESCE(SUM(area_m2),0) FROM mower_runs "
                                "WHERE device_sn=? AND start_time >= ? AND end_time IS NOT NULL",
                                (sn, week_ago),
                            ).fetchone()
                            if row:
                                week_runs, week_area = row
                            row = conn.execute(
                                "SELECT COUNT(*), COALESCE(SUM(area_m2),0) FROM mower_runs "
                                "WHERE device_sn=? AND end_time IS NOT NULL",
                                (sn,),
                            ).fetchone()
                            if row:
                                lifetime_runs, lifetime_area = row
                    except Exception:
                        log.exception("Failed to read mower_runs for summary")

                if today_runs:
                    lines.append(f"  📅 Today: *{today_runs}* run(s) — {today_area:.0f} m² — {today_dur//60} min — {today_batt:.0f}% used")
                else:
                    lines.append(f"  📅 Today: no runs")
                if week_runs:
                    lines.append(f"  🗓 7d: {week_runs} run(s), {week_area:.0f} m²")
                if lifetime_runs:
                    lines.append(f"  ♾ Lifetime: {lifetime_runs} run(s), {lifetime_area:.0f} m²")
                lines.append("")

        return "\n".join(lines)

    def _get_solar_charge_milestone(self, sn: str, soc: float) -> int | None:
        """Return SOC milestone if crossed while solar charging.
        Milestones: 50, 80, 90, 100
        """
        milestones = [50, 80, 90, 100]
        last = self._solar_charge_milestones.get(sn, 0)

        for m in milestones:
            if soc >= m and m > last:
                self._solar_charge_milestones[sn] = m
                return m

        return None

    @staticmethod
    def _fmt_time(minutes: float) -> str:
        m = int(minutes)
        if m <= 0:
            return "--"
        if m >= 1440:
            return f"{m // 1440}d {(m % 1440) // 60}h"
        if m >= 60:
            return f"{m // 60}h {m % 60}m"
        return f"{m}m"

    def _get_discharge_milestone(self, sn: str, soc: float) -> int | None:
        """Return SOC milestone if crossed, else None.
        Above 20%: milestones at 80, 60, 40, 20
        Below 20%: milestones at 15, 10, 5
        """
        if soc >= 20:
            milestones = [80, 60, 40, 20]
        else:
            milestones = [15, 10, 5]

        current_milestone = None
        for m in milestones:
            if soc <= m:
                current_milestone = m

        if current_milestone is None:
            return None

        last = self._discharge_milestones.get(sn, 100)
        if current_milestone < last:
            self._discharge_milestones[sn] = current_milestone
            return current_milestone

        # SOC went back up (charging) — reset tracking
        if soc > last + 5:
            self._discharge_milestones[sn] = 100

        return None

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        h = s // 3600
        m = (s % 3600) // 60
        if h >= 24:
            d = h // 24
            h = h % 24
            return f"{d}d {h}h {m}m"
        return f"{h}h {m}m"

    def _log_outage(self, sn: str, outage: dict, duration_s: float, b1_end: float, b2_end: float) -> None:
        """Log outage event to SQLite."""
        if not self._db_path:
            return
        try:
            import sqlite3
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS outages ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  sn TEXT, start_ts TEXT, end_ts TEXT,"
                    "  duration_s REAL,"
                    "  b1_start REAL, b1_end REAL,"
                    "  b2_start REAL, b2_end REAL,"
                    "  load_w REAL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO outages (sn, start_ts, end_ts, duration_s, b1_start, b1_end, b2_start, b2_end, load_w) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sn, outage["start_ts"], datetime.now().strftime("%H:%M:%S"),
                     duration_s, outage["b1_start"], b1_end,
                     outage.get("b2_start"), b2_end,
                     outage.get("load_at_start", 0)),
                )
        except Exception as e:
            log.warning("Failed to log outage: %s", e)

    def _check_tariff_change(self) -> None:
        """Alert on day/night tariff transitions for battery arbitrage."""
        if not self._energy_rate or not getattr(self, '_energy_rate_night', 0):
            return
        hour = datetime.now().hour
        day_start = int(os.environ.get("ENERGY_DAY_START", "7"))
        day_end = int(os.environ.get("ENERGY_DAY_END", "23"))

        if day_start <= hour < day_end:
            tariff = "day"
        else:
            tariff = "night"

        if self._current_tariff and tariff != self._current_tariff:
            rate_night = float(os.environ.get("ENERGY_RATE_NIGHT", "0"))
            currency = os.environ.get("ENERGY_CURRENCY", "$")

            if tariff == "night":
                self._send(
                    f"🌙 *NIGHT RATE STARTED*\n"
                    f"Rate: {currency}{rate_night}/kWh\n"
                    f"💡 Good time to charge from grid"
                )
            else:
                self._send(
                    f"☀️ *DAY RATE STARTED*\n"
                    f"Rate: {currency}{self._energy_rate}/kWh\n"
                    f"💡 Consider pausing grid charge"
                )

        self._current_tariff = tariff

    @staticmethod
    def _get_float(data: dict, *keys: str) -> float:
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0
