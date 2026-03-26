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
    "ALERT_HIGH_TEMP": "45",         # °C threshold
    "ALERT_OFFLINE_TIMEOUT": "300",  # seconds
    "ALERT_COOLDOWN": "1800",        # 30 min between repeated alerts
    "ALERT_DAILY_SUMMARY": "20",     # Hour (0-23) to send daily summary, empty to disable
}


def _env_int(key: str) -> int:
    return int(os.environ.get(key, DEFAULTS.get(key, "0")))


def _env_bool(key: str) -> bool:
    return os.environ.get(key, DEFAULTS.get(key, "0")) not in ("0", "false", "no", "")


class AlertManager:
    def __init__(
        self,
        mqtt_client: EcoFlowMqttClient,
        device_types: dict[str, str],
        device_names: dict[str, str],
        telegram_token: str,
        telegram_chat_id: str,
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
        self._high_temp = _env_int("ALERT_HIGH_TEMP")
        self._offline_timeout = _env_int("ALERT_OFFLINE_TIMEOUT")

        # Track last data timestamp per device
        self._last_data_ts: dict[str, float] = {}

        # Telegram connection status
        self._telegram_ok: bool = False
        self._telegram_error: str = ""

        # Daily summary
        self._summary_hour = os.environ.get("ALERT_DAILY_SUMMARY", DEFAULTS["ALERT_DAILY_SUMMARY"])
        self._last_summary_date: str = ""

    def start(self) -> None:
        # Send startup notification with device summary
        from . import __version__
        dev_list = []
        for sn, dtype in self._device_types.items():
            label = self._device_label(sn)
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

        while not self._stop.is_set():
            try:
                self._check_all()
                self._check_daily_summary()
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

    def _send(self, text: str, retries: int = 3) -> None:
        """Send a Telegram message (Markdown format) with retry."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        for attempt in range(retries):
            try:
                r = requests.post(url, json={
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
        label = "Delta Pro" if "delta" in dtype else "Smart Home Panel" if "panel" in dtype else dtype
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

            # ── Device offline detection ──
            if data != self._prev.get(sn):
                self._last_data_ts[sn] = now
            elif now - self._last_data_ts.get(sn, now) > self._offline_timeout:
                if self._can_alert(f"offline:{sn}"):
                    mins = int((now - self._last_data_ts[sn]) / 60)
                    self._send(f"🔴 *DEVICE OFFLINE*\n{label}\nNo data for {mins} min")

            if "panel" in dtype:
                self._check_shp(sn, data, prev, label, ts)
            elif "delta" in dtype:
                self._check_delta(sn, data, prev, label, ts)

            # Save current state
            self._prev[sn] = dict(data)

    def _check_shp(self, sn: str, data: dict, prev: dict, label: str, ts: str) -> None:
        # ── Grid outage / restore ──
        grid_now = data.get("gridSta") or data.get("heartbeat.gridSta")
        grid_prev = prev.get("gridSta") or prev.get("heartbeat.gridSta")

        if grid_prev is not None and grid_now != grid_prev:
            if not grid_now and _env_bool("ALERT_GRID_OUTAGE"):
                if self._can_alert(f"grid_out:{sn}"):
                    # Get battery levels for context
                    b1 = self._get_float(data, "energyInfos.0.batteryPercentage")
                    b2 = self._get_float(data, "energyInfos.1.batteryPercentage")
                    batt_info = f"Batt 1: {b1:.0f}%"
                    b2_conn = self._get_float(data, "energyInfos.1.stateBean.isConnect")
                    if b2_conn:
                        batt_info += f" | Batt 2: {b2:.0f}%"
                    self._send(f"⚡ *GRID OUTAGE*\n{label}\nPower lost at {ts}\n{batt_info}")

            elif grid_now and _env_bool("ALERT_GRID_RESTORE"):
                if self._can_alert(f"grid_on:{sn}"):
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

        # ── Battery low ──
        if self._battery_low > 0 and soc < self._battery_low:
            if prev_soc >= self._battery_low or not prev:
                if self._can_alert(f"batt_low:{sn}"):
                    self._send(f"🪫 *BATTERY LOW*\n{label}\nSOC: {soc:.0f}%")

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

        # ── High temperature ──
        if self._high_temp > 0:
            temps = {
                "Battery": self._get_float(data, "bmsMaster.temp"),
                "Inverter": self._get_float(data, "inv.outTemp"),
                "MPPT": self._get_float(data, "mppt.mpptTemp"),
            }
            for name, t in temps.items():
                if t > self._high_temp:
                    if self._can_alert(f"high_temp:{sn}:{name}"):
                        self._send(
                            f"🌡️ *HIGH TEMPERATURE*\n{label}\n{name}: {t:.0f}°C (threshold: {self._high_temp}°C)"
                        )

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
                lines.append(f"  Grid today: {grid_day/1000:.2f} kWh | Backup: {backup_day/1000:.2f} kWh")
                lines.append(f"  Current load: {total_load:.0f} W")

                for i in range(2):
                    conn = self._get_float(data, f"energyInfos.{i}.stateBean.isConnect")
                    if conn:
                        bsoc = self._get_float(data, f"energyInfos.{i}.batteryPercentage")
                        btemp = self._get_float(data, f"energyInfos.{i}.emsBatTemp")
                        lines.append(f"  Batt {i+1}: {bsoc:.0f}% | {btemp:.0f}°C")
                lines.append("")

        return "\n".join(lines)

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
