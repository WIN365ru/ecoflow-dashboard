from __future__ import annotations

import logging
import threading

import requests

from . import __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "WIN365ru/ecoflow-dashboard"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# Also check tags if no releases exist
GITHUB_TAGS_URL = f"https://api.github.com/repos/{GITHUB_REPO}/tags"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'v0.1.0' or '0.1.0' into (0, 1, 0)."""
    v = v.lstrip("vV").strip()
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) or (0,)


def check_for_update() -> str | None:
    """Check GitHub for a newer version. Returns message string or None."""
    try:
        # Try releases first
        resp = requests.get(GITHUB_API_URL, timeout=5, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 200:
            data = resp.json()
            latest = data.get("tag_name", "")
            if latest and _parse_version(latest) > _parse_version(__version__):
                url = data.get("html_url", f"https://github.com/{GITHUB_REPO}")
                return f"Update available: {__version__} -> {latest}  {url}"
            return None

        # No releases — try tags
        resp = requests.get(GITHUB_TAGS_URL, timeout=5, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 200:
            tags = resp.json()
            if tags:
                latest = tags[0].get("name", "")
                if latest and _parse_version(latest) > _parse_version(__version__):
                    return f"Update available: {__version__} -> {latest}  https://github.com/{GITHUB_REPO}"

    except Exception:
        log.debug("Version check failed", exc_info=True)

    return None


class VersionChecker:
    """Non-blocking version checker that runs in background."""

    def __init__(self) -> None:
        self.message: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        self.message = check_for_update()
        if self.message:
            log.info(self.message)
