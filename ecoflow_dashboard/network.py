from __future__ import annotations

import re
import subprocess


# Espressif OUI prefixes commonly used by EcoFlow devices
ESPRESSIF_OUIS = {
    "04:42:1a", "7c:df:a1", "ec:94:cb", "34:b4:72",
    "08:3a:f2", "08:b6:1f", "10:97:bd", "24:0a:c4",
    "24:6f:28", "24:dc:c3", "2c:bc:bb", "30:ae:a4",
    "34:85:18", "3c:61:05", "3c:71:bf", "40:4c:ca",
    "48:27:e2", "48:3f:da", "54:32:04", "58:bf:25",
    "70:04:1d", "74:4d:bd", "78:21:84", "7c:9e:bd",
    "80:65:99", "84:0d:8e", "84:f7:03", "8c:4b:14",
    "90:38:0c", "94:3c:c6", "94:b5:55", "98:cd:ac",
    "a0:76:4e", "a4:cf:12", "ac:67:b2", "b4:e6:2d",
    "b8:d6:1a", "bc:dd:c2", "c0:49:ef", "c4:4f:33",
    "c4:de:e2", "c8:2b:96", "cc:50:e3", "d4:d4:da",
    "d8:bf:c0", "dc:54:75", "e0:5a:1b", "e8:68:e7",
    "ec:62:60", "ec:66:d1", "f0:08:d1", "f4:12:fa",
    "fc:b4:67",
}


def find_ecoflow_ips() -> list[dict[str, str]]:
    """Scan ARP table for devices with Espressif MAC prefixes (likely EcoFlow)."""
    try:
        result = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except Exception:
        return []

    devices = []
    for line in output.splitlines():
        # Windows format: "  192.168.50.8          04-42-1a-4c-99-f5     dynamic"
        match = re.search(
            r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2})",
            line, re.IGNORECASE
        )
        if not match:
            continue
        ip = match.group(1)
        mac = match.group(2).replace("-", ":").lower()
        oui = mac[:8]
        if oui in ESPRESSIF_OUIS:
            devices.append({"ip": ip, "mac": mac})

    return devices
