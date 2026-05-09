"""Solar forecast using Open-Meteo free API (no API key needed)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)


class SolarForecast:
    """Fetches solar irradiance forecast and predicts daily yield."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        panel_watts_peak: float = 400,
        panel_efficiency: float = 0.15,
        alert_callback=None,
    ) -> None:
        self._lat = latitude
        self._lon = longitude
        self._peak_watts = panel_watts_peak
        self._efficiency = panel_efficiency
        self._alert = alert_callback
        self._forecast: dict = {}  # date_str → {hours: [...], total_kwh, sunrise, sunset}
        self._last_fetch: float = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._lat and self._lon)

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="solar-forecast")
        self._thread.start()
        log.info("Solar forecast started for %.2f, %.2f", self._lat, self._lon)

    def stop(self) -> None:
        self._stop.set()

    def get_forecast(self, date_str: str | None = None) -> dict | None:
        """Get forecast for a date (YYYY-MM-DD). None = today."""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        return self._forecast.get(date_str)

    def get_tomorrow(self) -> dict | None:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        return self._forecast.get(tomorrow)

    def upcoming_rain_probability(self, hours_ahead: int = 3) -> int:
        """Max % precipitation probability across the next `hours_ahead` hours.
        Returns 0 if forecast hasn't loaded yet."""
        times = getattr(self, "_rain_times", None)
        probs = getattr(self, "_rain_prob", None)
        if not times or not probs:
            return 0
        now = datetime.now()
        horizon = now + timedelta(hours=hours_ahead)
        peak = 0
        for t, p in zip(times, probs):
            try:
                ts = datetime.fromisoformat(t)
            except Exception:
                continue
            if ts < now or ts > horizon:
                continue
            if p is None:
                continue
            try:
                peak = max(peak, int(p))
            except (TypeError, ValueError):
                continue
        return peak

    def get_recommendation(self) -> str:
        """Should we charge from grid tonight or wait for solar tomorrow?"""
        tomorrow = self.get_tomorrow()
        if not tomorrow:
            return ""
        kwh = tomorrow.get("total_kwh", 0)
        if kwh > 3.0:
            return f"☀️ Good solar tomorrow ({kwh:.1f} kWh expected) — skip grid charging"
        elif kwh > 1.5:
            return f"🌤 Moderate solar tomorrow ({kwh:.1f} kWh) — partial grid charge recommended"
        else:
            return f"☁️ Low solar tomorrow ({kwh:.1f} kWh) — charge from grid tonight"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._fetch()
            except Exception as e:
                log.warning("Solar forecast fetch failed: %s", e)
            # Refresh every 3 hours
            self._stop.wait(3 * 3600)

    def _fetch(self) -> None:
        """Fetch 3-day forecast from Open-Meteo."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "hourly": "shortwave_radiation,cloud_cover,precipitation_probability",
            "daily": "sunrise,sunset",
            "timezone": "auto",
            "forecast_days": 3,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        daily = data.get("daily", {})
        times = hourly.get("time", [])
        radiation = hourly.get("shortwave_radiation", [])
        cloud = hourly.get("cloud_cover", [])
        rain_prob = hourly.get("precipitation_probability", [])
        # Cache the raw hourly precipitation series for upcoming_rain_probability().
        self._rain_times = times
        self._rain_prob = rain_prob

        # Group by day
        by_day: dict[str, list] = {}
        for i, t in enumerate(times):
            day = t[:10]
            if day not in by_day:
                by_day[day] = []
            ghi = radiation[i] if i < len(radiation) else 0
            cc = cloud[i] if i < len(cloud) else 0
            # Estimate panel output: GHI (W/m²) × efficiency × panel_area
            # Simplified: ratio of GHI to standard (1000 W/m²) × peak watts
            panel_watts = (ghi / 1000) * self._peak_watts * (1 - cc / 200)
            panel_watts = max(0, panel_watts)
            by_day[day].append({
                "hour": t[11:16],
                "ghi": ghi,
                "cloud": cc,
                "est_watts": round(panel_watts),
            })

        # Build forecasts
        sunrises = daily.get("sunrise", [])
        sunsets = daily.get("sunset", [])

        for i, (day, hours) in enumerate(by_day.items()):
            total_wh = sum(h["est_watts"] for h in hours)  # each entry is 1 hour
            self._forecast[day] = {
                "hours": hours,
                "total_kwh": round(total_wh / 1000, 2),
                "peak_watts": max(h["est_watts"] for h in hours) if hours else 0,
                "sunrise": sunrises[i][11:16] if i < len(sunrises) else "",
                "sunset": sunsets[i][11:16] if i < len(sunsets) else "",
                "avg_cloud": round(sum(h["cloud"] for h in hours) / len(hours)) if hours else 0,
            }

        self._last_fetch = time.time()
        log.info("Solar forecast updated: %s", {d: f"{v['total_kwh']}kWh" for d, v in self._forecast.items()})

        # Send evening recommendation if alert callback available
        if self._alert and datetime.now().hour >= 20:
            rec = self.get_recommendation()
            if rec:
                self._alert(f"🔮 *Solar Forecast*\n{rec}")
