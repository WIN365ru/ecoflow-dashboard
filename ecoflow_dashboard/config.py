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
    energy_rate: float = 0.0       # cost per kWh
    energy_currency: str = "$"     # currency symbol


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
        energy_currency=os.environ.get("ENERGY_CURRENCY", "$"),
    )
