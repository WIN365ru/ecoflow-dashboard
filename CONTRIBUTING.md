# Contributing

Thanks for your interest in contributing to EcoFlow Dashboard!

## How to Contribute

### Bug Reports
- Open an [issue](https://github.com/WIN365ru/ecoflow-dashboard/issues) with steps to reproduce
- Include your device model, Python version, and OS
- Attach `--debug` output if possible

### Feature Requests
- Open an issue describing the feature and why it would be useful
- If you have a specific EcoFlow device, mention which data points you'd like to see

### Pull Requests
1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Test with your devices (`python -m ecoflow_dashboard --debug`)
5. Submit a PR with a clear description

### Adding Device Support
If you have an EcoFlow device not yet supported:
1. Run `python -m ecoflow_dashboard --dump` to capture all MQTT data keys
2. Open an issue with the dump output (redact your serial numbers)
3. We'll map the fields to dashboard panels

## Development Setup

```bash
git clone https://github.com/WIN365ru/ecoflow-dashboard.git
cd ecoflow-dashboard
pip install -e .
cp .env.example .env  # fill in credentials
python -m ecoflow_dashboard --debug
```

## Project Structure

- `config.py` — configuration loading
- `api.py` — EcoFlow API authentication
- `mqtt_client.py` — MQTT connection and data store
- `dashboard.py` — Rich CLI terminal UI
- `web.py` — Flask web dashboard
- `controls.py` — device command definitions
- `logger.py` — SQLite data logging
