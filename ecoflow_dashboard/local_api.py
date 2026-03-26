"""EcoFlow local API client — connects to Delta Pro on port 8055.

Binary protocol: aa02 frames with protobuf-like payloads.
Frame structure:
  [0xAA 0x02] [len:2LE] [header:4] [seq:4] [src:2] [dst:2] [cmd_set] [cmd_id] [payload...] [crc16:2]

This module provides LAN-based data collection as a supplement to (or replacement for)
cloud MQTT. Data is merged into the same shared dict used by the MQTT client.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAGIC = b"\xaa\x02"
RECV_BUF = 8192
RECONNECT_DELAY = 10

# Known module types (from community reverse engineering)
MODULE_NAMES = {
    1: "pd",       # Power Distribution
    2: "bms",      # Battery Management
    3: "inv",      # Inverter
    4: "bms_slave", # Secondary BMS
    5: "mppt",     # MPPT / Solar
    6: "ems",      # Energy Management
}


@dataclass
class LocalDevice:
    ip: str
    port: int = 8055
    sn: str = ""  # Mapped serial number


def _crc16(data: bytes) -> int:
    """CRC-16/MODBUS."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _parse_frames(buf: bytes) -> list[tuple[bytes, int]]:
    """Extract aa02 frames from buffer. Returns [(frame_data, end_pos), ...]."""
    frames = []
    pos = 0
    while pos < len(buf) - 4:
        # Find magic bytes
        idx = buf.find(MAGIC, pos)
        if idx < 0 or idx + 4 > len(buf):
            break
        # Length is 2 bytes LE after magic
        frame_len = struct.unpack_from("<H", buf, idx + 2)[0]
        frame_end = idx + 4 + frame_len
        if frame_end > len(buf):
            break  # Incomplete frame, wait for more data
        frames.append((buf[idx:frame_end], frame_end))
        pos = frame_end
    return frames


def _decode_frame(frame: bytes) -> dict | None:
    """Attempt to decode known fields from an aa02 frame.

    This is a best-effort decoder based on community reverse engineering.
    Different frame types have different layouts; we extract what we can.
    """
    if len(frame) < 20:
        return None

    # Frame layout (approximate, varies by device/firmware):
    # [0:2]   magic aa02
    # [2:4]   payload length (LE)
    # [4:6]   header/checksum
    # [6:10]  sequence number (LE)
    # [10:12] src module (LE) — 0x0003 = device module 3
    # [12:14] dst (LE) — 0xFFFF = broadcast, 0x0020 = app
    # [14]    version
    # [15]    cmd_set
    # [16]    cmd_id
    # [17:]   payload (protobuf or raw)

    try:
        payload_len = struct.unpack_from("<H", frame, 2)[0]
        if len(frame) < 17:
            return None

        # Try to extract module addressing
        src = frame[10] if len(frame) > 10 else 0
        dst = frame[12] if len(frame) > 12 else 0
        cmd_set = frame[14] if len(frame) > 14 else 0
        cmd_id = frame[15] if len(frame) > 15 else 0

        return {
            "_frame_len": payload_len,
            "_src": src,
            "_dst": dst,
            "_cmd_set": cmd_set,
            "_cmd_id": cmd_id,
            "_payload": frame[16:-2] if len(frame) > 18 else b"",
            "_raw": frame,
        }
    except Exception:
        return None


class LocalApiClient:
    """Connects to EcoFlow devices on LAN port 8055 and collects raw frame data."""

    def __init__(self, devices: list[LocalDevice], data_callback=None) -> None:
        """
        Args:
            devices: List of LocalDevice with IP and mapped SN.
            data_callback: Optional callback(sn, key, value) for each decoded field.
        """
        self._devices = devices
        self._callback = data_callback
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._connected: dict[str, bool] = {d.ip: False for d in devices}
        self._frame_counts: dict[str, int] = {d.ip: 0 for d in devices}
        self._last_frame_ts: dict[str, float] = {d.ip: 0.0 for d in devices}
        # Raw frame buffer for analysis
        self._recent_frames: dict[str, list[dict]] = {d.ip: [] for d in devices}

    def start(self) -> None:
        for dev in self._devices:
            t = threading.Thread(target=self._reader_loop, args=(dev,), daemon=True)
            t.start()
            self._threads.append(t)
            log.info("Local API client started for %s (%s:%d)", dev.sn or dev.ip, dev.ip, dev.port)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    def is_connected(self, ip: str) -> bool:
        return self._connected.get(ip, False)

    def frame_count(self, ip: str) -> int:
        return self._frame_counts.get(ip, 0)

    def get_recent_frames(self, ip: str, limit: int = 20) -> list[dict]:
        """Get recent decoded frames for analysis."""
        return list(self._recent_frames.get(ip, []))[-limit:]

    def get_status(self) -> dict[str, dict]:
        """Get connection status for all devices."""
        result = {}
        for dev in self._devices:
            result[dev.ip] = {
                "sn": dev.sn,
                "connected": self._connected.get(dev.ip, False),
                "frames": self._frame_counts.get(dev.ip, 0),
                "last_frame": self._last_frame_ts.get(dev.ip, 0),
            }
        return result

    def _reader_loop(self, dev: LocalDevice) -> None:
        """Persistent connection loop for one device."""
        buf = b""
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((dev.ip, dev.port))
                self._connected[dev.ip] = True
                log.info("Connected to %s:%d", dev.ip, dev.port)

                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(RECV_BUF)
                        if not chunk:
                            log.warning("Connection closed by %s", dev.ip)
                            break
                        buf += chunk

                        # Parse complete frames
                        frames = _parse_frames(buf)
                        if frames:
                            # Update buffer to start after last complete frame
                            last_end = frames[-1][1]
                            buf = buf[last_end:]

                            for frame_data, _ in frames:
                                self._frame_counts[dev.ip] = self._frame_counts.get(dev.ip, 0) + 1
                                self._last_frame_ts[dev.ip] = time.time()

                                decoded = _decode_frame(frame_data)
                                if decoded:
                                    # Keep recent frames for analysis
                                    recent = self._recent_frames.get(dev.ip, [])
                                    recent.append(decoded)
                                    if len(recent) > 100:
                                        recent = recent[-100:]
                                    self._recent_frames[dev.ip] = recent

                        # Prevent buffer from growing too large
                        if len(buf) > 65536:
                            buf = buf[-8192:]

                    except socket.timeout:
                        continue
                    except Exception as e:
                        log.warning("Read error from %s: %s", dev.ip, e)
                        break

            except (socket.error, OSError) as e:
                log.warning("Connection to %s:%d failed: %s", dev.ip, dev.port, e)
            finally:
                self._connected[dev.ip] = False
                try:
                    sock.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                log.info("Reconnecting to %s in %ds...", dev.ip, RECONNECT_DELAY)
                self._stop.wait(RECONNECT_DELAY)
