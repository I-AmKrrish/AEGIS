"""Weather acquisition service and irradiance estimation pipeline for AEGIS.

Fetches long-range outlook (10 days) and irradiance data from Open-Meteo (free, no key),
and current conditions from OpenWeather (if configured).
Operates on the server in a resilient background thread with backoff and error handling.
Implements the irradiance source priority:
measured (Simulink telemetry) > open-meteo > cloud-cover-estimate.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import AegisConfig

logger = logging.getLogger("aegis.weather")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def solar_irradiance_estimate(latitude: float, longitude: float, clouds: float, at: datetime) -> float:
    """Approximate GHI from location, UTC time and cloud fraction (fallback estimate)."""
    day = at.timetuple().tm_yday
    hour = at.hour + at.minute / 60 + at.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    equation_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = (hour * 60 + equation_time + 4 * longitude) % 1440
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(latitude)
    sin_elevation = (
        math.sin(lat) * math.sin(declination)
        + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    )
    if sin_elevation <= 0:
        return 0.0
    cloud_fraction = min(1.0, max(0.0, clouds / 100.0))
    cloud_factor = max(0.0, 1.0 - 0.75 * (cloud_fraction**3.4))
    return round(1000.0 * sin_elevation * 0.75 * cloud_factor, 1)


def fetch_open_meteo_forecast(lat: float, lon: float, timeout_s: float = 10.0) -> dict[str, Any]:
    """Fetch 10-day outlook and irradiance from Open-Meteo.
    
    Verified parameters:
      - hourly: shortwave_radiation,cloud_cover,precipitation_probability
      - daily: shortwave_radiation_sum,precipitation_sum,precipitation_probability_max
      - current: shortwave_radiation
      - forecast_days: 10
      - timezone: auto
    """
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "shortwave_radiation,cloud_cover,precipitation_probability",
        "daily": "shortwave_radiation_sum,precipitation_sum,precipitation_probability_max",
        "current": "shortwave_radiation",
        "forecast_days": 10,
        "timezone": "auto",
    }
    url = f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "AEGIS-SolarMicrogrid/2.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_open_meteo_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse raw Open-Meteo response into structured outlook data.
    
    Note: Open-Meteo returns shortwave_radiation_sum in MJ/m^2.
    1 MJ = 1/3.6 kWh. We retain the sum and also compute kWh/m^2.
    """
    daily_raw = raw.get("daily") or {}
    daily_dates = daily_raw.get("time") or []
    daily_rad_sum_mj = daily_raw.get("shortwave_radiation_sum") or []
    daily_precip_sum = daily_raw.get("precipitation_sum") or []
    daily_pop_max = daily_raw.get("precipitation_probability_max") or []

    daily_items: list[dict[str, Any]] = []
    for i, date_str in enumerate(daily_dates):
        rad_mj = float(daily_rad_sum_mj[i]) if i < len(daily_rad_sum_mj) and daily_rad_sum_mj[i] is not None else 0.0
        precip = float(daily_precip_sum[i]) if i < len(daily_precip_sum) and daily_precip_sum[i] is not None else 0.0
        pop_max = float(daily_pop_max[i]) if i < len(daily_pop_max) and daily_pop_max[i] is not None else 0.0
        rad_kwh = round(rad_mj / 3.6, 3)

        daily_items.append({
            "date": date_str,
            "shortwave_radiation_sum_mj_m2": rad_mj,
            "shortwave_radiation_sum_kwh_m2": rad_kwh,
            "precipitation_sum_mm": precip,
            "precipitation_probability_max": pop_max,
        })

    hourly_raw = raw.get("hourly") or {}
    hourly_times = hourly_raw.get("time") or []
    hourly_rad = hourly_raw.get("shortwave_radiation") or []
    hourly_clouds = hourly_raw.get("cloud_cover") or []
    hourly_pop = hourly_raw.get("precipitation_probability") or []

    hourly_items: list[dict[str, Any]] = []
    for i, time_str in enumerate(hourly_times[:240]):  # up to 10 days
        rad = float(hourly_rad[i]) if i < len(hourly_rad) and hourly_rad[i] is not None else 0.0
        clouds = float(hourly_clouds[i]) if i < len(hourly_clouds) and hourly_clouds[i] is not None else 0.0
        pop = float(hourly_pop[i]) if i < len(hourly_pop) and hourly_pop[i] is not None else 0.0

        hourly_items.append({
            "time": time_str,
            "shortwave_radiation_w_m2": rad,
            "cloud_cover_pct": clouds,
            "precipitation_probability_pct": pop,
        })

    current_raw = raw.get("current") or {}
    current_sw = current_raw.get("shortwave_radiation")
    if current_sw is not None:
        current_rad = float(current_sw)
    elif hourly_items:
        current_rad = hourly_items[0]["shortwave_radiation_w_m2"]
    else:
        current_rad = 0.0

    return {
        "fetched_at": iso_now(),
        "current_shortwave_radiation_w_m2": max(0.0, current_rad),
        "daily": daily_items,
        "hourly": hourly_items,
    }


def fetch_openweather_current(lat: float, lon: float, api_key: str, timeout_s: float = 8.0) -> dict[str, Any]:
    """Fetch current weather from OpenWeather API."""
    if not api_key:
        raise ValueError("OpenWeather API key not configured")

    query = urlencode({"lat": lat, "lon": lon, "units": "metric", "appid": api_key})
    url = f"https://api.openweathermap.org/data/2.5/weather?{query}"
    with urlopen(url, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_openweather_current(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenWeather current weather payload into standard schema."""
    main = data.get("main") or {}
    wind = data.get("wind") or {}
    rain = data.get("rain") or {}
    weather_list = data.get("weather") or [{}]
    clouds = (data.get("clouds") or {}).get("all", 0)

    return {
        "city": data.get("name") or "Unknown",
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "temp_max": main.get("temp_max"),
        "temp_min": main.get("temp_min"),
        "humidity": main.get("humidity"),
        "pressure_hpa": main.get("pressure"),
        "wind_kmh": round(float(wind.get("speed", 0)) * 3.6, 1),
        "wind_deg": wind.get("deg", 0),
        "clouds": clouds,
        "rain_mm_1h": float(rain.get("1h", 0)),
        "description": weather_list[0].get("description", "Clear").capitalize(),
    }


def resolve_irradiance(
    state: dict[str, Any],
    outlook: dict[str, Any] | None,
    config: AegisConfig,
    now: datetime | None = None,
) -> tuple[float, str, str]:
    """Resolve irradiance value according to source priority:
    1. measured: Simulink telemetry, valid while age <= measured_max_age_s
    2. open-meteo: Open-Meteo shortwave radiation, fresh or stale
    3. cloud-cover-estimate: Fallback mathematical estimate
    
    Returns: (irradiance_w_m2, source, quality)
    """
    if now is None:
        now = utc_now()

    # Priority 1: Measured telemetry
    last_telem_str = state.get("last_telemetry_at")
    if last_telem_str and "measured_irradiance" in state:
        try:
            telem_dt = datetime.fromisoformat(last_telem_str.replace("Z", "+00:00"))
            age_s = (now - telem_dt).total_seconds()
            if 0 <= age_s <= config.measured_max_age_s:
                return (round(float(state["measured_irradiance"]), 1), "measured", "fresh")
        except (ValueError, TypeError):
            pass

    # Priority 2: Open-Meteo
    if outlook and "current_shortwave_radiation_w_m2" in outlook:
        fetched_str = outlook.get("fetched_at")
        quality = "stale"
        if fetched_str:
            try:
                fetched_dt = datetime.fromisoformat(fetched_str.replace("Z", "+00:00"))
                age_s = (now - fetched_dt).total_seconds()
                # Fresh if younger than 2x the refresh interval
                if age_s <= config.forecast_refresh_interval_minutes * 60 * 2:
                    quality = "fresh"
            except (ValueError, TypeError):
                quality = "stale"

        val = max(0.0, float(outlook["current_shortwave_radiation_w_m2"]))
        return (round(val, 1), "open-meteo", quality)

    # Priority 3: Fallback estimate
    clouds = 0.0
    weather = state.get("weather") or {}
    if "clouds" in weather and weather["clouds"] is not None:
        clouds = float(weather["clouds"])
    est = solar_irradiance_estimate(config.plant_lat, config.plant_lon, clouds, now)
    return (est, "cloud-cover-estimate", "fallback")


class WeatherService:
    """Server-side scheduler for weather and irradiance data."""

    def __init__(self, config: AegisConfig, state_updater: Any | None = None) -> None:
        self.config = config
        self.state_updater = state_updater
        self.outlook: dict[str, Any] | None = None
        self.current_weather: dict[str, Any] | None = None
        self.last_forecast_fetch: datetime | None = None
        self.last_current_fetch: datetime | None = None
        self.last_error: str | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def fetch_forecast_now(self) -> dict[str, Any]:
        """Synchronously fetch and update Open-Meteo forecast."""
        raw = fetch_open_meteo_forecast(self.config.plant_lat, self.config.plant_lon)
        parsed = parse_open_meteo_payload(raw)
        with self._lock:
            self.outlook = parsed
            self.last_forecast_fetch = utc_now()
        if self.state_updater:
            self.state_updater(outlook=parsed)
        return parsed

    def fetch_current_now(self) -> dict[str, Any] | None:
        """Synchronously fetch and update OpenWeather current conditions."""
        if not self.config.openweather_api_key:
            return None
        raw = fetch_openweather_current(self.config.plant_lat, self.config.plant_lon, self.config.openweather_api_key)
        normalized = normalize_openweather_current(raw)
        with self._lock:
            self.current_weather = normalized
            self.last_current_fetch = utc_now()
        if self.state_updater:
            self.state_updater(weather=normalized)
        return normalized

    def refresh_all(self) -> None:
        """Perform a cycle of weather and forecast refresh with error handling."""
        now = utc_now()

        # Check if forecast needs refresh
        need_forecast = False
        with self._lock:
            if not self.last_forecast_fetch:
                need_forecast = True
            else:
                elapsed = (now - self.last_forecast_fetch).total_seconds()
                if elapsed >= self.config.forecast_refresh_interval_minutes * 60:
                    need_forecast = True

        if need_forecast:
            try:
                self.fetch_forecast_now()
                self.last_error = None
            except Exception as e:
                self.last_error = f"Open-Meteo error: {e}"
                logger.warning("Forecast fetch failed: %s", e)

        # Check if current weather needs refresh
        need_current = False
        with self._lock:
            if self.config.openweather_api_key:
                if not self.last_current_fetch:
                    need_current = True
                else:
                    elapsed = (now - self.last_current_fetch).total_seconds()
                    if elapsed >= self.config.current_weather_refresh_interval_minutes * 60:
                        need_current = True

        if need_current:
            try:
                self.fetch_current_now()
            except Exception as e:
                logger.warning("Current weather fetch failed: %s", e)

    def start(self) -> None:
        """Start the background scheduler thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AegisWeatherScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background scheduler thread."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run_loop(self) -> None:
        """Background loop executing weather updates with backoff."""
        # Initial fetch
        self.refresh_all()

        while not self._stop_event.is_set():
            # Check every 10 seconds if any interval expired
            if self._stop_event.wait(10.0):
                break
            try:
                self.refresh_all()
            except Exception as e:
                logger.error("Unexpected error in weather scheduler loop: %s", e)
