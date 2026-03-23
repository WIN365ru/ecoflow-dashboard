from __future__ import annotations

import base64
import hashlib
import hmac
import random
import time
import uuid
from dataclasses import dataclass

import requests

from .config import AUTH_PRIVATE, AUTH_PUBLIC, Config


@dataclass
class MqttCredentials:
    host: str
    port: int
    username: str
    password: str
    # Private API extras
    user_id: str = ""
    protocol: str = "mqtts"


@dataclass
class DeviceInfo:
    sn: str
    product_name: str
    device_name: str
    online: bool


# ---------------------------------------------------------------------------
# Public API helpers (HMAC-SHA256 signed)
# ---------------------------------------------------------------------------

def _sign_request(
    access_key: str, secret_key: str, params: dict | None = None
) -> dict[str, str]:
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))

    headers_to_sign = {
        "accessKey": access_key,
        "nonce": nonce,
        "timestamp": timestamp,
    }

    parts = []
    if params:
        parts.append("&".join(f"{k}={params[k]}" for k in sorted(params)))
    parts.append("&".join(f"{k}={headers_to_sign[k]}" for k in sorted(headers_to_sign)))
    sign_str = "&".join(parts)

    sign = hmac.new(
        secret_key.encode(), sign_str.encode(), hashlib.sha256
    ).hexdigest()

    return {
        "accessKey": access_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
        "Content-Type": "application/json",
    }


def _public_get(config: Config, path: str, params: dict | None = None) -> dict:
    headers = _sign_request(config.access_key, config.secret_key, params)
    url = f"https://{config.api_host}{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"EcoFlow API error: {body.get('message', body)}")
    return body.get("data", {})


# ---------------------------------------------------------------------------
# Private API helpers (Bearer token)
# ---------------------------------------------------------------------------

_private_token: str = ""
_private_user_id: str = ""


def _private_login(config: Config) -> tuple[str, str]:
    """Login with email/password, return (token, user_id)."""
    global _private_token, _private_user_id
    if _private_token:
        return _private_token, _private_user_id

    url = f"https://{config.api_host}/auth/login"
    payload = {
        "email": config.email,
        "password": base64.b64encode(config.password.encode()).decode(),
        "scene": "IOT_APP",
        "userType": "ECOFLOW",
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"EcoFlow login failed: {body.get('message', body)}")

    data = body.get("data", {})
    _private_token = data["token"]
    _private_user_id = data["user"]["userId"]
    return _private_token, _private_user_id


def _private_get(config: Config, path: str, params: dict | None = None) -> dict:
    token, _ = _private_login(config)
    url = f"https://{config.api_host}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"EcoFlow API error: {body.get('message', body)}")
    return body.get("data", {})


# ---------------------------------------------------------------------------
# Unified API functions
# ---------------------------------------------------------------------------

def fetch_device_list(config: Config) -> list[DeviceInfo]:
    """Fetch device list. Only works with Public API."""
    data = _public_get(config, "/iot-open/sign/device/list")
    items = data if isinstance(data, list) else []
    devices = []
    for d in items:
        devices.append(
            DeviceInfo(
                sn=d.get("sn", ""),
                product_name=d.get("productName", "Unknown"),
                device_name=d.get("deviceName", d.get("sn", "")),
                online=bool(d.get("online")),
            )
        )
    return devices


def fetch_device_quota(config: Config, sn: str) -> dict:
    """Fetch full device quota. Only works with Public API."""
    return _public_get(config, "/iot-open/sign/device/quota/all", params={"sn": sn})


def fetch_mqtt_credentials(config: Config) -> MqttCredentials:
    if config.auth_mode == AUTH_PUBLIC:
        data = _public_get(config, "/iot-open/sign/certification")
        return MqttCredentials(
            host=data["url"],
            port=int(data["port"]),
            username=data["certificateAccount"],
            password=data["certificatePassword"],
        )
    else:
        token, user_id = _private_login(config)
        url = f"https://{config.api_host}/iot-auth/app/certification"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "0":
            raise RuntimeError(f"EcoFlow MQTT cert failed: {body.get('message', body)}")
        data = body.get("data", {})
        return MqttCredentials(
            host=data["url"],
            port=int(data["port"]),
            username=data["certificateAccount"],
            password=data["certificatePassword"],
            user_id=user_id,
            protocol=data.get("protocol", "mqtts"),
        )
