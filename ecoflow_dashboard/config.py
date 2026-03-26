from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

AUTH_PUBLIC = "public"
AUTH_PRIVATE = "private"


@dataclass
class Config:
    auth_mode: str  # "public" or "private"
    # Public API
    access_key: str = ""
    secret_key: str = ""
    api_host: str = "api-e.ecoflow.com"
    # Private API
    email: str = ""
    password: str = ""
    # Common
    device_sns: list[str] | None = None
    # Telegram alerts
    telegram_token: str = ""
    telegram_chat_id: str = ""
    # Energy cost tracking
    energy_rate: float = 0.0       # cost per kWh (flat rate or day rate)
    energy_rate_night: float = 0.0 # night rate per kWh (0 = flat rate)
    energy_day_start: int = 7      # day rate starts at this hour
    energy_day_end: int = 23       # night rate starts at this hour
    energy_currency: str = "$"     # currency symbol
    # Circuit names (12 comma-separated names for SHP circuits)
    circuit_names: list[str] | None = None
    # Solar forecast (Open-Meteo, no API key needed)
    latitude: float = 0.0
    longitude: float = 0.0
    solar_peak_watts: float = 400
    # Local API (LAN direct connection)
    local_devices: list[tuple[str, str]] | None = None  # [(ip, sn), ...]


def load_config(env_file: str = ".env") -> Config:
    load_dotenv(env_file)

    access_key = os.environ.get("ECOFLOW_ACCESS_KEY", "")
    secret_key = os.environ.get("ECOFLOW_SECRET_KEY", "")
    email = os.environ.get("ECOFLOW_EMAIL", "")
    password = os.environ.get("ECOFLOW_PASSWORD", "")

    if access_key and secret_key:
        auth_mode = AUTH_PUBLIC
    elif email and password:
        auth_mode = AUTH_PRIVATE
    else:
        raise SystemExit(
            "Set either ECOFLOW_ACCESS_KEY + ECOFLOW_SECRET_KEY (Public API)\n"
            "or ECOFLOW_EMAIL + ECOFLOW_PASSWORD (Private API) in .env"
        )

    device_sns = None
    raw_sns = os.environ.get("ECOFLOW_DEVICE_SNS", "")
    if raw_sns:
        device_sns = [s.strip() for s in raw_sns.split(",") if s.strip()]

    if auth_mode == AUTH_PRIVATE and not device_sns:
        raise SystemExit(
            "Private API cannot auto-discover devices.\n"
            "Set ECOFLOW_DEVICE_SNS in .env (comma-separated serial numbers).\n"
            "You can find serial numbers in the EcoFlow app under device settings."
        )

    api_host = os.environ.get("ECOFLOW_API_HOST", "")
    if not api_host:
        api_host = "api.ecoflow.com" if auth_mode == AUTH_PRIVATE else "api-e.ecoflow.com"

    return Config(
        auth_mode=auth_mode,
        access_key=access_key,
        secret_key=secret_key,
        api_host=api_host,
        email=email,
        password=password,
        device_sns=device_sns,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        energy_rate=float(os.environ.get("ENERGY_RATE", "0")),
        energy_rate_night=float(os.environ.get("ENERGY_RATE_NIGHT", "0")),
        energy_day_start=int(os.environ.get("ENERGY_DAY_START", "7")),
        energy_day_end=int(os.environ.get("ENERGY_DAY_END", "23")),
        energy_currency=os.environ.get("ENERGY_CURRENCY", "$"),
        circuit_names=_parse_circuit_names(os.environ.get("CIRCUIT_NAMES", "")),
        latitude=float(os.environ.get("SOLAR_LATITUDE", "0")),
        longitude=float(os.environ.get("SOLAR_LONGITUDE", "0")),
        solar_peak_watts=float(os.environ.get("SOLAR_PEAK_WATTS", "400")),
        local_devices=_parse_local_devices(os.environ.get("LOCAL_DEVICES", "")),
    )


def get_energy_rate(config: Config, hour: int | None = None) -> float:
    """Get energy rate for a given hour. Supports day/night tariffs."""
    if not config.energy_rate:
        return 0.0
    if not config.energy_rate_night:
        return config.energy_rate  # flat rate
    if hour is None:
        from datetime import datetime
        hour = datetime.now().hour
    # Day rate window (e.g. 7-23)
    if config.energy_day_start <= config.energy_day_end:
        is_day = config.energy_day_start <= hour < config.energy_day_end
    else:  # overnight day window (unusual but handle it)
        is_day = hour >= config.energy_day_start or hour < config.energy_day_end
    return config.energy_rate if is_day else config.energy_rate_night


def _parse_local_devices(raw: str) -> list[tuple[str, str]] | None:
    """Parse LOCAL_DEVICES=ip1=sn1,ip2=sn2 format."""
    if not raw:
        return None
    devices = []
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            ip, sn = pair.split("=", 1)
            devices.append((ip.strip(), sn.strip()))
    return devices if devices else None


def _parse_circuit_names(raw: str) -> list[str] | None:
    if not raw:
        return None
    names = [n.strip() for n in raw.split(",")]
    # Pad to 12 if fewer provided
    while len(names) < 12:
        names.append("")
    return names[:12]
