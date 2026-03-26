<div align="center">

# EcoFlow Dashboard

**Real-time CLI + Web dashboard for EcoFlow power stations & Smart Home Panel**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED.svg)](https://github.com/WIN365ru/ecoflow-dashboard/pkgs/container/ecoflow-dashboard)
[![EcoFlow](https://img.shields.io/badge/EcoFlow-Cloud_API-orange.svg)](https://www.ecoflow.com/)

Monitor battery status, power consumption, solar production, and control your EcoFlow devices from the terminal or any browser.

</div>

---

## Preview

### CLI Dashboard

```
EcoFlow Dashboard  v0.9.13  MQTT Connected  TG ✓  2026-03-26 09:30:55

  Delta Pro (DCEBZ8ZEBXXXXXX) ☀ 83%     Delta Pro (DCEBZ8ZEBXXXXXX)  90%
+-----------------------------------------+-----------------------------------------+
| ########################--------------- | ################################------- |
|                                         |                                         |
| Solar In  44 W (52.5V 0.8A) AC In 0 W   | Solar In       0 W  AC In        0 W    |
| AC Out         0 W  12V/Car Out    0 W  | AC Out         0 W  12V/Car Out  0 W    |
| Total In      44 W  Total Out      0 W  | Total In       0 W  Total Out    0 W    |
|                                         |                                         |
| Solar / MPPT                            |                                         |
| PV Input      44 W  PV Voltage  52.5 V  |                                         |
| PV Current  0.84 A  PV Power    44.1 W  |                                         |
| MPPT Eff      97%   Solar→Batt    69%   |                                         |
| Charge Source Solar  Max DC Cur    8 A  |                                         |
| Lifetime   926 kWh  MPPT Hours   4482h  |                                         |
|                                         |                                         |
| Charge Time  1d 0h  Voltage     50.5 V  | Idle Time        --  Voltage    49.8 V  |
| Current     +0.6 A  DC Conv      45 W   | Current      -0.1 A  DC Conv     1.4 W  |
| Cell V  3.35-3.37V  Δ22mV  24-24°C      | Cell V  3.30-3.33V  Δ30mV  24-24°C      |
| Batt/Inv   24/30°C  MPPT/MOS  42/24°C   | Batt/Inv   24/28°C  MPPT/MOS  30/25°C   |
| Cycles       569  Limits   10% - 100%   | Cycles       655  Limits   10% - 100%   |
| Fan      Off (Lv1)  Beep           ON   | Fan      Off (Auto)  Beep          ON   |
|                                         |                                         |
| PD 1.2.0.156  MPPT 3.1.0.50             | PD 1.2.0.156  MPPT 3.1.0.50             |
| BMS 1.1.5.35  Inv 2.1.1.158             | BMS 1.1.5.35  Inv 2.1.1.158             |
|                                         |                                         |
| Lifetime Energy                         | Lifetime Energy                         |
| AC Chg  1919 kWh  Solar  926 kWh        | AC Chg  2590 kWh  Solar  426 kWh        |
| AC Dsg  2189 kWh  DC Dsg  13 kWh        | AC Dsg  2254 kWh  DC Dsg   6 kWh        |
| Health: 94% (Excellent) 3.17/3.81 kWh   | Health: 93% (Excellent) 3.31/3.68 kWh   |
+-----------------------------------------+-----------------------------------------+

+------------------------ Smart Home Panel (SP10ZEWXXXXXXXX) -----------------------+
| Grid         ON (230V 50Hz)  Grid Today  6.85 kWh ($0.55) (Day: $0.10)            |
| Combined  87%  6.41 kWh     Limits       --     Sched Chg         OFF             |
|                                                                                   |
| Batt 1: 91% 3.28 kWh 25°C  Standby                        Chg:9d 8h               |
| Batt 2: 83% 3.00 kWh 23°C  Standby                        Chg:8d 15h              |
|                                                                                   |
|                            Circuits                                               |
| #  Name          Power  Mode Pri │ #  Name          Power  Mode Pri               |
| 1  Подвал        165 W  Auto  -  │ 7  Свет2\Су       0 W  Auto  6                 |
| 2  Кабинет       735 W  Auto  1  │ 8  Столовая      35 W  Auto  7                 |
| 3  Кухня           0 W  Auto  2  │ 9  Кабинет\      45 W  Auto  8                 |
| 4  Т пол           0 W  Auto  3  │10  Свет3          0 W  Auto  9                 |
| 5  Свет            0 W  Auto  4  │11  DP.0352        0 W  Auto  -                 |
| 6  Гост\Пер       60 W  Auto  5  │12  DP.0801        0 W  Auto  -                 |
|                                           1.0 kW | Up: 3d 1h                      |
+------------------------------------------------------------------------------------+
  [a] AC  [d] DC  [x] XBoost  [b] Beep  [c] Charging  [+/-] Chg%  [w/s] ChgW
  [e] EPS  [g] Grid Chg B1  [h] Grid Chg B2  [1-3] Device  [Ctrl+C] Exit
```

### Web Dashboard

Access from any device on your LAN — installable as PWA on iPhone/iPad:

```bash
python -m ecoflow_dashboard --web
# Open http://<your-ip>:5000 in any browser
```

- Dark theme, responsive layout (mobile + desktop)
- Real-time auto-refresh every 2 seconds
- Control buttons (AC, DC, EPS, Grid Charge toggles)
- Charts: live rolling (1h) + historical (custom date ranges from SQLite)
- Battery health degradation chart with replacement prediction
- Power outage log with SOC and load tracking
- Energy flow diagram (Grid → SHP → Circuits)
- Solar analytics with self-consumption ratio and savings
- CSV export for spreadsheet analysis
- Installable as PWA (Add to Home Screen)

## Features

### Monitoring

- **Battery** — SOC%, health (SOH), remaining energy (kWh), charge cycles
- **Power flow** — solar, AC, DC input/output with voltage, current, frequency
- **Solar / MPPT** — PV voltage, current, power, MPPT efficiency, solar-to-battery efficiency
- **Efficiency** — system efficiency, SHP Delta Pro charging efficiency
- **Cell-level** — min/max cell voltage with delta (mV), temperature range
- **Temperatures** — battery, inverter, MPPT, MOS with fan status
- **Lifetime energy** — total AC/solar charged, AC/DC discharged (kWh)
- **Smart Home Panel** — grid status (V/Hz), 12-circuit power with custom names and priority, per-battery detail
- **Energy cost** — time-of-use pricing (day/night rates), daily cost tracking
- **Data logging** — automatic SQLite snapshots for historical analysis

### Analytics (Web)

- **Battery degradation** — SOH% chart over time with replacement prediction
- **Power outage log** — auto-detect grid drops, log duration, SOC used, peak/avg load
- **Energy flow diagram** — real-time Sankey-style power flow visualization
- **Solar analytics** — daily generation chart, self-consumption ratio, money saved
- **Historical charts** — custom date ranges, up to 1 year+
- **CSV export** — download any time range as spreadsheet

### Controls (keyboard shortcuts)

**Delta Pro:**

| Key | Action | Range |
|-----|--------|-------|
| `a` | Toggle AC output | On / Off |
| `d` | Toggle DC output | On / Off |
| `x` | Toggle X-Boost | On / Off |
| `b` | Toggle beeper | On / Off |
| `c` | Pause / resume AC charging | 0 — 2900 W |
| `+` / `-` | Adjust max charge limit | 50 — 100% |
| `[` / `]` | Adjust min discharge limit | 0 — 30% |
| `w` / `s` | Adjust AC charge power | 200 — 2900 W |

**Smart Home Panel:**

| Key | Action |
|-----|--------|
| `e` | Toggle EPS mode |
| `g` | Toggle grid charge (Battery 1) |
| `h` | Toggle grid charge (Battery 2) |

### Telegram Alerts

| Alert | Trigger |
|-------|---------|
| ⚡ Grid Outage / ✅ Restore | Grid power lost/restored |
| 🔋 Discharge milestones | 80%, 60%, 40%, 20%, 15%, 10%, 5% |
| ☀️ Solar charge milestones | 50%, 80%, 90%, 100% |
| 🔋 Battery full | SOC reaches 100% |
| 🌡️ High temperature | Any sensor > threshold |
| 🔴 Device offline | No data for 5 min |
| 📊 Daily summary | Configurable hour (default 8 PM) |
| 📅 Monthly report | Energy consumption + cost on 1st of month |

### Charge Scheduler

| Feature | Env var |
|---------|---------|
| Grid charge window | `SCHEDULE_CHARGE_START=23:00` / `SCHEDULE_CHARGE_STOP=06:00` |
| Grid SOC limit | `SCHEDULE_MAX_SOC_GRID=80` |
| Solar SOC limit | `SCHEDULE_MAX_SOC_SOLAR=100` |

## Supported Devices

| Device | Monitoring | Controls |
|--------|-----------|----------|
| **Delta Pro** | Full | AC / DC / XBoost / Beep / Charging |
| **Smart Home Panel** | Full | EPS / Grid Charge |
| Other EcoFlow devices | Partial (auto-detect) | — |

## Installation

### Option A: pip (local)

```bash
git clone https://github.com/WIN365ru/ecoflow-dashboard.git
cd ecoflow-dashboard
pip install -e .
cp .env.example .env   # fill in credentials
python -m ecoflow_dashboard
```

### Option B: Docker

```bash
docker pull ghcr.io/win365ru/ecoflow-dashboard:latest

docker run -d --name ecoflow-dashboard \
  --env-file .env \
  -e TZ=Europe/Kyiv \
  -p 5000:5000 \
  -v ecoflow-data:/data \
  --restart unless-stopped \
  ghcr.io/win365ru/ecoflow-dashboard:latest
```

### Option C: Docker Compose

```bash
curl -O https://raw.githubusercontent.com/WIN365ru/ecoflow-dashboard/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/WIN365ru/ecoflow-dashboard/main/.env.example
cp .env.example .env   # fill in credentials
docker compose up -d
```

### Option D: Portainer

1. **Stacks** → **Add stack**
2. Paste contents of [`docker-compose.portainer.yml`](docker-compose.portainer.yml)
3. Set environment variables in Portainer UI
4. Deploy → `http://<server-ip>:5000`

## Configuration

### Authentication

```env
# Option 1: Private API (works immediately)
ECOFLOW_EMAIL=your_email@example.com
ECOFLOW_PASSWORD=your_password
ECOFLOW_DEVICE_SNS=DELTA_SN_1,DELTA_SN_2,PANEL_SN

# Option 2: Public API (requires developer portal)
ECOFLOW_ACCESS_KEY=your_access_key
ECOFLOW_SECRET_KEY=your_secret_key
```

### Energy Cost (Time-of-Use)

```env
ENERGY_RATE=0.10              # Day rate per kWh
ENERGY_RATE_NIGHT=0.02        # Night rate (0 = flat rate)
ENERGY_DAY_START=7            # Day starts at this hour
ENERGY_DAY_END=23             # Night starts at this hour
ENERGY_CURRENCY=$             # Currency symbol
```

### Circuit Names

```env
CIRCUIT_NAMES=Подвал,Кабинет,Кухня,Т пол,Свет,Гост\Пер,Свет2\Су,Столовая,Кабинет\,Свет3,,
```

### Telegram Alerts

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=123456789
ALERT_DAILY_SUMMARY=20        # Hour for daily summary (0-23)
ALERT_BATTERY_LOW=20          # SOC % threshold
ALERT_HIGH_TEMP=45            # °C threshold
```

### Charge Scheduler

```env
SCHEDULE_CHARGE_START=23:00   # Grid charge window start
SCHEDULE_CHARGE_STOP=06:00    # Grid charge window end
SCHEDULE_MAX_SOC_GRID=80      # Stop grid charging at 80%
SCHEDULE_MAX_SOC_SOLAR=100    # Allow solar to 100%
```

### Docker

```env
TZ=Europe/Kyiv                # Container timezone
```

## Usage

```bash
python -m ecoflow_dashboard              # CLI dashboard
python -m ecoflow_dashboard --web        # Web dashboard (port 5000)
python -m ecoflow_dashboard --web --web-port 8080
python -m ecoflow_dashboard --debug      # Show raw MQTT messages
python -m ecoflow_dashboard --dump       # Dump all device data keys
python -m ecoflow_dashboard --log-interval 60   # Log every 60s
python -m ecoflow_dashboard --no-log     # Disable SQLite logging
python -m ecoflow_dashboard --version    # Show version
```

## API Endpoints (Web)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web dashboard |
| `GET /api/devices` | All device data (JSON) |
| `GET /api/live?sn=X` | Live data buffer (1h) |
| `GET /api/history?sn=X&hours=24` | Historical data from SQLite |
| `GET /api/history?sn=X&start=DATE&end=DATE` | Custom date range |
| `GET /api/history/range` | Available data date range |
| `GET /api/degradation?sn=X` | SOH% over time with prediction |
| `GET /api/outages` | Power outage log |
| `GET /api/solar?sn=X&days=30` | Solar analytics |
| `GET /api/export/csv?sn=X&hours=24` | CSV download |
| `POST /api/command` | Send device command |
| `GET /manifest.json` | PWA manifest |

## Project Structure

```
ecoflow-dashboard/
  .env.example                # Configuration template
  pyproject.toml              # Dependencies and packaging
  Dockerfile                  # Docker image build
  docker-compose.yml          # Docker Compose (classic)
  docker-compose.portainer.yml # Portainer stack
  ecoflow_dashboard/
    __main__.py               # CLI entry point
    config.py                 # Environment loading, TOU pricing
    api.py                    # REST API with retry logic
    mqtt_client.py            # MQTT connection and commands
    dashboard.py              # Rich terminal UI
    controls.py               # Keyboard input and commands
    web.py                    # Flask web dashboard + charts
    logger.py                 # SQLite logging + outage detection
    alerts.py                 # Telegram notifications + scheduler milestones
    scheduler.py              # Time-based charge automation
    version_check.py          # GitHub release checker
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| All values show 0 | Wait 10-15 seconds for MQTT data |
| MQTT disconnects | Auto-reconnects with exponential backoff |
| SSL timeout | API retries 3x automatically |
| Wrong device type | Set `ECOFLOW_DEVICE_SNS` explicitly |
| Charts wrong timezone | Set `TZ=Your/Timezone` in Docker env |
| Web not accessible | Check firewall allows port 5000 |
| Telegram not sending | Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` |
| Circuit names empty | Set `CIRCUIT_NAMES` in .env |

## Acknowledgments

- [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) — API reference
- [Mark-Hicks/ecoflow-api-examples](https://github.com/Mark-Hicks/ecoflow-api-examples) — Public API signing

## License

[MIT](LICENSE)
