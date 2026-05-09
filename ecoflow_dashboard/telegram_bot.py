"""Telegram bot for controlling EcoFlow devices via inline keyboards."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

# Inline keyboard helper
def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Build inline keyboard markup. Each tuple is (text, callback_data)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        mqtt_client: EcoFlowMqttClient,
        device_types: dict[str, str],
        device_names: dict[str, str],
        energy_rate: float = 0.0,
        energy_rate_night: float = 0.0,
        energy_day_start: int = 7,
        energy_day_end: int = 23,
        energy_currency: str = "$",
        circuit_names: list[str] | None = None,
        db_path: str = "ecoflow_history.db",
    ) -> None:
        self._token = token
        self._chat_id = str(chat_id)
        self._mqtt = mqtt_client
        self._device_types = device_types
        self._device_names = device_names
        self._energy_rate = energy_rate
        self._energy_rate_night = energy_rate_night
        self._energy_day_start = energy_day_start
        self._energy_day_end = energy_day_end
        self._currency = energy_currency
        self._circuit_names = circuit_names or []
        self._db_path = db_path
        self._solar_forecast = None  # set externally if available
        self._base = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Session with optional SOCKS5 proxy
        self._session = requests.Session()
        proxy = os.environ.get("TELEGRAM_PROXY", "")
        if proxy:
            self._session.proxies = {"https": proxy, "http": proxy}
            log.info("Telegram bot using proxy: %s", proxy.split("@")[-1] if "@" in proxy else proxy)

    def start(self) -> None:
        self._register_commands()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="telegram-bot")
        self._thread.start()
        log.info("Telegram bot polling started")

    def _register_commands(self) -> None:
        """Publish the bot's command list so Telegram clients show a menu/autocomplete.
        Without this call, /commands and the menu button stay empty even though
        the bot handles the commands."""
        cmds = [
            {"command": "status",   "description": "Device status overview"},
            {"command": "control",  "description": "Control devices"},
            {"command": "circuits", "description": "Smart Panel circuit loads"},
            {"command": "solar",    "description": "Solar production"},
            {"command": "forecast", "description": "Solar forecast (today + tomorrow)"},
            {"command": "cost",     "description": "Energy costs (today + month)"},
            {"command": "blade",    "description": "Mower runs history"},
            {"command": "blade_debug", "description": "Dump all Blade telemetry keys"},
            {"command": "help",     "description": "Show all commands"},
        ]
        try:
            self._session.post(
                f"{self._base}/setMyCommands",
                json={"commands": cmds},
                timeout=10,
            )
            log.info("Telegram bot commands registered (%d)", len(cmds))
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
                for u in updates:
                    self._offset = u["update_id"] + 1
                    if "message" in u:
                        self._handle_message(u["message"])
                    elif "callback_query" in u:
                        self._handle_callback(u["callback_query"])
            except Exception as e:
                log.warning("Bot poll error: %s", e)
                time.sleep(5)

    def _get_updates(self) -> list[dict]:
        try:
            r = self._session.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 30, "allowed_updates": '["message","callback_query"]'},
                timeout=35,
            )
            data = r.json()
            return data.get("result", [])
        except requests.RequestException:
            return []

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            return  # ignore messages from other chats
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        cmd = text.split()[0].lower().replace("@", " ").split(" ")[0]
        handlers = {
            "/start": self._cmd_help,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/s": self._cmd_status,
            "/control": self._cmd_control,
            "/c": self._cmd_control,
            "/circuits": self._cmd_circuits,
            "/solar": self._cmd_solar,
            "/forecast": self._cmd_forecast,
            "/f": self._cmd_forecast,
            "/cost": self._cmd_cost,
            "/blade": self._cmd_blade,
            "/b": self._cmd_blade,
            "/blade_debug": self._cmd_blade_debug,
        }
        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send("Unknown command. Try /help")

    def _handle_callback(self, cb: dict) -> None:
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            return
        data = cb.get("data", "")
        msg_id = cb.get("message", {}).get("message_id")

        # Acknowledge callback
        try:
            self._session.post(f"{self._base}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=5)
        except Exception:
            pass

        # Route callback
        if data.startswith("dev:"):
            self._cb_device_control(data[4:], msg_id)
        elif data.startswith("cmd:"):
            parts = data[4:].split(":", 1)
            if len(parts) == 2:
                self._cb_execute_command(parts[0], parts[1], msg_id)
        elif data.startswith("ct:"):
            # Circuit toggle: ct:SN:CH
            parts = data[3:].split(":", 1)
            if len(parts) == 2:
                self._cb_circuit_toggle(parts[0], int(parts[1]), msg_id)
        elif data == "back":
            self._cmd_control(msg_id)
        elif data == "close":
            self._delete_msg(msg_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _cmd_help(self) -> None:
        self._send(
            "⚡ *EcoFlow Dashboard Bot*\n\n"
            "/status — Device status overview\n"
            "/control — Control devices\n"
            "/circuits — Smart Panel circuits\n"
            "/solar — Solar production\n"
            "/forecast — Solar forecast (tomorrow)\n"
            "/cost — Energy costs\n"
            "/blade — Mower runs history\n"
            "/help — This message"
        )

    def _cmd_status(self) -> None:
        lines = []
        for sn, dtype in self._device_types.items():
            data = self._mqtt.get_device_data(sn)
            label = self._label(sn)
            age = self._mqtt.last_update_age(sn)
            stale = " ⚠️STALE" if age > 120 else ""

            if "delta" in dtype:
                soc = self._gf(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
                solar = self._gf(data, "mppt.inWatts") / 10
                total_in = self._gf(data, "pd.wattsInSum")
                total_out = self._gf(data, "pd.wattsOutSum")
                chg = self._gf(data, "ems.chgRemainTime")
                dsg = self._gf(data, "ems.dsgRemainTime")
                volts = self._gf(data, "bmsMaster.vol") / 1000
                temp = self._gf(data, "bmsMaster.temp")

                if total_in > total_out and total_in > 0:
                    state = f"⚡Chg {self._fmt_time(chg)}"
                elif total_out > 0:
                    state = f"🔋Dsg {self._fmt_time(dsg)}"
                else:
                    state = "💤Idle"

                solar_str = f"  ☀️{solar:.0f}W" if solar > 1 else ""
                lines.append(
                    f"🔋 *{label}*: *{soc:.0f}%* {state}{stale}\n"
                    f"    In: {total_in:.0f}W  Out: {total_out:.0f}W{solar_str}\n"
                    f"    {volts:.1f}V  {temp:.0f}°C"
                )
            elif "panel" in dtype:
                combined = self._gf(data, "backupBatPer", "heartbeat.backupBatPer")
                grid = self._gf(data, "gridSta", "heartbeat.gridSta")
                grid_day = self._gf(data, "gridDayWatth", "heartbeat.gridDayWatth")
                total_load = sum(
                    self._gf(data, f"infoList.{i}.chWatt") for i in range(12)
                )
                grid_str = "Grid ✅" if grid else "Grid ❌"
                lines.append(
                    f"🏠 *{label}*: *{combined:.0f}%* {grid_str}{stale}\n"
                    f"    Load: {total_load:.0f}W  Grid today: {grid_day/1000:.1f}kWh"
                )
            elif "blade" in dtype:
                BLADE_STATES = {
                    0x500: "Idle", 0x501: "Standby", 0x502: "Mowing",
                    0x503: "Returning", 0x504: "Charging", 0x505: "Mapping",
                    0x506: "Paused", 0x507: "Error", 0x801: "Charging",
                }
                battery = self._gf(data, "normalBleHeartBeat.batteryRemainPercent")
                state_code = int(self._gf(data, "normalBleHeartBeat.robotState"))
                state = BLADE_STATES.get(state_code, f"0x{state_code:X}")
                err_count = int(self._gf(data, "normalBleHeartBeat.errorCount"))
                rain_cd = int(self._gf(data, "normalBleHeartBeat.rainCountdown"))
                rtk_state = int(self._gf(data, "normalBleHeartBeat.rtkState"))
                rtk_label = {0:"no fix",1:"single",2:"DGPS",3:"RTK float",4:"RTK fixed"}.get(rtk_state, "?")
                work_area = self._gf(data, "normalBleHeartBeat.currentWorkArea")
                work_prog = int(self._gf(data, "normalBleHeartBeat.currentWorkProgress"))
                extras = []
                if work_area > 0:
                    extras.append(f"Job: {work_area:.1f}m² ({work_prog}%)")
                if rain_cd > 0:
                    extras.append(f"🌧️ {rain_cd}s")
                if err_count > 0:
                    extras.append(f"⚠️ {err_count} err")
                extras_str = ("\n    " + "  ".join(extras)) if extras else ""
                lines.append(
                    f"🤖 *{label}*: *{battery:.0f}%* {state}{stale}\n"
                    f"    RTK: {rtk_label}{extras_str}"
                )

        ts = datetime.now().strftime("%H:%M")
        self._send(f"📊 *Status* ({ts})\n\n" + "\n\n".join(lines))

    def _cmd_control(self, edit_msg_id: int | None = None) -> None:
        rows = []
        for sn, dtype in self._device_types.items():
            label = self._label(sn)
            emoji = "🔋" if "delta" in dtype else "🏠"
            rows.append([(f"{emoji} {label}", f"dev:{sn}")])
        rows.append([("❌ Close", "close")])
        self._send("Select device:", reply_markup=_kb(rows), edit_msg_id=edit_msg_id)

    def _cb_device_control(self, sn: str, msg_id: int) -> None:
        dtype = self._device_types.get(sn, "")
        label = self._label(sn)
        data = self._mqtt.get_device_data(sn)

        if "delta" in dtype:
            ac = self._gf(data, "inv.cfgAcEnabled")
            dc = self._gf(data, "mppt.carState")
            xb = self._gf(data, "inv.cfgAcXboost")
            beep = self._gf(data, "pd.beepState")
            chg_w = self._gf(data, "inv.cfgSlowChgWatts")
            max_chg = self._gf(data, "ems.maxChargeSoc")
            soc = self._gf(data, "ems.lcdShowSoc", "bmsMaster.f32ShowSoc")

            rows = [
                [
                    (f"{'🟢' if ac else '🔴'} AC", f"cmd:{sn}:a"),
                    (f"{'🟢' if dc else '🔴'} DC", f"cmd:{sn}:d"),
                    (f"{'🟢' if xb else '🔴'} XBoost", f"cmd:{sn}:x"),
                ],
                [
                    (f"🔊 Beep {'ON' if not beep else 'OFF'}", f"cmd:{sn}:b"),
                    (f"⚡ Chg {'PAUSED' if chg_w == 0 else f'{chg_w:.0f}W'}", f"cmd:{sn}:c"),
                ],
                [
                    ("Chg +5%", f"cmd:{sn}:="),
                    (f"Limit {max_chg:.0f}%", "noop"),
                    ("Chg -5%", f"cmd:{sn}:-"),
                ],
                [
                    ("⬅️ Back", "back"),
                    ("❌ Close", "close"),
                ],
            ]
            self._send(
                f"🔋 *{label}* — {soc:.0f}%",
                reply_markup=_kb(rows), edit_msg_id=msg_id,
            )
        elif "panel" in dtype:
            eps = data.get("eps")
            gc1 = self._gf(data, "backupCmdChCtrlInfos.0.ctrlSta")
            gc2 = self._gf(data, "backupCmdChCtrlInfos.1.ctrlSta")

            rows = [
                [
                    (f"{'🟢' if eps else '🔴'} EPS", f"cmd:{sn}:e"),
                    (f"{'🟢' if gc1 else '🔴'} Grid Chg B1", f"cmd:{sn}:g"),
                    (f"{'🟢' if gc2 else '🔴'} Grid Chg B2", f"cmd:{sn}:h"),
                ],
                [
                    ("⚡ Power Save", f"cmd:{sn}:p"),
                ],
                [
                    ("📋 Circuits", f"cmd:{sn}:circuits"),
                    ("⬅️ Back", "back"),
                    ("❌ Close", "close"),
                ],
            ]
            combined = self._gf(data, "backupBatPer")
            self._send(
                f"🏠 *{label}* — {combined:.0f}%",
                reply_markup=_kb(rows), edit_msg_id=msg_id,
            )

    def _cb_execute_command(self, sn: str, key: str, msg_id: int) -> None:
        if key == "circuits":
            self._cb_show_circuits(sn, msg_id)
            return
        if key == "noop":
            return

        # Import controller logic
        from .controls import DeviceController
        ctrl = DeviceController(self._mqtt, self._device_types)
        result = ctrl.handle_key(key, sn)
        # Refresh the device panel after command
        time.sleep(1)  # brief wait for MQTT update
        self._cb_device_control(sn, msg_id)

    def _cb_show_circuits(self, sn: str, msg_id: int) -> None:
        data = self._mqtt.get_device_data(sn)
        lines = ["📋 *Circuits*\n"]
        rows = []
        for i in range(12):
            w = self._gf(data, f"infoList.{i}.chWatt")
            name = self._circuit_names[i] if i < len(self._circuit_names) and self._circuit_names[i] else ""
            mode = self._gf(data, f"loadCmdChCtrlInfos.{i}.ctrlMode")
            status = "❌" if mode == 1 else "✅"

            if i >= 10:
                # Delta Pro circuits
                dp_sns = [s for s in self._device_types if "delta" in self._device_types[s]]
                dp_idx = i - 10
                if dp_idx < len(dp_sns):
                    name = f"DP.{dp_sns[dp_idx][-4:]}"

            label = f"{name}" if name else f"#{i+1}"
            lines.append(f"{status} {i+1}. {label}: *{w:.0f}W*")

        # Toggle buttons in 2 columns
        for r in range(0, 10, 2):
            row = []
            for j in range(2):
                idx = r + j
                if idx < 10:
                    n = self._circuit_names[idx] if idx < len(self._circuit_names) and self._circuit_names[idx] else f"#{idx+1}"
                    row.append((f"Toggle {n[:8]}", f"ct:{sn}:{idx}"))
            rows.append(row)

        rows.append([("⬅️ Back", f"dev:{sn}"), ("❌ Close", "close")])

        self._send("\n".join(lines), reply_markup=_kb(rows), edit_msg_id=msg_id)

    def _cb_circuit_toggle(self, sn: str, ch: int, msg_id: int) -> None:
        data = self._mqtt.get_device_data(sn)
        mode = self._gf(data, f"loadCmdChCtrlInfos.{ch}.ctrlMode")
        if mode == 1:
            self._mqtt.send_command(sn, {"cmdSet": 11, "id": 16, "ch": ch, "ctrlMode": 0, "sta": 0})
        else:
            self._mqtt.send_command(sn, {"cmdSet": 11, "id": 16, "ch": ch, "ctrlMode": 1, "sta": 1})
        time.sleep(1)
        self._cb_show_circuits(sn, msg_id)

    def _cmd_circuits(self) -> None:
        shp_sn = next((sn for sn, dt in self._device_types.items() if "panel" in dt), None)
        if not shp_sn:
            self._send("No Smart Home Panel found")
            return
        data = self._mqtt.get_device_data(shp_sn)
        lines = ["📋 *Circuit Loads*\n"]
        total = 0.0
        for i in range(12):
            w = self._gf(data, f"infoList.{i}.chWatt")
            total += w
            name = self._circuit_names[i] if i < len(self._circuit_names) and self._circuit_names[i] else ""
            mode = self._gf(data, f"loadCmdChCtrlInfos.{i}.ctrlMode")
            status = "❌" if mode == 1 else ""

            if i >= 10:
                dp_sns = [s for s in self._device_types if "delta" in self._device_types[s]]
                dp_idx = i - 10
                if dp_idx < len(dp_sns):
                    name = f"DP.{dp_sns[dp_idx][-4:]}"

            label = f"{name}" if name else f"#{i+1}"
            lines.append(f"  {i+1:2d}. {label:12s} *{w:6.0f}W* {status}")
        lines.append(f"\n⚡ Total: *{total:.0f}W*")
        self._send("\n".join(lines))

    def _cmd_solar(self) -> None:
        lines = []
        for sn, dtype in self._device_types.items():
            if "delta" not in dtype:
                continue
            data = self._mqtt.get_device_data(sn)
            label = self._label(sn)
            solar_w = self._gf(data, "mppt.inWatts") / 10
            solar_v = self._gf(data, "mppt.inVol") / 10
            solar_a = self._gf(data, "mppt.inAmp") / 100
            lifetime = self._gf(data, "pd.chgSunPower")
            mppt_hrs = self._gf(data, "pd.mpptUsedTime") / 3600

            if solar_w > 1:
                lines.append(
                    f"☀️ *{label}*\n"
                    f"  Power: *{solar_w:.0f}W* ({solar_v:.1f}V × {solar_a:.2f}A)\n"
                    f"  Lifetime: {lifetime/1000:.1f} kWh ({mppt_hrs:.0f}h)"
                )
            else:
                lines.append(f"🌙 *{label}*: No solar input\n  Lifetime: {lifetime/1000:.1f} kWh")

            if self._energy_rate and lifetime:
                saved = lifetime / 1000 * self._energy_rate
                lines[-1] += f"\n  💰 Saved: {self._currency}{saved:.2f}"

        self._send("☀️ *Solar*\n\n" + "\n\n".join(lines) if lines else "No Delta Pro devices found")

    def _cmd_cost(self) -> None:
        shp_sn = next((sn for sn, dt in self._device_types.items() if "panel" in dt), None)
        if not shp_sn or not self._energy_rate:
            self._send("Cost tracking not configured. Set ENERGY\\_RATE in .env")
            return

        data = self._mqtt.get_device_data(shp_sn)
        grid_today_wh = self._gf(data, "gridDayWatth", "heartbeat.gridDayWatth")
        grid_today_kwh = grid_today_wh / 1000

        # Current rate
        hour = datetime.now().hour
        if self._energy_rate_night and not (self._energy_day_start <= hour < self._energy_day_end):
            rate = self._energy_rate_night
            rate_label = "night"
        else:
            rate = self._energy_rate
            rate_label = "day" if self._energy_rate_night else "flat"

        cost_today = grid_today_kwh * rate  # simplified, should use TOU split

        # Monthly from SQLite
        monthly_str = ""
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            month_start = datetime.now().strftime("%Y-%m-01")
            cur = conn.execute(
                "SELECT SUM(value) FROM snapshots WHERE sn=? AND key='gridDayWatth' "
                "AND timestamp >= ? ORDER BY timestamp DESC",
                (shp_sn, month_start),
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                # gridDayWatth resets daily, so take max per day
                conn = sqlite3.connect(self._db_path)
                cur = conn.execute(
                    "SELECT DATE(timestamp), MAX(value) FROM snapshots "
                    "WHERE sn=? AND key='gridDayWatth' AND timestamp >= ? "
                    "GROUP BY DATE(timestamp)",
                    (shp_sn, month_start),
                )
                daily_maxes = [r[1] for r in cur.fetchall()]
                conn.close()
                month_kwh = sum(daily_maxes) / 1000
                month_cost = month_kwh * self._energy_rate  # simplified
                monthly_str = f"\n📅 This month: *{month_kwh:.1f} kWh* = *{self._currency}{month_cost:.2f}*"
        except Exception:
            pass

        self._send(
            f"💰 *Energy Costs*\n\n"
            f"Current rate: {self._currency}{rate} /kWh ({rate_label})\n"
            f"📆 Today: *{grid_today_kwh:.1f} kWh* = *{self._currency}{cost_today:.2f}*"
            f"{monthly_str}"
        )

    def _cmd_blade(self) -> None:
        """Show mower runs history (today + 7d + lifetime + last 5 sessions)."""
        blades = [(sn, dt) for sn, dt in self._device_types.items() if "blade" in dt]
        if not blades:
            self._send("No Blade mower configured.")
            return

        import sqlite3
        from datetime import timedelta
        out = []
        for sn, _ in blades:
            label = self._label(sn)
            data = self._mqtt.get_device_data(sn)
            BLADE_STATES = {
                0x500: "Idle", 0x501: "Standby", 0x502: "Mowing",
                0x503: "Returning", 0x504: "Charging", 0x505: "Mapping",
                0x506: "Paused", 0x507: "Error", 0x801: "Charging",
            }
            battery = self._gf(data, "normalBleHeartBeat.batteryRemainPercent")
            state_code = int(self._gf(data, "normalBleHeartBeat.robotState"))
            state = BLADE_STATES.get(state_code, f"0x{state_code:X}")
            out.append(f"🤖 *{label}*\n  Now: *{battery:.0f}%* {state}")

            try:
                today = datetime.now().strftime("%Y-%m-%d")
                week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                with sqlite3.connect(self._db_path) as conn:
                    today_row = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(area_m2),0), COALESCE(SUM(duration_sec),0), "
                        "COALESCE(SUM(battery_used),0) FROM mower_runs "
                        "WHERE device_sn=? AND start_time >= ? AND end_time IS NOT NULL",
                        (sn, today),
                    ).fetchone()
                    week_row = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(area_m2),0), COALESCE(SUM(duration_sec),0) "
                        "FROM mower_runs WHERE device_sn=? AND start_time >= ? AND end_time IS NOT NULL",
                        (sn, week_ago),
                    ).fetchone()
                    life_row = conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(area_m2),0), COALESCE(SUM(duration_sec),0) "
                        "FROM mower_runs WHERE device_sn=? AND end_time IS NOT NULL",
                        (sn,),
                    ).fetchone()
                    last_runs = conn.execute(
                        "SELECT start_time, duration_sec, area_m2, battery_used, end_state, error_count "
                        "FROM mower_runs WHERE device_sn=? AND end_time IS NOT NULL "
                        "ORDER BY start_time DESC LIMIT 5",
                        (sn,),
                    ).fetchall()

                if today_row and today_row[0]:
                    out.append(f"  📅 Today: *{today_row[0]}* run(s), {today_row[1]:.0f} m², "
                               f"{today_row[2]//60} min, {today_row[3]:.0f}% used")
                else:
                    out.append("  📅 Today: no runs")
                if week_row and week_row[0]:
                    out.append(f"  🗓 7 days: {week_row[0]} run(s), {week_row[1]:.0f} m², {week_row[2]//60} min")
                if life_row and life_row[0]:
                    out.append(f"  ♾ Lifetime: {life_row[0]} run(s), {life_row[1]:.0f} m², {life_row[2]//3600}h")

                # Battery efficiency over last 30 days
                eff_row = conn.execute(
                    "SELECT SUM(area_m2)/NULLIF(SUM(battery_used),0), AVG(area_m2/NULLIF(duration_sec,0))*60 "
                    "FROM mower_runs WHERE device_sn=? AND end_time IS NOT NULL "
                    "AND battery_used > 0 AND start_time >= ?",
                    (sn, (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")),
                ).fetchone()
                if eff_row and eff_row[0]:
                    eff_m2_per_pct = eff_row[0]
                    speed_m2_per_min = eff_row[1] or 0
                    out.append(f"  ⚡ Efficiency (30d): {eff_m2_per_pct:.1f} m² / % battery"
                               + (f", {speed_m2_per_min:.1f} m²/min" if speed_m2_per_min else ""))

                if last_runs:
                    out.append("\n*Recent runs:*")
                    for st, dur, area, batt, end_st, errs in last_runs:
                        # Compact "MM-DD HH:MM" timestamp
                        try:
                            t = datetime.fromisoformat(st).strftime("%m-%d %H:%M")
                        except Exception:
                            t = st
                        end_emoji = {0x504: "🔌", 0x801: "🔌", 0x507: "⚠️"}.get(int(end_st or 0), "🏠")
                        err_str = f" ⚠️{errs}" if errs else ""
                        out.append(f"  {t}  {dur//60:>3}min  {area:>4.0f}m²  -{batt:.0f}% {end_emoji}{err_str}")
            except Exception as e:
                log.warning("blade history query failed: %s", e)

        self._send("\n".join(out))

    def _cmd_blade_debug(self) -> None:
        """Dump every key currently held for the Blade — for field discovery."""
        blades = [(sn, dt) for sn, dt in self._device_types.items() if "blade" in dt]
        if not blades:
            self._send("No Blade configured.")
            return
        for sn, _ in blades:
            data = self._mqtt.get_device_data(sn)
            if not data:
                self._send(f"`{sn}`: no data yet")
                continue
            keys = sorted(data.keys())
            # Send in chunks to fit Telegram's 4 KB message cap.
            chunk: list[str] = []
            chunks_sent = 0
            for k in keys:
                v = data[k]
                line = f"`{k}` = `{v}`"
                if sum(len(x) for x in chunk) + len(line) > 3500:
                    self._send(f"*Blade `{sn}` ({len(keys)} keys, part {chunks_sent+1})*\n" + "\n".join(chunk))
                    chunk = []
                    chunks_sent += 1
                chunk.append(line)
            if chunk:
                self._send(f"*Blade `{sn}` ({len(keys)} keys, part {chunks_sent+1})*\n" + "\n".join(chunk))

    def _cmd_forecast(self) -> None:
        if not self._solar_forecast:
            self._send("Solar forecast not configured.\nSet SOLAR\\_LATITUDE and SOLAR\\_LONGITUDE in .env")
            return
        today = self._solar_forecast.get_forecast()
        tomorrow = self._solar_forecast.get_tomorrow()
        lines = ["🔮 *Solar Forecast*\n"]

        for label, fc in [("Today", today), ("Tomorrow", tomorrow)]:
            if not fc:
                lines.append(f"*{label}*: No data")
                continue
            lines.append(
                f"*{label}*\n"
                f"  ☀️ Expected: *{fc['total_kwh']} kWh*\n"
                f"  ⚡ Peak: {fc['peak_watts']}W\n"
                f"  ☁️ Cloud: {fc['avg_cloud']}%\n"
                f"  🌅 {fc['sunrise']} → 🌇 {fc['sunset']}"
            )
            if self._energy_rate and fc['total_kwh'] > 0:
                saved = fc['total_kwh'] * self._energy_rate
                lines.append(f"  💰 Est. savings: {self._currency}{saved:.2f}")
            lines.append("")

        rec = self._solar_forecast.get_recommendation()
        if rec:
            lines.append(f"\n{rec}")

        self._send("\n".join(lines))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _label(self, sn: str) -> str:
        dtype = self._device_types.get(sn, "")
        short = sn[-4:] if len(sn) > 4 else sn
        if "delta" in dtype:
            return f"Delta Pro ({short})"
        if "panel" in dtype:
            return f"Smart Panel ({short})"
        if "blade" in dtype:
            return f"Blade ({short})"
        return sn

    def _gf(self, data: dict, *keys: str) -> float:
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _fmt_time(self, minutes: float) -> str:
        m = int(minutes)
        if m <= 0:
            return "--"
        if m >= 1440:
            return f"{m // 1440}d {(m % 1440) // 60}h"
        return f"{m // 60}h {m % 60}m"

    def _send(self, text: str, reply_markup: dict | None = None, edit_msg_id: int | None = None) -> None:
        try:
            payload: dict = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            if edit_msg_id:
                payload["message_id"] = edit_msg_id
                self._session.post(f"{self._base}/editMessageText", json=payload, timeout=10)
            else:
                self._session.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            log.warning("Bot send error: %s", e)

    def _delete_msg(self, msg_id: int) -> None:
        try:
            self._session.post(f"{self._base}/deleteMessage",
                          json={"chat_id": self._chat_id, "message_id": msg_id}, timeout=5)
        except Exception:
            pass
