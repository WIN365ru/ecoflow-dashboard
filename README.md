<div align="center">

# EcoFlow Dashboard

**Real-time CLI dashboard for EcoFlow power stations & Smart Home Panel**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![EcoFlow](https://img.shields.io/badge/EcoFlow-Cloud_API-orange.svg)](https://www.ecoflow.com/)

Monitor battery status, power consumption, and control your EcoFlow devices from the terminal.

</div>

---

## Preview

```
EcoFlow Dashboard  MQTT Connected  2026-03-23 22:12:16

 Delta Pro (DCEBZ8ZEBXXXXXX) 79%                Delta Pro (DCEBZ8ZEBXXXXXX) 47%
+-----------------------------------------------+-----------------------------------------------+
| ################################------------- | ##################--------------------------- |
|                                               |                                               |
| Solar In          0 W  AC In            0 W   | Solar In          0 W  AC In            0 W   |
| AC Out      75 W (230V 50Hz)  12V/Car   0 W   | AC Out            0 W  12V/Car         14 W   |
| Total In          0 W  Total Out  75 W (61%)  | Total In          0 W  Total Out        0 W   |
|                                               |                                               |
| Discharge     1d 0h   Voltage        49.7 V   | Idle Time           --  Voltage        49.6 V  |
| Current       -2.1 A  DC Bus         123 W    | Current         -0.1 A  DC Bus          14 W   |
| Cell V   3.27-3.32V  D55mV   Cell T  26-28C  | Cell V   3.29-3.30V  D6mV    Cell T  25-26C  |
| Batt/Inv    27/78 C   MPPT/MOS    66/27 C    | Batt/Inv    26/29 C   MPPT/MOS    31/27 C    |
| Cycles           569  Limits      10% - 100%  | Cycles           655  Limits      10% - 100%  |
| Fan    Inv ON (Auto)  Beep               OFF  | Fan       Off (Auto)  Beep               OFF  |
|                                               |                                               |
| Lifetime Energy                               | Lifetime Energy                               |
| AC Charged   1918 kWh  Solar Charged  925 kWh | AC Charged   2589 kWh  Solar Charged  426 kWh |
| AC Dischg    2189 kWh  DC Discharged   13 kWh | AC Dischg    2254 kWh  DC Discharged    6 kWh |
|  Health: 94% (Excellent)  2.9 / 3.7 kWh      |  Health: 93% (Excellent)  1.7 / 3.7 kWh      |
+-----------------------------------------------+-----------------------------------------------+

+------------------------------- Smart Home Panel (SP10ZEW5ZEAQ0143) ------+
| Grid       ON (230V 50Hz)  Grid Today  15 kWh   Backup Today      0 Wh  |
| Combined     47%  3.80 kWh  Limits     0%-100%   Sched Chg        OFF   |
|                                                                          |
| Batt 1: 47% 26C  Standby  3.80 kWh / 3.6 kW  Chg:4d 20h               |
| Batt 2: Not connected                                                    |
|                                                                          |
|  #  Name      Power    A   M  P  |  #  Name      Power    A   M  P     |
|  1             68 W   --  A  -  |  7             15 W   --  A  6       |
|  2            630 W   --  A  1  |  8             36 W   --  A  7       |
|  3              0 W   --  A  2  |  9             48 W   --  A  8       |
|  4              0 W   --  A  3  | 10              0 W   --  A  9       |
|  5              0 W   --  A  4  | 11              0 W   --  A  -       |
|  6             10 W   --  A  5  | 12              0 W   --  A  -       |
|                                              809 W | Up: 19h 24m         |
+--------------------------------------------------------------------------+
  [a] AC  [d] DC  [x] XBoost  [b] Beep  [c] Charging  [+/-] Chg%  [w/s] ChgW  [1-3] Device
```

## Features

### Monitoring

- **Battery** -- SOC%, health (SOH), remaining/full capacity, charge cycles
- **Power flow** -- solar, AC, DC input/output with voltage, current, frequency
- **Efficiency** -- DC bus power vs total output percentage
- **Cell-level** -- min/max cell voltage with delta (mV), temperature range
- **Temperatures** -- battery, inverter, MPPT, MOS with fan status
- **Lifetime energy** -- total AC/solar charged, AC/DC discharged (kWh)
- **Smart Home Panel** -- grid status (V/Hz), 12-circuit power/current/priority, per-battery detail
- **Firmware** -- PD, BMS, MPPT, inverter versions
- **Data logging** -- automatic SQLite snapshots for historical analysis

### Controls (keyboard shortcuts)

**Delta Pro:**

| Key | Action | Range |
|-----|--------|-------|
| `a` | Toggle AC output | On / Off |
| `d` | Toggle DC output | On / Off |
| `x` | Toggle X-Boost | On / Off |
| `b` | Toggle beeper | On / Off |
| `c` | Pause / resume AC charging | 0 -- 2900 W |
| `+` / `-` | Adjust max charge limit | 50 -- 100% |
| `[` / `]` | Adjust min discharge limit | 0 -- 30% |
| `w` / `s` | Adjust AC charge power | 200 -- 2900 W |

**Smart Home Panel:**

| Key | Action |
|-----|--------|
| `e` | Toggle EPS mode |
| `g` | Toggle grid charge (Battery 1) |
| `h` | Toggle grid charge (Battery 2) |

**Navigation:**

| Key | Action |
|-----|--------|
| `1` -- `3` | Select device |
| `Ctrl+C` | Exit |

## Supported Devices

| Device | Monitoring | Controls |
|--------|-----------|----------|
| **Delta Pro** | Full | AC / DC / XBoost / Beep / Charging |
| **Smart Home Panel** | Full | EPS / Grid Charge |
| Other EcoFlow devices | Partial (auto-detect) | -- |

## Installation

```bash
git clone https://github.com/WIN365ru/ecoflow-dashboard.git
cd ecoflow-dashboard
pip install -e .
```

## Configuration

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

### Option 1: Private API (works immediately)

Use your EcoFlow app login. Find serial numbers in the app under **Device Settings**.

```env
ECOFLOW_EMAIL=your_email@example.com
ECOFLOW_PASSWORD=your_password
ECOFLOW_DEVICE_SNS=DELTA_SN_1,DELTA_SN_2,PANEL_SN
```

### Option 2: Public API (requires developer portal approval)

Register at the [EcoFlow Developer Portal](https://developer.ecoflow.com/). Devices are auto-discovered.

```env
ECOFLOW_ACCESS_KEY=your_access_key
ECOFLOW_SECRET_KEY=your_secret_key
```

## Usage

```bash
# Start the dashboard
python -m ecoflow_dashboard

# Debug mode (raw MQTT messages to stderr)
python -m ecoflow_dashboard --debug

# Dump all available data keys and exit
python -m ecoflow_dashboard --dump

# Adjust logging interval (default: 300s)
python -m ecoflow_dashboard --log-interval 60

# Disable SQLite logging
python -m ecoflow_dashboard --no-log

# Custom database path
python -m ecoflow_dashboard --db /path/to/history.db
```

## Architecture

```
                  HTTPS (auth)              MQTT (TLS, real-time)
  EcoFlow App -----> EcoFlow Cloud <-----> ecoflow-dashboard
                         |                       |
                    REST API:               paho-mqtt:
                    - /auth/login           - subscribe device topics
                    - /certification        - publish control commands
                    - /device/list          - auto-reconnect
                         |                       |
                         v                       v
                    MQTT Broker            Rich Live Terminal
                    (port 8883)            (0.5s refresh)
                                                 |
                                           SQLite Logger
                                           (periodic snapshots)
```

## Project Structure

```
ecoflow-dashboard/
  .env.example           # Configuration template
  pyproject.toml         # Dependencies and packaging
  LICENSE                # MIT License
  ecoflow_dashboard/
    __main__.py          # CLI entry point and startup sequence
    config.py            # Environment loading, auth mode detection
    api.py               # REST API -- auth, device list, MQTT credentials
    mqtt_client.py       # MQTT connection, subscriptions, command publishing
    dashboard.py         # Rich terminal UI with side-by-side panels
    controls.py          # Keyboard input thread and device command registry
    logger.py            # SQLite periodic data snapshots
```

## Data Points

<details>
<summary><b>Delta Pro</b> -- 90+ fields</summary>

| Category | Fields |
|----------|--------|
| Battery | SOC, SOH, voltage, current, remaining/full/design capacity, cycles |
| Cells | min/max voltage, min/max temperature, delta |
| Power | AC in/out, solar in, DC out, USB, total in/out |
| AC Detail | input/output voltage, frequency |
| Solar Detail | voltage, current, MPPT temperature |
| Temperatures | battery, inverter (in/out), MPPT, MOS |
| Fan | EMS level, inverter state, mode |
| Charging | charge/discharge time, limits, AC charge power, pause flag |
| Lifetime | AC charged/discharged, solar charged, DC discharged (kWh) |
| Firmware | PD, BMS, MPPT, inverter versions |
| System | beep state, WiFi RSSI |

</details>

<details>
<summary><b>Smart Home Panel</b> -- 170+ fields</summary>

| Category | Fields |
|----------|--------|
| Grid | status, voltage, frequency, daily energy |
| Battery (per unit) | SOC, temperature, charge/discharge time, input/output power, capacity, rated power |
| Battery State | connected, grid charge, solar charge, AC open, outputting, enabled |
| Circuits (x12) | power (W), current (A), control mode, priority, name |
| System | combined SOC, total capacity, charge limits, EPS mode |
| Scheduled Charge | enabled, target level, power, battery selection |
| Diagnostics | self-check result, error codes, work time |
| Emergency | backup mode, overload mode |

</details>

## Troubleshooting

| Issue | Solution |
|-------|----------|
| All values show 0 | Wait 10-15 seconds for MQTT data to arrive |
| MQTT disconnects | Auto-reconnects with exponential backoff |
| Wrong device type | Set `ECOFLOW_DEVICE_SNS` explicitly |
| No circuit names | Name circuits in the EcoFlow app first |
| `--dump` shows no data | Ensure device serial numbers are correct |

## Acknowledgments

- [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) -- Home Assistant integration used as API reference
- [Mark-Hicks/ecoflow-api-examples](https://github.com/Mark-Hicks/ecoflow-api-examples) -- Public API signing examples

## License

[MIT](LICENSE) -- use it however you like.
