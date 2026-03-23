from __future__ import annotations

import logging
import platform
import queue
import threading
from dataclasses import dataclass

from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command definitions
# ---------------------------------------------------------------------------

@dataclass
class Command:
    label: str          # Human-readable name shown in help bar
    read_key: str       # MQTT data key to read current value
    param_id: int       # "id" field in the params payload
    field: str          # Field name in params (e.g. "enabled", "maxChgSoc")
    action: str         # "toggle", "+5", "-5", "+100", "-100", "cycle:0,1,2,3"
    min_val: int = 0
    max_val: int = 100
    cmd_set: int | None = None  # SHP uses cmdSet
    inverted: bool = False      # True if 0=ON, 1=OFF (e.g. beep)
    cycle_labels: dict[int, str] | None = None  # Labels for cycle values


# Delta Pro commands
DELTA_PRO_COMMANDS: dict[str, Command] = {
    "a": Command("AC Output",      "inv.cfgAcEnabled",   66, "enabled",      "toggle"),
    "d": Command("DC Output",      "mppt.carState",      81, "enabled",      "toggle"),
    "x": Command("X-Boost",        "inv.cfgAcXboost",    66, "xboost",       "toggle"),
    "b": Command("Beep",           "pd.beepState",       38, "enabled",      "toggle", inverted=True),
    "=": Command("Chg Limit +5%",  "ems.maxChargeSoc",   49, "maxChgSoc",    "+5",  50, 100),
    "-": Command("Chg Limit -5%",  "ems.maxChargeSoc",   49, "maxChgSoc",    "-5",  50, 100),
    "]": Command("Dsg Limit +5%",  "ems.minDsgSoc",      51, "minDsgSoc",    "+5",  0, 30),
    "[": Command("Dsg Limit -5%",  "ems.minDsgSoc",      51, "minDsgSoc",    "-5",  0, 30),
    "w": Command("AC Power +100W", "inv.cfgSlowChgWatts", 69, "slowChgPower", "+100", 200, 2900),
    "s": Command("AC Power -100W", "inv.cfgSlowChgWatts", 69, "slowChgPower", "-100", 200, 2900),
    "c": Command("AC Charging",    "inv.cfgSlowChgWatts", 69, "slowChgPower", "charge_toggle"),
}

# Smart Home Panel commands
SHP_COMMANDS: dict[str, Command] = {
    "e": Command("EPS Mode",       "eps",                24, "eps",          "toggle", cmd_set=11),
    "g": Command("Grid Charge B1", "energyInfos.0.stateBean.isGridCharge", 17, "",     "shp_grid_charge_1", cmd_set=11),
    "h": Command("Grid Charge B2", "energyInfos.1.stateBean.isGridCharge", 17, "",     "shp_grid_charge_2", cmd_set=11),
}

# Default AC charge power to restore when un-pausing
DEFAULT_AC_CHARGE_WATTS = 2500

# Friendly key labels for help bar
KEY_LABELS = {
    "=": "+",
    "-": "-",
    "[": "[",
    "]": "]",
}


# ---------------------------------------------------------------------------
# Device controller
# ---------------------------------------------------------------------------

class DeviceController:
    def __init__(self, mqtt_client: EcoFlowMqttClient, device_types: dict[str, str]) -> None:
        self._mqtt = mqtt_client
        self._device_types = device_types
        self._saved_charge_watts: dict[str, int] = {}  # Remember charge watts before pause

    def get_commands(self, sn: str) -> dict[str, Command]:
        dtype = self._device_types.get(sn, "")
        if "delta" in dtype:
            return DELTA_PRO_COMMANDS
        if "panel" in dtype:
            return SHP_COMMANDS
        return {}

    def handle_key(self, key: str, sn: str) -> str:
        """Process a keypress. Returns status message or empty string."""
        commands = self.get_commands(sn)
        cmd = commands.get(key)
        if not cmd:
            return ""

        data = self._mqtt.get_device_data(sn)
        current = data.get(cmd.read_key, 0)
        try:
            current = int(float(current))
        except (TypeError, ValueError):
            current = 0

        # Compute new value and build params
        params: dict | None = None

        if cmd.action == "toggle":
            new_val = 0 if current else 1
            if cmd.inverted:
                val_txt = "ON" if new_val == 0 else "OFF"
            else:
                val_txt = "ON" if new_val else "OFF"
            params = {"id": cmd.param_id, cmd.field: new_val}

        elif cmd.action == "cycle" and cmd.cycle_labels:
            values = sorted(cmd.cycle_labels.keys())
            try:
                idx = values.index(current)
                new_val = values[(idx + 1) % len(values)]
            except ValueError:
                new_val = values[0]
            val_txt = cmd.cycle_labels.get(new_val, str(new_val))
            params = {"id": cmd.param_id, cmd.field: new_val}

        elif cmd.action == "charge_toggle":
            # Delta Pro: set AC charge watts to 0 (pause) or restore (resume)
            if current > 0:
                self._saved_charge_watts[sn] = current
                new_val = 0
                val_txt = "PAUSED"
            else:
                new_val = self._saved_charge_watts.get(sn, DEFAULT_AC_CHARGE_WATTS)
                val_txt = f"ON ({new_val}W)"
            params = {"id": cmd.param_id, cmd.field: new_val}

        elif cmd.action.startswith("shp_grid_charge"):
            # SHP battery grid charge: cmdSet=11, id=17
            batt_idx = int(cmd.action[-1])  # 1 or 2
            ch = 9 + batt_idx  # ch=10 for batt1, ch=11 for batt2
            if current:
                params = {"cmdSet": 11, "id": 17, "ch": ch, "sta": 0, "ctrlMode": 0}
                val_txt = "OFF"
            else:
                params = {"cmdSet": 11, "id": 17, "ch": ch, "sta": 2, "ctrlMode": 1}
                val_txt = "ON"

        elif cmd.action.startswith("+"):
            step = int(cmd.action[1:])
            new_val = min(current + step, cmd.max_val)
            val_txt = str(new_val)
            params = {"id": cmd.param_id, cmd.field: new_val}

        elif cmd.action.startswith("-"):
            step = int(cmd.action[1:])
            new_val = max(current - step, cmd.min_val)
            val_txt = str(new_val)
            params = {"id": cmd.param_id, cmd.field: new_val}

        else:
            return ""

        if params is None:
            return ""

        if cmd.cmd_set is not None and "cmdSet" not in params:
            params["cmdSet"] = cmd.cmd_set

        self._mqtt.send_command(sn, params)
        log.info("Command: %s → %s on %s", cmd.label, val_txt, sn)
        return f"{cmd.label} → {val_txt}"


# ---------------------------------------------------------------------------
# Cross-platform keyboard input thread
# ---------------------------------------------------------------------------

class KeyboardThread(threading.Thread):
    """Background thread that reads single keypresses into a queue."""

    def __init__(self, key_queue: queue.Queue) -> None:
        super().__init__(daemon=True)
        self._queue = key_queue
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if platform.system() == "Windows":
            self._run_windows()
        else:
            self._run_unix()

    def _run_windows(self) -> None:
        import msvcrt
        while not self._stop_event.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                try:
                    key = ch.decode("utf-8", errors="ignore")
                except Exception:
                    key = ""
                if key:
                    self._queue.put(key)
            else:
                self._stop_event.wait(0.05)

    def _run_unix(self) -> None:
        import select
        import sys
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop_event.is_set():
                rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                if rlist:
                    key = sys.stdin.read(1)
                    if key:
                        self._queue.put(key)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
