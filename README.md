<div align="center">

# EcoFlow Dashboard

**Real-time CLI + Web dashboard for EcoFlow power stations & Smart Home Panel**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](Dockerfile)
[![EcoFlow](https://img.shields.io/badge/EcoFlow-Cloud_API-orange.svg)](https://www.ecoflow.com/)

Monitor battery status, power consumption, and control your EcoFlow devices from the terminal or any browser.

</div>

---

## Preview

### CLI Dashboard

```
EcoFlow Dashboard  v0.5.1  MQTT Connected  2026-03-26 04:30:55

  Delta Pro (DCEBZ8ZEBXXXXXX)  71%         Delta Pro (DCEBZ8ZEBXXXXXX)  91%
+-----------------------------------------+-----------------------------------------+
| ########################--------------- | ################################------- |
|                                         |                                         |
| Solar In       0 W  AC In  1.3 kW (50Hz)| Solar In       0 W  AC In        0 W   |
| AC Out         0 W  12V/Car Out    0 W  | AC Out         0 W  12V/Car Out  0 W   |
| USB Out        1 W  Total Out  1 W (0%) | Total In       0 W  Total Out    0 W   |
| Total In   1.3 kW                       |                                         |
|                                         |                                         |
| Charge Time  0h 49m  Voltage    51.5 V  | Idle Time        --  Voltage    50.1 V  |
| Current     +24.0 A  DC Bus      54 W   | Current      -0.2 A  DC Bus      14 W   |
| Cell V  3.42-3.49V  D71mV  23-24C       | Cell V  3.32-3.35V  D24mV  24-25C       |
| Batt/Inv   23/48C  MPPT/MOS  66/27C     | Batt/Inv   25/32C  MPPT/MOS  30/26C     |
| Cycles       569  Limits   10% - 100%   | Cycles       655  Limits   10% - 100%   |
| Fan    Inv ON (Lvl1)  Beep          ON  | Fan      Off (Auto)  Beep          ON   |
|                                         |                                         |
| PD 1.2.0.156  MPPT 3.1.0.50            | PD 1.2.0.156  MPPT 3.1.0.50            |
| BMS 1.1.5.35  Inv 2.1.1.158            | BMS 1.1.5.35  Inv 2.1.1.158            |
|                                         |                                         |
| Lifetime Energy                         | Lifetime Energy                         |
| AC Chg  1918 kWh  Solar  925 kWh        | AC Chg  2590 kWh  Solar  426 kWh        |
| AC Dsg  2189 kWh  DC Dsg  13 kWh        | AC Dsg  2254 kWh  DC Dsg   6 kWh        |
|  Health: 94% (Excellent) 2.75/3.56 kWh  |  Health: 93% (Excellent) 1.38/3.72 kWh  |
+-----------------------------------------+-----------------------------------------+

+------------------------ Smart Home Panel (SP10ZEWXXXXXXXX) -----------------------+
| Grid          ON   Grid Today    2.82 kWh   Backup Today             0 Wh         |
| Combined  81%  6.71 kWh   Limits       --   Sched Chg              OFF            |
|                                                                                    |
| Batt 1: 91% 3.28 kWh 25C  Standby                         Chg:9d 8h              |
| Batt 2: 71% 2.56 kWh 23C  GridChg  +1.3 kW                Chg:0h 49m             |
|                                                                                    |
|                                  Circuits                                          |
|  #  Name      Power  Mode  Priority  |  #  Name      Power  Mode  Priority        |
|  1             70 W  Auto     -      |  7             15 W   Auto     6            |
|  2            700 W  Auto     1      |  8             40 W   Auto     7            |
|  3             35 W  Auto     2      |  9             45 W   Auto     8            |
|  4              0 W  Auto     3      | 10              0 W   Auto     9            |
|  5              0 W  Auto     4      | 11  DP.0352     0 W   Auto     -            |
|  6             10 W  Auto     5      | 12  DP.0801  1.4 kW   Auto     -            |
|                                      |                           98%               |
|                                             1.3 kW | Up: 3d 1h                    |
+------------------------------------------------------------------------------------+
  [a] AC  [d] DC  [x] XBoost  [b] Beep  [c] Charging  [+/-] Chg%  [w/s] ChgW
  [e] EPS  [g] Grid Chg B1  [h] Grid Chg B2  [1-3] Device  [Ctrl+C] Exit
```

### Web Dashboard

Access from any device on your LAN (iPad, iPhone, tablet):

```bash
python -m ecoflow_dashboard --web
# Open http://<your-ip>:5000 in any browser
```

- Dark theme, responsive layout (mobile + desktop)
- Real-time auto-refresh every 2 seconds
- Control buttons (AC, DC, EPS, Grid Charge toggles)
- Charts: live rolling (1h) + historical (up to 30 days from SQLite)

## Features

### Monitoring

- **Battery** -- SOC%, health (SOH), remaining energy (kWh), charge cycles
- **Power flow** -- solar, AC, DC input/output with voltage, current, frequency
- **Efficiency** -- system efficiency from battery power vs useful output, SHP charging efficiency
- **Cell-level** -- min/max cell voltage with delta (mV), temperature range
- **Temperatures** -- battery, inverter, MPPT, MOS with fan status
- **Lifetime energy** -- total AC/solar charged, AC/DC discharged (kWh)
- **Smart Home Panel** -- grid status, 12-circuit power with priority, per-battery detail with remaining energy
- **Firmware** -- PD, BMS, MPPT, inverter versions
- **Data logging** -- automatic SQLite snapshots for historical analysis
- **Web dashboard** -- accessible from any browser with live + historical charts

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

### Option A: pip (local)

```bash
git clone https://github.com/WIN365ru/ecoflow-dashboard.git
cd ecoflow-dashboard
pip install -e .
```

### Option B: Docker

```bash
git clone https://github.com/WIN365ru/ecoflow-dashboard.git
cd ecoflow-dashboard
cp .env.example .env   # fill in credentials
docker compose up -d
# Web dashboard at http://localhost:5000
```

### Option C: Portainer Stack

1. In Portainer: **Stacks** > **Add stack** > **Repository**
2. Repository URL: `https://github.com/WIN365ru/ecoflow-dashboard`
3. Add environment variables (see Configuration below)
4. Deploy -- dashboard at `http://<server-ip>:5000`

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
# CLI dashboard (default)
python -m ecoflow_dashboard

# Web dashboard (for iPad/mobile/remote access)
python -m ecoflow_dashboard --web
python -m ecoflow_dashboard --web --web-port 8080

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
                    - /device/list          - auto-reconnect (3x retry)
                         |                       |
                         v                       v
                    MQTT Broker          +------ Main Process ------+
                    (port 8883)          |                          |
                                         |  Rich CLI    Flask Web  |
                                         |  Dashboard   Dashboard  |
                                         |  (terminal)  (browser)  |
                                         |                          |
                                         +--- SQLite Logger -------+
                                              (periodic snapshots)
```

## Project Structure

```
ecoflow-dashboard/
  .env.example           # Configuration template
  pyproject.toml         # Dependencies and packaging
  Dockerfile             # Docker image build
  docker-compose.yml     # Docker Compose / Portainer stack
  LICENSE                # MIT License
  ecoflow_dashboard/
    __main__.py          # CLI entry point, --web flag routing
    config.py            # Environment loading, auth mode detection
    api.py               # REST API with retry logic (auth, MQTT creds)
    mqtt_client.py       # MQTT connection, subscriptions, commands
    dashboard.py         # Rich terminal UI with side-by-side panels
    controls.py          # Keyboard input and device command registry
    web.py               # Flask web dashboard with charts
    logger.py            # SQLite periodic data snapshots
    version_check.py     # GitHub release version checker
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
| Grid | status, daily energy |
| Battery (per unit) | SOC, remaining energy (kWh), temperature, charge/discharge time, input/output power |
| Battery State | connected, grid charge, solar charge, AC open, outputting, enabled |
| Circuits (x12) | power (W), control mode, priority, Delta Pro auto-labeling, charging efficiency |
| System | combined SOC, total capacity, charge limits, EPS mode |
| Scheduled Charge | enabled, target level, power, battery selection |
| Diagnostics | error codes (x20), uptime |
| Emergency | backup mode, overload mode |

</details>

## Troubleshooting

| Issue | Solution |
|-------|----------|
| All values show 0 | Wait 10-15 seconds for MQTT data to arrive |
| MQTT disconnects | Auto-reconnects with exponential backoff |
| SSL timeout on startup | API retries 3x automatically (2s/4s/8s backoff) |
| Wrong device type | Set `ECOFLOW_DEVICE_SNS` explicitly |
| No circuit names | Name circuits in the EcoFlow app first |
| `--dump` shows no data | Ensure device serial numbers are correct |
| Web dashboard not accessible | Check firewall allows port 5000, use `0.0.0.0` binding |
| Docker can't connect | Ensure `.env` is mounted or env vars are set |

## Acknowledgments

- [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) -- Home Assistant integration used as API reference
- [Mark-Hicks/ecoflow-api-examples](https://github.com/Mark-Hicks/ecoflow-api-examples) -- Public API signing examples

## License

[MIT](LICENSE) -- use it however you like.
