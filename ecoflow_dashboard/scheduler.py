from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)


class ChargeScheduler:
    """Timer-based charging automation.

    Supports:
    - Time-of-use: charge from grid only during cheap hours
    - SOC limiter: pause charging when SOC reaches target
    """

    def __init__(
        self,
        mqtt_client: EcoFlowMqttClient,
        device_types: dict[str, str],
        alerter: object | None = None,
    ) -> None:
        self._mqtt = mqtt_client
        self._device_types = device_types
        self._alerter = alerter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Time-of-use charging (SHP grid charge)
        self._charge_start = os.environ.get("SCHEDULE_CHARGE_START", "")  # "23:00"
        self._charge_stop = os.environ.get("SCHEDULE_CHARGE_STOP", "")   # "06:00"
        self._charge_active = False

        # SOC limiter (Delta Pro) — separate limits for grid vs solar
        self._max_soc_grid = int(os.environ.get("SCHEDULE_MAX_SOC_GRID",
                                 os.environ.get("SCHEDULE_MAX_SOC", "0")))  # grid limit
        self._max_soc_solar = int(os.environ.get("SCHEDULE_MAX_SOC_SOLAR", "100"))  # solar limit
        self._charge_paused: dict[str, bool] = {}  # sn → is_paused
        self._default_charge_watts = int(os.environ.get("SCHEDULE_CHARGE_WATTS", "2500"))

    @property
    def enabled(self) -> bool:
        return bool(self._charge_start or self._max_soc_grid)

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        rules = []
        if self._charge_start:
            rules.append(f"Grid charge {self._charge_start}-{self._charge_stop}")
        if self._max_soc_grid:
            rules.append(f"Grid SOC limit {self._max_soc_grid}%")
        if self._max_soc_solar < 100:
            rules.append(f"Solar SOC limit {self._max_soc_solar}%")
        else:
            rules.append(f"Solar: charge to 100%")
        log.info("Scheduler started: %s", ", ".join(rules))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self._stop.wait(20)  # Wait for MQTT data
        while not self._stop.is_set():
            try:
                if self._charge_start:
                    self._check_time_of_use()
                if self._max_soc_grid:
                    self._check_soc_limit()
            except Exception:
                log.exception("Scheduler check failed")
            self._stop.wait(60)

    def _notify(self, msg: str) -> None:
        log.info("Scheduler: %s", msg)
        if self._alerter and hasattr(self._alerter, "_send"):
            self._alerter._send(f"⏰ *Scheduler*\n{msg}")

    def _is_in_window(self, start_str: str, stop_str: str) -> bool:
        """Check if current time is within start-stop window (handles overnight)."""
        now = datetime.now()
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, stop_str.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        now_min = now.hour * 60 + now.minute

        if start_min <= end_min:
            return start_min <= now_min < end_min
        else:  # overnight: e.g. 23:00 - 06:00
            return now_min >= start_min or now_min < end_min

    def _check_time_of_use(self) -> None:
        """Enable/disable SHP grid charging based on time window."""
        in_window = self._is_in_window(self._charge_start, self._charge_stop)

        if in_window and not self._charge_active:
            # Enable grid charging
            self._charge_active = True
            for sn, dtype in self._device_types.items():
                if "panel" in dtype:
                    for ch in [10, 11]:  # Battery 1 and 2
                        self._mqtt.send_command(sn, {
                            "cmdSet": 11, "id": 17, "ch": ch, "sta": 2, "ctrlMode": 1,
                        })
            self._notify(f"Grid charging ENABLED ({self._charge_start}-{self._charge_stop})")

        elif not in_window and self._charge_active:
            # Disable grid charging
            self._charge_active = False
            for sn, dtype in self._device_types.items():
                if "panel" in dtype:
                    for ch in [10, 11]:
                        self._mqtt.send_command(sn, {
                            "cmdSet": 11, "id": 17, "ch": ch, "sta": 0, "ctrlMode": 0,
                        })
            self._notify(f"Grid charging DISABLED (outside {self._charge_start}-{self._charge_stop})")

    def _check_soc_limit(self) -> None:
        """Pause/resume charging based on source-aware SOC limits.

        Grid charging: limited to SCHEDULE_MAX_SOC_GRID (default 80%)
        Solar charging: limited to SCHEDULE_MAX_SOC_SOLAR (default 100%)
        """
        for sn, dtype in self._device_types.items():
            if "delta" not in dtype:
                continue
            data = self._mqtt.get_device_data(sn)
            if not data:
                continue

            soc = 0
            for key in ["ems.lcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc"]:
                v = data.get(key)
                if v is not None:
                    try:
                        soc = float(v)
                        if soc > 0:
                            break
                    except (TypeError, ValueError):
                        continue

            # Detect charging source
            solar_watts = float(data.get("mppt.inWatts", 0) or 0)
            ac_in_watts = float(data.get("inv.inputWatts", 0) or 0)
            is_solar = solar_watts > 10
            is_grid = ac_in_watts > 10

            # Choose limit based on source
            if is_solar and not is_grid:
                active_limit = self._max_soc_solar
                source = "solar"
            elif is_grid:
                active_limit = self._max_soc_grid
                source = "grid"
            else:
                active_limit = self._max_soc_grid  # default to grid limit
                source = "idle"

            short_sn = sn[-6:]
            is_paused = self._charge_paused.get(sn, False)

            if soc >= active_limit and not is_paused and (is_solar or is_grid):
                # Pause charging — reached limit for current source
                self._mqtt.send_command(sn, {"id": 69, "slowChgPower": 0})
                self._charge_paused[sn] = True
                self._notify(
                    f"Charging PAUSED on DP {short_sn}\n"
                    f"SOC {soc:.0f}% reached {source} limit ({active_limit}%)"
                )

            elif is_paused:
                # Check if we should resume:
                # 1. Source changed (was grid-limited, now solar available → higher limit)
                # 2. SOC dropped below limit - hysteresis
                should_resume = False
                reason = ""

                if is_solar and soc < self._max_soc_solar:
                    # Solar available and below solar limit → resume
                    should_resume = True
                    reason = f"Solar active, SOC {soc:.0f}% below solar limit ({self._max_soc_solar}%)"
                elif soc < active_limit - 5:
                    # Below current limit with hysteresis → resume
                    should_resume = True
                    reason = f"SOC {soc:.0f}% below {source} limit ({active_limit - 5}%)"

                if should_resume:
                    self._mqtt.send_command(sn, {"id": 69, "slowChgPower": self._default_charge_watts})
                    self._charge_paused[sn] = False
                    self._notify(f"Charging RESUMED on DP {short_sn}\n{reason}")
