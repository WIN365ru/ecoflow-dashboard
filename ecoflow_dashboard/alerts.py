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
    "ALERT_MONTHLY_REPORT": "1",     # Send monthly energy/cost report on 1st of month
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
        energy_rate: float = 0.0,
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
        self._high_temp = _env_int("ALERT_HIGH_TEMP")
        self._offline_timeout = _env_int("ALERT_OFFLINE_TIMEOUT")

        # Track last data timestamp per device
        self._last_data_ts: dict[str, float] = {}

        # Telegram connection status
        self._telegram_ok: bool = False
        self._telegram_error: str = ""

        # Energy cost
        self._energy_rate = energy_rate
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

        while not self._stop.is_set():
            try:
                self._check_all()
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
    def _get_float(data: dict, *keys: str) -> float:
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0
