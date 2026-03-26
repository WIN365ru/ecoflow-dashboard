from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console

from . import __version__
from .api import DeviceInfo, fetch_device_list, fetch_device_quota, fetch_mqtt_credentials
from .config import AUTH_PRIVATE, load_config
from .dashboard import DELTA_PRO, SMART_HOME_PANEL, run_dashboard
from .logger import DataLogger
from .mqtt_client import EcoFlowMqttClient
from .version_check import VersionChecker

console = Console()


def _detect_type(product_name: str) -> str:
    name = product_name.lower()
    if "delta" in name and "pro" in name:
        return DELTA_PRO
    if "panel" in name:
        return SMART_HOME_PANEL
    return "unknown"


def _detect_type_from_sn(sn: str) -> str:
    """Guess device type from serial number prefix."""
    s = sn.upper()
    if s.startswith("SP"):
        return SMART_HOME_PANEL
    # Delta Pro SNs vary — default to delta_pro for anything else
    return DELTA_PRO


def main() -> None:
    parser = argparse.ArgumentParser(description="EcoFlow CLI Dashboard")
    parser.add_argument("--version", action="version", version=f"ecoflow-dashboard {__version__}")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--dump", action="store_true", help="Dump all device data keys and exit")
    parser.add_argument("--log-interval", type=int, default=300, help="SQLite logging interval in seconds (default: 300)")
    parser.add_argument("--db", default="ecoflow_history.db", help="SQLite database path (default: ecoflow_history.db)")
    parser.add_argument("--no-log", action="store_true", help="Disable SQLite logging")
    parser.add_argument("--web", action="store_true", help="Start web dashboard instead of CLI")
    parser.add_argument("--web-port", type=int, default=5000, help="Web dashboard port (default: 5000)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    config = load_config(args.env_file)

    device_types: dict[str, str] = {}
    device_names: dict[str, str] = {}

    if config.auth_mode == AUTH_PRIVATE:
        # Private API: use manually configured SNs, login to verify creds
        console.print(f"[dim]Private API mode — using {len(config.device_sns)} configured device(s)[/]")
        for sn in config.device_sns:
            dtype = _detect_type_from_sn(sn)
            device_types[sn] = dtype
            device_names[sn] = sn
            console.print(f"  {sn} (detected: {dtype})")
    else:
        # Public API: auto-discover
        with console.status("Fetching device list..."):
            devices: list[DeviceInfo] = fetch_device_list(config)

        if not devices:
            console.print("[red]No devices found on your EcoFlow account.[/]")
            raise SystemExit(1)

        console.print(f"Found {len(devices)} device(s):")
        for d in devices:
            dtype = _detect_type(d.product_name)
            device_types[d.sn] = dtype
            device_names[d.sn] = d.device_name or d.product_name
            status = "[green]online[/]" if d.online else "[red]offline[/]"
            console.print(f"  {d.device_name} ({d.product_name}) [{d.sn}] {status}")

        # Filter to configured SNs if specified
        if config.device_sns:
            device_types = {sn: t for sn, t in device_types.items() if sn in config.device_sns}
            device_names = {sn: n for sn, n in device_names.items() if sn in config.device_sns}

    if not device_types:
        console.print("[red]No matching devices to monitor.[/]")
        raise SystemExit(1)

    # Fetch MQTT credentials (retry on network errors)
    import time as _t
    mqtt_creds = None
    for attempt in range(1, 6):
        try:
            with console.status(f"Authenticating & fetching MQTT credentials (attempt {attempt}/5)..."):
                mqtt_creds = fetch_mqtt_credentials(config)
            break
        except Exception as e:
            err = str(e)
            if "resolve" in err.lower() or "timeout" in err.lower() or "connection" in err.lower():
                console.print(f"[yellow]Network error (attempt {attempt}/5): {err[:80]}[/]")
                if attempt < 5:
                    console.print(f"[dim]Retrying in {attempt * 10}s...[/]")
                    _t.sleep(attempt * 10)
                else:
                    console.print("[red]Failed to connect after 5 attempts. Check your internet connection.[/]")
                    raise SystemExit(1)
            else:
                raise

    # Create MQTT client
    mqtt_client = EcoFlowMqttClient(mqtt_creds, list(device_types.keys()), config.auth_mode)

    # Pre-populate with initial data (Public API only)
    if config.auth_mode != AUTH_PRIVATE:
        with console.status("Fetching initial device data..."):
            for sn in device_types:
                try:
                    quota = fetch_device_quota(config, sn)
                    mqtt_client.set_initial_data(sn, quota)
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not fetch initial data for {sn}: {e}[/]")

    # Start MQTT
    mqtt_client.start()
    console.print("[green]MQTT connected.[/]")

    if args.dump:
        import time as _time
        console.print("Waiting 10s for MQTT data...")
        _time.sleep(10)
        for sn in device_types:
            data = mqtt_client.get_device_data(sn)
            console.print(f"\n[bold]=== {sn} ({device_types[sn]}) — {len(data)} keys ===[/]")
            for k in sorted(data):
                console.print(f"  {k} = {data[k]}")
        mqtt_client.stop()
        return

    # Start Telegram alerts
    alerter = None
    if config.telegram_token and config.telegram_chat_id:
        from .alerts import AlertManager
        alerter = AlertManager(
            mqtt_client, device_types, device_names,
            config.telegram_token, config.telegram_chat_id,
            energy_rate=config.energy_rate,
            energy_rate_night=config.energy_rate_night,
            energy_currency=config.energy_currency,
            db_path=args.db,
        )
        alerter.start()
        console.print("[dim]Telegram alerts enabled[/]")

    # Start solar forecast (before bot so bot can reference it)
    solar_forecast = None
    if config.latitude and config.longitude:
        from .solar_forecast import SolarForecast
        solar_forecast = SolarForecast(
            config.latitude, config.longitude,
            panel_watts_peak=config.solar_peak_watts,
            alert_callback=alerter._send if alerter else None,
        )
        solar_forecast.start()
        console.print(f"[dim]Solar forecast enabled ({config.latitude}, {config.longitude})[/]")

    # Start Telegram bot (command handler)
    tg_bot = None
    if config.telegram_token and config.telegram_chat_id:
        from .telegram_bot import TelegramBot
        tg_bot = TelegramBot(
            config.telegram_token, config.telegram_chat_id,
            mqtt_client, device_types, device_names,
            energy_rate=config.energy_rate,
            energy_rate_night=config.energy_rate_night,
            energy_day_start=config.energy_day_start,
            energy_day_end=config.energy_day_end,
            energy_currency=config.energy_currency,
            circuit_names=config.circuit_names,
            db_path=args.db,
        )
        if solar_forecast:
            tg_bot._solar_forecast = solar_forecast
        tg_bot.start()
        console.print("[dim]Telegram bot commands enabled[/]")

    # Start local API client (LAN direct connection)
    local_client = None
    if config.local_devices:
        from .local_api import LocalApiClient, LocalDevice
        local_devs = [LocalDevice(ip=ip, sn=sn) for ip, sn in config.local_devices]
        local_client = LocalApiClient(local_devs)
        local_client.start()
        console.print(f"[dim]Local API: {len(local_devs)} device(s) on LAN[/]")

    # Start charge scheduler
    scheduler = None
    from .scheduler import ChargeScheduler
    sched = ChargeScheduler(mqtt_client, device_types, alerter=alerter)
    if sched.enabled:
        sched.start()
        scheduler = sched
        console.print("[dim]Charge scheduler enabled[/]")

    # Start data logger
    data_logger: DataLogger | None = None
    if not args.no_log:
        data_logger = DataLogger(mqtt_client, device_types, db_path=args.db, interval=args.log_interval)
        data_logger.start()
        console.print(f"[dim]Logging to {args.db} every {args.log_interval}s[/]")

    # Check for updates (non-blocking)
    version_checker = VersionChecker()
    version_checker.start()

    if args.web:
        from .web import run_web
        console.print(f"Starting web dashboard v{__version__} on http://0.0.0.0:{args.web_port}")
        try:
            run_web(mqtt_client, device_types, device_names, port=args.web_port, db_path=args.db,
                    alerter=alerter, energy_rate=config.energy_rate, energy_rate_night=config.energy_rate_night,
                    energy_day_start=config.energy_day_start, energy_day_end=config.energy_day_end,
                    energy_currency=config.energy_currency, circuit_names=config.circuit_names)
        except KeyboardInterrupt:
            pass
        finally:
            if scheduler:
                scheduler.stop()
            if tg_bot:
                tg_bot.stop()
            if alerter:
                alerter.stop()
            if data_logger:
                data_logger.stop()
            mqtt_client.stop()
            console.print("\nWeb dashboard stopped.")
    else:
        # Check for updates (non-blocking)
        version_checker = VersionChecker()
        version_checker.start()

        console.print(f"Starting dashboard v{__version__}...")

        try:
            run_dashboard(mqtt_client, device_types, device_names, version_checker=version_checker, alerter=alerter,
                          energy_rate=config.energy_rate, energy_rate_night=config.energy_rate_night,
                          energy_day_start=config.energy_day_start, energy_day_end=config.energy_day_end,
                          energy_currency=config.energy_currency, circuit_names=config.circuit_names)
        except KeyboardInterrupt:
            pass
        finally:
            if tg_bot:
                tg_bot.stop()
            if scheduler:
                scheduler.stop()
            if alerter:
                alerter.stop()
            if data_logger:
                data_logger.stop()
            mqtt_client.stop()
            console.print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
