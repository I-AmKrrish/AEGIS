"""AEGIS configuration module.

Single source of truth for all operational modes, setpoints, physical constants,
decision policy parameters, timings, and environment variable overrides.
No magic numbers in application logic.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


def validate_number(
    value: Any,
    name: str,
    minimum: float | None = None,
    maximum: float | None = None,
    must_be_int: bool = False,
) -> float | int:
    """Validate a numeric value, rejecting booleans, NaN, Inf, and out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Config parameter '{name}' must be a number, got {type(value).__name__}")
    
    val_float = float(value)
    if math.isnan(val_float) or math.isinf(val_float):
        raise ValueError(f"Config parameter '{name}' cannot be NaN or Infinite")
    
    if must_be_int and not val_float.is_integer():
        raise ValueError(f"Config parameter '{name}' must be an integer")

    result = int(val_float) if must_be_int else val_float

    if minimum is not None and result < minimum:
        raise ValueError(f"Config parameter '{name}'={result} is below minimum allowed {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"Config parameter '{name}'={result} is above maximum allowed {maximum}")

    return result


@dataclass(frozen=True)
class ModeSpec:
    """Definition and setpoints for an operating mode."""

    name: str
    meaning: str
    ui_description: str
    load_limit_ratio: float | None  # None indicates dynamic computation (e.g. in boost)
    electrolyzer_start_soc: float
    electrolyzer_stop_soc: float
    hydrogen_priority: bool
    fuel_cell_night_use: str  # e.g., 'only_if_battery_low' or 'allowed_at_night'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Canonical modes table: Single source of truth for Flask, Simulink payloads, and UI.
MODES: dict[str, ModeSpec] = {
    "balanced": ModeSpec(
        name="balanced",
        meaning="Normal days: solar by day, battery by night",
        ui_description="Share load between solar, battery and hydrogen",
        load_limit_ratio=1.0,
        electrolyzer_start_soc=90.0,
        electrolyzer_stop_soc=88.0,
        hydrogen_priority=False,
        fuel_cell_night_use="only_if_battery_low",
    ),
    "eco": ModeSpec(
        name="eco",
        meaning="Mild caution (moderate bad weather, tank adequate)",
        ui_description="Save hydrogen, charge the battery first",
        load_limit_ratio=0.85,
        electrolyzer_start_soc=90.0,
        electrolyzer_stop_soc=88.0,
        hydrogen_priority=False,
        fuel_cell_night_use="only_if_battery_low",
    ),
    "boost": ModeSpec(
        name="boost",
        meaning="Prolonged bad weather: reserve power, fill the hydrogen tank",
        ui_description="Boost hydrogen reserves with reduced load",
        load_limit_ratio=None,  # Dynamic calculation based on solar deficit and stored energy
        electrolyzer_start_soc=75.0,
        electrolyzer_stop_soc=73.0,
        hydrogen_priority=True,
        fuel_cell_night_use="allowed_at_night",
    ),
}


@dataclass
class AegisConfig:
    """Consolidated configuration class for AEGIS."""

    # 1. Plant physical specifications
    panel_area_m2: float = 1.0
    panel_efficiency: float = 0.20
    performance_ratio: float = 0.80  # Conservative forecast degradation factor
    load_nominal_w: float = 100.0
    battery_hours: float = 18.0  # Hours of backup at nominal load
    h2_tank_wh: float = 2400.0  # Deliverable electrical energy at 100% tank
    plant_lat: float = 13.1007  # Yelahanka default
    plant_lon: float = 77.5963
    plant_city: str = "Yelahanka"

    # 2. Decision policy & energy forecast
    lookahead_days: int = 10
    bad_days_min: int = 2
    low_sun_ratio: float = 0.60  # Ratio of nominal load below which a day is low-sun
    min_load_ratio: float = 0.40  # Minimum load ratio clamp for boost mode
    decline_cooldown_hours: float = 6.0
    proposal_ttl_minutes: float = 60.0
    h2_boost_threshold_pct: float = 98.0  # Propose Boost only if tank < this %
    h2_return_target_pct: float = 90.0  # Target tank % to propose return to Balanced
    eco_rain_prob_pct: float = 70.0  # Rain probability threshold for Eco proposal

    # 3. Timers and refresh intervals
    forecast_refresh_interval_minutes: float = 60.0
    current_weather_refresh_interval_minutes: float = 10.0
    telemetry_stale_seconds: float = 15.0  # Max age before simulink_connected becomes false
    measured_max_age_s: float = 30.0  # Max age for measured irradiance from telemetry
    command_ttl_seconds: float = 300.0  # Expiry duration for queued commands

    # 4. Networking, host, database & security
    host: str = "127.0.0.1"
    port: int = 5000
    database_path: str = "data/aegis.sqlite3"
    aegis_token: str = ""  # Master shared secret for X-AEGIS-Token
    device_token: str = ""  # Token for Simulink adapter endpoints
    operator_token: str = ""  # Token for Dashboard operator POST actions
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    openweather_api_key: str = ""
    max_content_length: int = 256 * 1024

    # 5. Operational modes specification
    modes: dict[str, ModeSpec] = field(default_factory=lambda: dict(MODES))

    @property
    def battery_wh(self) -> float:
        """Derived battery capacity in Watt-hours."""
        return self.load_nominal_w * self.battery_hours

    def validate(self) -> None:
        """Ensure all fields satisfy operational boundaries and type constraints."""
        validate_number(self.panel_area_m2, "panel_area_m2", minimum=0.01, maximum=1000.0)
        validate_number(self.panel_efficiency, "panel_efficiency", minimum=0.01, maximum=1.0)
        validate_number(self.performance_ratio, "performance_ratio", minimum=0.1, maximum=1.0)
        validate_number(self.load_nominal_w, "load_nominal_w", minimum=1.0, maximum=1e6)
        validate_number(self.battery_hours, "battery_hours", minimum=0.1, maximum=1000.0)
        validate_number(self.h2_tank_wh, "h2_tank_wh", minimum=10.0, maximum=1e9)
        validate_number(self.plant_lat, "plant_lat", minimum=-90.0, maximum=90.0)
        validate_number(self.plant_lon, "plant_lon", minimum=-180.0, maximum=180.0)

        validate_number(self.lookahead_days, "lookahead_days", minimum=1, maximum=30, must_be_int=True)
        validate_number(self.bad_days_min, "bad_days_min", minimum=1, maximum=14, must_be_int=True)
        validate_number(self.low_sun_ratio, "low_sun_ratio", minimum=0.05, maximum=1.0)
        validate_number(self.min_load_ratio, "min_load_ratio", minimum=0.05, maximum=1.0)
        validate_number(self.decline_cooldown_hours, "decline_cooldown_hours", minimum=0.1, maximum=168.0)
        validate_number(self.proposal_ttl_minutes, "proposal_ttl_minutes", minimum=1.0, maximum=1440.0)
        validate_number(self.h2_boost_threshold_pct, "h2_boost_threshold_pct", minimum=10.0, maximum=100.0)
        validate_number(self.h2_return_target_pct, "h2_return_target_pct", minimum=10.0, maximum=100.0)
        validate_number(self.eco_rain_prob_pct, "eco_rain_prob_pct", minimum=0.0, maximum=100.0)

        validate_number(self.forecast_refresh_interval_minutes, "forecast_refresh_interval_minutes", minimum=1.0, maximum=1440.0)
        validate_number(self.current_weather_refresh_interval_minutes, "current_weather_refresh_interval_minutes", minimum=1.0, maximum=360.0)
        validate_number(self.telemetry_stale_seconds, "telemetry_stale_seconds", minimum=1.0, maximum=3600.0)
        validate_number(self.measured_max_age_s, "measured_max_age_s", minimum=1.0, maximum=3600.0)
        validate_number(self.command_ttl_seconds, "command_ttl_seconds", minimum=5.0, maximum=86400.0)

        validate_number(self.port, "port", minimum=1, maximum=65535, must_be_int=True)
        validate_number(self.max_content_length, "max_content_length", minimum=1024, maximum=100 * 1024 * 1024, must_be_int=True)

        if not isinstance(self.plant_city, str) or not self.plant_city.strip():
            raise ValueError("Config parameter 'plant_city' must be a non-empty string")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("Config parameter 'host' must be a non-empty string")
        if not isinstance(self.database_path, str) or not self.database_path.strip():
            raise ValueError("Config parameter 'database_path' must be a non-empty string")
        if not isinstance(self.cors_origins, list):
            raise ValueError("Config parameter 'cors_origins' must be a list of strings")

    def to_dict(self) -> dict[str, Any]:
        """Convert config attributes to a dict compatible with Flask app.config."""
        d = asdict(self)
        d["battery_wh"] = self.battery_wh
        d["MODES"] = {k: v.to_dict() for k, v in self.modes.items()}
        # Add legacy keys for backwards-compatibility with app.config
        d["DATABASE"] = self.database_path
        d["OPENWEATHER_API_KEY"] = self.openweather_api_key
        d["MAX_CONTENT_LENGTH"] = self.max_content_length
        return d


def load_config(
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AegisConfig:
    """Instantiate AegisConfig by loading defaults, applying environment variables, and applying overrides."""
    if env is None:
        env = os.environ

    cfg = AegisConfig()

    # Environment variable overrides
    if "AEGIS_DB" in env:
        cfg.database_path = env["AEGIS_DB"]
    elif "DATABASE" in env:
        cfg.database_path = env["DATABASE"]

    if "OPENWEATHER_API_KEY" in env:
        cfg.openweather_api_key = env["OPENWEATHER_API_KEY"]

    if "AEGIS_HOST" in env:
        cfg.host = env["AEGIS_HOST"]
    if "AEGIS_PORT" in env:
        cfg.port = int(env["AEGIS_PORT"])

    if "AEGIS_TOKEN" in env:
        cfg.aegis_token = env["AEGIS_TOKEN"]
    if "AEGIS_DEVICE_TOKEN" in env:
        cfg.device_token = env["AEGIS_DEVICE_TOKEN"]
    if "AEGIS_OPERATOR_TOKEN" in env:
        cfg.operator_token = env["AEGIS_OPERATOR_TOKEN"]

    if "AEGIS_CORS_ORIGINS" in env:
        cfg.cors_origins = [o.strip() for o in env["AEGIS_CORS_ORIGINS"].split(",") if o.strip()]

    # Numeric environment variables
    env_num_mappings: dict[str, tuple[str, bool]] = {
        "AEGIS_PANEL_AREA": ("panel_area_m2", False),
        "AEGIS_PANEL_EFFICIENCY": ("panel_efficiency", False),
        "AEGIS_PERFORMANCE_RATIO": ("performance_ratio", False),
        "AEGIS_LOAD_NOMINAL_W": ("load_nominal_w", False),
        "AEGIS_BATTERY_HOURS": ("battery_hours", False),
        "AEGIS_H2_TANK_WH": ("h2_tank_wh", False),
        "AEGIS_PLANT_LAT": ("plant_lat", False),
        "AEGIS_PLANT_LON": ("plant_lon", False),
        "AEGIS_LOOKAHEAD_DAYS": ("lookahead_days", True),
        "AEGIS_BAD_DAYS_MIN": ("bad_days_min", True),
        "AEGIS_LOW_SUN_RATIO": ("low_sun_ratio", False),
        "AEGIS_MIN_LOAD_RATIO": ("min_load_ratio", False),
        "AEGIS_DECLINE_COOLDOWN_HOURS": ("decline_cooldown_hours", False),
        "AEGIS_PROPOSAL_TTL_MINUTES": ("proposal_ttl_minutes", False),
        "AEGIS_H2_BOOST_THRESHOLD_PCT": ("h2_boost_threshold_pct", False),
        "AEGIS_H2_RETURN_TARGET_PCT": ("h2_return_target_pct", False),
        "AEGIS_ECO_RAIN_PROB_PCT": ("eco_rain_prob_pct", False),
        "AEGIS_TELEMETRY_STALE_SECONDS": ("telemetry_stale_seconds", False),
        "AEGIS_MEASURED_MAX_AGE_S": ("measured_max_age_s", False),
        "AEGIS_COMMAND_TTL_SECONDS": ("command_ttl_seconds", False),
    }

    for env_var, (attr_name, is_int) in env_num_mappings.items():
        if env_var in env:
            raw = env[env_var]
            try:
                val = float(raw)
            except ValueError:
                raise ValueError(f"Environment variable '{env_var}' must be a valid number, got {raw!r}")
            setattr(cfg, attr_name, int(val) if is_int else val)

    if "AEGIS_PLANT_CITY" in env:
        cfg.plant_city = env["AEGIS_PLANT_CITY"]

    # Explicit programmatic overrides (e.g. from test_config)
    if overrides:
        for k, v in overrides.items():
            norm_key = k.lower()
            if hasattr(cfg, norm_key):
                setattr(cfg, norm_key, v)
            elif k == "DATABASE":
                cfg.database_path = str(v)
            elif k == "OPENWEATHER_API_KEY":
                cfg.openweather_api_key = str(v)
            elif k == "MAX_CONTENT_LENGTH":
                cfg.max_content_length = int(v)

    cfg.validate()
    return cfg
