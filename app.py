"""AEGIS supervisory API for the dashboard and Simulink adapter.

Simulink remains responsible for fast electrical control and safety interlocks.
This service stores telemetry, forecasts supervisory intent, and exposes durable
commands for a Simulink process to poll and acknowledge.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from flask import Flask, Response, jsonify, request

from config import MODES, AegisConfig, load_config
from weather_service import (
    WeatherService,
    fetch_open_meteo_forecast,
    fetch_openweather_current,
    normalize_openweather_current,
    parse_open_meteo_payload,
    resolve_irradiance,
    solar_irradiance_estimate,
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    cfg: AegisConfig = load_config(overrides=test_config)
    app.config.update(cfg.to_dict())
    if test_config:
        app.config.update(test_config)
    app.aegis_config = cfg

    state_defaults: dict[str, Any] = {
        "schema_version": 2,
        "battery_soc": 46.0,
        "h2_level": 68.0,
        "irradiance": 0.0,
        "irradiance_source": "cloud-cover-estimate",
        "data_quality": "fallback",
        "measured_irradiance": None,
        "solar_power_w": None,
        "load_power_w": None,
        "mode": "balanced",
        "applied_mode": None,
        "mode_control": "auto",
        "electrolyzer_control_mode": "auto",
        "electrolyzer_enabled": False,
        "electrolyzer_available": True,
        "location": {"lat": cfg.plant_lat, "lon": cfg.plant_lon},
        "weather": None,
        "forecast": [],
        "outlook_daily": [],
        "recommendation": None,
        "pending_decision": None,
        "alerts": [],
        "last_telemetry_at": None,
        "last_weather_at": None,
        "last_command_id": 0,
        "simulink_connected": False,
    }

    lock = threading.RLock()

    def connect() -> sqlite3.Connection:
        path = app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        connection = sqlite3.connect(path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize_db() -> None:
        with connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS app_state (id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS weather_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS weather_outlook (id INTEGER PRIMARY KEY CHECK (id=1), fetched_at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS telemetry_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, acknowledged_at TEXT)")
            existing = db.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
            if existing is None:
                db.execute("INSERT INTO app_state(id,payload) VALUES(1,?)", (json.dumps(state_defaults),))

    def load_state(db: sqlite3.Connection | None = None) -> dict[str, Any]:
        if db is None:
            with connect() as connection:
                return load_state(connection)
        row = db.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
        state = dict(state_defaults)
        if row:
            state.update(json.loads(row["payload"]))
        return state

    def save_state(state: dict[str, Any], db: sqlite3.Connection | None = None) -> None:
        if db is None:
            with connect() as connection:
                save_state(state, connection)
            return
        db.execute("UPDATE app_state SET payload=? WHERE id=1", (json.dumps(state, separators=(",", ":")),))

    def add_alert(state: dict[str, Any], severity: str, title: str, message: str, key: str | None = None) -> None:
        if key and any(item.get("key") == key for item in state["alerts"]):
            return
        state["alerts"].append({
            "id": str(uuid.uuid4()),
            "severity": severity,
            "title": title,
            "message": message,
            "created_at": iso_now(),
            "key": key,
        })
        state["alerts"] = state["alerts"][-100:]

    def queue_command(db: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> int:
        cursor = db.execute(
            "INSERT INTO commands(kind,payload,status,created_at) VALUES(?,?, 'pending', ?)",
            (kind, json.dumps(payload, separators=(",", ":")), iso_now()),
        )
        return int(cursor.lastrowid)

    def evaluate_electrolyzer(db: sqlite3.Connection, state: dict[str, Any]) -> None:
        if state["electrolyzer_control_mode"] != "auto":
            return
        soc = float(state["battery_soc"])
        desired = state["electrolyzer_enabled"]
        if soc >= 90:
            desired = True
        elif soc <= 88:
            desired = False
        if desired == state["electrolyzer_enabled"]:
            return
        state["electrolyzer_enabled"] = desired
        command_id = queue_command(db, "electrolyzer", {"enabled": desired, "control_mode": "auto", "reason": "battery_soc_threshold", "battery_soc": soc})
        state["last_command_id"] = command_id
        title = "Electrolyzer enabled" if desired else "Electrolyzer disabled"
        add_alert(state, "info", title, f"Automatic control changed state at {soc:.0f}% battery charge.")

    def assess_mode(db: sqlite3.Connection, state: dict[str, Any]) -> None:
        if not app.config.get("AUTO_MODE_CHANGES", True) or state["mode_control"] != "auto" or not state["forecast"]:
            return
        now = utc_now()
        horizon: list[dict[str, Any]] = []
        for item in state["forecast"]:
            try:
                timestamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError):
                continue
            if now <= timestamp <= now + timedelta(hours=6):
                horizon.append(item)
        max_pop = max((float(item.get("pop", 0)) for item in horizon), default=0)
        rain_mm = max((float(item.get("rain_mm_3h", 0)) for item in horizon), default=0)
        current_rain = float((state["weather"] or {}).get("rain_mm_1h", 0))
        target = "eco" if current_rain > 0 or max_pop >= 60 or rain_mm >= 1 else None
        if target is None and state["mode"] == "eco" and max_pop < 30 and current_rain == 0 and state["battery_soc"] >= 60:
            target = "balanced"
        if target is None or target == state["mode"]:
            pending = state.get("pending_decision")
            if pending and pending.get("status") == "notice_period":
                pending["status"] = "cancelled"
            state["recommendation"] = {"mode": state["mode"], "reason": "No forecast trigger for a mode change.", "confidence": "low"}
            return

        reason = f"Rain risk in the next 6 hours: {max_pop:.0f}% precipitation probability; {rain_mm:.1f} mm forecast."
        state["recommendation"] = {"mode": target, "reason": reason, "confidence": "medium"}
        pending = state.get("pending_decision")
        notice_seconds = app.config.get("MODE_NOTICE_SECONDS", 45)
        if pending and pending.get("status") == "notice_period" and pending.get("target_mode") == target:
            deadline = datetime.fromisoformat(pending["apply_after"])
            if now >= deadline:
                command_id = queue_command(db, "mode", {"mode": target, "source": "weather_policy", "reason": reason})
                state["mode"] = target
                state["last_command_id"] = command_id
                pending["status"] = "command_queued"
                pending["command_id"] = command_id
            return
        if pending and pending.get("status") == "notice_period":
            pending["status"] = "cancelled"
        decision_id = str(uuid.uuid4())
        apply_after = (now + timedelta(seconds=notice_seconds)).isoformat()
        state["pending_decision"] = {"id": decision_id, "target_mode": target, "reason": reason, "status": "notice_period", "created_at": now.isoformat(), "apply_after": apply_after}
        add_alert(state, "warn", f"Mode change planned: {target.title()}", f"{reason} Planned in {notice_seconds} seconds unless conditions change.", key=f"mode:{decision_id}")

    def update_from_weather_service(weather: dict[str, Any] | None = None, outlook: dict[str, Any] | None = None) -> None:
        with lock, connect() as db:
            state = load_state(db)
            if weather:
                state["weather"] = weather
                state["last_weather_at"] = iso_now()
            if outlook:
                db.execute(
                    "INSERT OR REPLACE INTO weather_outlook(id, fetched_at, payload) VALUES(1, ?, ?)",
                    (outlook["fetched_at"], json.dumps(outlook)),
                )
                state["outlook_daily"] = outlook.get("daily", [])
                hourly = outlook.get("hourly") or []
                if hourly:
                    state["forecast"] = [
                        {
                            "timestamp": item.get("time"),
                            "temp": None,
                            "pop": round(float(item.get("precipitation_probability_pct", 0))),
                            "clouds": round(float(item.get("cloud_cover_pct", 0))),
                            "rain_mm_3h": 0,
                            "wind_kmh": 0,
                        }
                        for item in hourly[:40]
                    ]
            irr, source, quality = resolve_irradiance(state, outlook, cfg, utc_now())
            state["irradiance"] = irr
            state["irradiance_source"] = source
            state["data_quality"] = quality
            save_state(state, db)

    def state_response(db: sqlite3.Connection) -> dict[str, Any]:
        state = load_state(db)
        state["weather_provider_configured"] = bool(app.config.get("OPENWEATHER_API_KEY"))

        last_telem = state.get("last_telemetry_at")
        if last_telem:
            try:
                telem_dt = datetime.fromisoformat(last_telem.replace("Z", "+00:00"))
                age_s = (utc_now() - telem_dt).total_seconds()
                state["simulink_connected"] = (age_s <= cfg.telemetry_stale_seconds)
            except Exception:
                state["simulink_connected"] = False
        else:
            state["simulink_connected"] = False

        outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
        outlook = json.loads(outlook_row["payload"]) if outlook_row else None
        irr, source, quality = resolve_irradiance(state, outlook, cfg, utc_now())
        state["irradiance"] = irr
        state["irradiance_source"] = source
        state["data_quality"] = quality
        if outlook and "daily" in outlook:
            state["outlook_daily"] = outlook["daily"]

        assess_mode(db, state)
        evaluate_electrolyzer(db, state)
        save_state(state, db)
        pending_commands = db.execute("SELECT COUNT(*) AS count FROM commands WHERE status='pending'").fetchone()["count"]
        state["pending_command_count"] = pending_commands
        return state

    @app.after_request
    def cors(response):
        allowed_origins = app.config.get("CORS_ORIGINS", ["*"])
        req_origin = request.headers.get("Origin")
        if "*" in allowed_origins or not allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif req_origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = req_origin
        else:
            response.headers["Access-Control-Allow-Origin"] = allowed_origins[0]

        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Device-Id, X-AEGIS-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/<path:path>", methods=["OPTIONS"])
    def options(path: str):
        return "", 204

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "aegis-supervisor",
            "api_version": 2,
            "weather_provider_configured": bool(app.config.get("OPENWEATHER_API_KEY")),
            "timestamp": iso_now(),
        })

    @app.get("/api/simulink/inputs")
    def simulink_inputs():
        with lock, connect() as db:
            state = load_state(db)
            outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
            outlook = json.loads(outlook_row["payload"]) if outlook_row else None
            irr, source, quality = resolve_irradiance(state, outlook, cfg, utc_now())
            return jsonify({
                "irradiance_w_m2": irr,
                "irradiance_source": source,
                "quality": quality,
                "timestamp": iso_now(),
                "valid_for_s": 60,
            })

    @app.get("/api/weather/provider")
    def weather_provider():
        api_key = app.config.get("OPENWEATHER_API_KEY")
        if not api_key:
            return jsonify({"error": "Weather provider is not configured. Set OPENWEATHER_API_KEY in the Flask server environment."}), 503
        try:
            latitude = finite_number(float(request.args.get("lat", "nan")), "lat", -90, 90)
            longitude = finite_number(float(request.args.get("lon", "nan")), "lon", -180, 180)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        query = urlencode({"lat": latitude, "lon": longitude, "units": "metric", "appid": api_key})

        def fetch_json(endpoint: str) -> dict[str, Any]:
            url = f"https://api.openweathermap.org/data/2.5/{endpoint}?{query}"
            with urlopen(url, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            current, forecast_data = fetch_json("weather"), fetch_json("forecast")
        except HTTPError as error:
            return jsonify({"error": "Weather provider rejected the request", "provider_status": error.code}), 502
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            return jsonify({"error": "Weather provider is temporarily unavailable", "detail": str(error)}), 502

        current_weather = current.get("weather") or [{}]
        current_main = current.get("main") or {}
        current_wind = current.get("wind") or {}
        current_rain = current.get("rain") or {}
        current_clouds = (current.get("clouds") or {}).get("all", 0)
        normalized_weather = {
            "city": current.get("name"),
            "temp": current_main.get("temp"),
            "feels_like": current_main.get("feels_like"),
            "temp_max": current_main.get("temp_max"),
            "temp_min": current_main.get("temp_min"),
            "humidity": current_main.get("humidity"),
            "pressure_hpa": current_main.get("pressure"),
            "wind_kmh": float(current_wind.get("speed", 0)) * 3.6,
            "wind_deg": current_wind.get("deg", 0),
            "clouds": current_clouds,
            "rain_mm_1h": current_rain.get("1h", 0),
            "description": current_weather[0].get("description", "Unknown").capitalize(),
        }
        forecast = []
        for item in (forecast_data.get("list") or [])[:40]:
            main = item.get("main") or {}
            forecast_wind = item.get("wind") or {}
            forecast_rain = item.get("rain") or {}
            forecast.append({
                "timestamp": datetime.fromtimestamp(item["dt"], timezone.utc).isoformat(),
                "temp": main.get("temp"),
                "pop": round(float(item.get("pop", 0)) * 100),
                "clouds": (item.get("clouds") or {}).get("all", current_clouds),
                "rain_mm_3h": forecast_rain.get("3h", 0),
                "wind_kmh": float(forecast_wind.get("speed", 0)) * 3.6,
            })
        return jsonify({"lat": latitude, "lon": longitude, "weather": normalized_weather, "forecast": forecast})

    @app.get("/api/weather/tiles/<layer>/<int:zoom>/<int:x>/<int:y>.png")
    def weather_tile(layer: str, zoom: int, x: int, y: int):
        api_key = app.config.get("OPENWEATHER_API_KEY")
        tile_layers = {"rain": "precipitation_new", "wind": "wind_new"}
        if not api_key:
            return jsonify({"error": "Weather provider is not configured"}), 503
        if layer not in tile_layers or not 0 <= zoom <= 18 or not 0 <= x < 2**zoom or not 0 <= y < 2**zoom:
            return jsonify({"error": "invalid weather tile request"}), 404
        url = f"https://tile.openweathermap.org/map/{tile_layers[layer]}/{zoom}/{x}/{y}.png?{urlencode({'appid': api_key})}"
        try:
            with urlopen(url, timeout=8) as upstream:
                image = upstream.read()
                return Response(image, mimetype="image/png", headers={"Cache-Control": "public, max-age=300"})
        except HTTPError as error:
            return jsonify({"error": "Weather tile provider rejected the request", "provider_status": error.code}), 502
        except (URLError, TimeoutError) as error:
            return jsonify({"error": "Weather tile provider is unavailable", "detail": str(error)}), 502

    @app.get("/api/state")
    def get_state():
        with lock, connect() as db:
            return jsonify(state_response(db))

    @app.post("/api/weather")
    def ingest_weather():
        body = request.get_json(silent=True) or {}
        weather = body.get("weather")
        if not isinstance(weather, dict):
            return jsonify({"error": "weather must be an object"}), 400
        try:
            latitude = finite_number(body.get("lat", cfg.plant_lat), "lat", -90, 90)
            longitude = finite_number(body.get("lon", cfg.plant_lon), "lon", -180, 180)
            clouds = finite_number(weather.get("clouds", 0), "weather.clouds", 0, 100)
            clean_weather = {
                key: value for key, value in weather.items()
                if key in {"city", "temp", "feels_like", "temp_max", "temp_min", "humidity", "wind_kmh", "wind_deg", "clouds", "rain_mm_1h", "description", "pressure_hpa"}
            }
            for key, bounds in {
                "temp": (-90, 65), "feels_like": (-100, 70), "temp_max": (-90, 70),
                "temp_min": (-100, 65), "humidity": (0, 100), "wind_kmh": (0, 400),
                "wind_deg": (0, 360), "rain_mm_1h": (0, 1000), "pressure_hpa": (100, 1200),
            }.items():
                if key in clean_weather:
                    clean_weather[key] = finite_number(clean_weather[key], f"weather.{key}", *bounds)
            if "city" in clean_weather and not isinstance(clean_weather["city"], str):
                raise ValueError("weather.city must be a string")
            if "description" in clean_weather and not isinstance(clean_weather["description"], str):
                raise ValueError("weather.description must be a string")
            forecast_input = body.get("forecast", [])
            if not isinstance(forecast_input, list) or len(forecast_input) > 60:
                raise ValueError("forecast must be a list of at most 60 entries")
            forecast = []
            for entry in forecast_input:
                if not isinstance(entry, dict) or not isinstance(entry.get("timestamp"), str):
                    continue
                forecast.append({
                    "timestamp": entry["timestamp"],
                    "temp": finite_number(entry.get("temp", 0), "forecast.temp", -100, 70),
                    "pop": finite_number(entry.get("pop", 0), "forecast.pop", 0, 100),
                    "clouds": finite_number(entry.get("clouds", clouds), "forecast.clouds", 0, 100),
                    "rain_mm_3h": finite_number(entry.get("rain_mm_3h", 0), "forecast.rain_mm_3h", 0, 1000),
                    "wind_kmh": finite_number(entry.get("wind_kmh", 0), "forecast.wind_kmh", 0, 400),
                })
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        timestamp = utc_now()
        snapshot = {"source": body.get("source", "openweather-client"), "lat": latitude, "lon": longitude, "weather": clean_weather, "forecast": forecast, "received_at": timestamp.isoformat()}
        with lock, connect() as db:
            state = load_state(db)
            state["weather"] = clean_weather
            state["forecast"] = forecast
            state["last_weather_at"] = timestamp.isoformat()
            if app.config.get("TESTING"):
                state["location"] = {"lat": latitude, "lon": longitude}

            outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
            outlook = json.loads(outlook_row["payload"]) if outlook_row else None
            irradiance, source, quality = resolve_irradiance(state, outlook, cfg, timestamp)
            state["irradiance"] = irradiance
            state["irradiance_source"] = source
            state["data_quality"] = quality

            db.execute("INSERT INTO weather_samples(received_at,payload) VALUES(?,?)", (timestamp.isoformat(), json.dumps(snapshot)))
            assess_mode(db, state)
            evaluate_electrolyzer(db, state)
            save_state(state, db)
            return jsonify({
                "accepted": True,
                "irradiance_w_m2": irradiance,
                "irradiance_source": state["irradiance_source"],
                "quality": state["data_quality"],
                "recommendation": state["recommendation"],
                "pending_decision": state["pending_decision"],
                "alerts": state["alerts"],
            })

    def ingest_telemetry(body: dict[str, Any], source: str):
        telemetry = body.get("telemetry", body)
        if not isinstance(telemetry, dict):
            return jsonify({"error": "telemetry must be an object"}), 400
        fields = {
            "battery_soc": (0, 100), "h2_level": (0, 100), "irradiance_w_m2": (0, 1600),
            "solar_power_w": (0, 1e9), "load_power_w": (0, 1e9),
        }
        try:
            values = {key: finite_number(telemetry[key], key, *bounds) for key, bounds in fields.items() if key in telemetry}
            if not values:
                raise ValueError("telemetry must include at least one supported measurement")
            if "operating_mode" in telemetry and telemetry["operating_mode"] not in MODES:
                raise ValueError("operating_mode must be eco, balanced, or boost")
            if "electrolyzer_enabled" in telemetry and not isinstance(telemetry["electrolyzer_enabled"], bool):
                raise ValueError("electrolyzer_enabled must be boolean")
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        timestamp = iso_now()
        clean = {**values}
        for key in ("operating_mode", "electrolyzer_enabled", "device_id", "sequence"):
            if key in telemetry:
                clean[key] = telemetry[key]

        with lock, connect() as db:
            state = load_state(db)
            if "battery_soc" in values:
                state["battery_soc"] = values["battery_soc"]
            if "h2_level" in values:
                state["h2_level"] = values["h2_level"]
            if "irradiance_w_m2" in values:
                state["irradiance"] = values["irradiance_w_m2"]
                if source == "simulink":
                    state["measured_irradiance"] = values["irradiance_w_m2"]
                state["irradiance_source"] = "measured" if source == "simulink" else source
            for key in ("solar_power_w", "load_power_w"):
                if key in values:
                    state[key] = values[key]
            if "operating_mode" in clean:
                state["applied_mode"] = clean["operating_mode"]
            if "electrolyzer_enabled" in clean:
                state["electrolyzer_available"] = True
                if state["electrolyzer_control_mode"] == "manual":
                    state["electrolyzer_enabled"] = clean["electrolyzer_enabled"]
            state["last_telemetry_at"] = timestamp
            state["simulink_connected"] = True

            outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
            outlook = json.loads(outlook_row["payload"]) if outlook_row else None
            irr, src, qual = resolve_irradiance(state, outlook, cfg, utc_now())
            state["irradiance"] = irr
            state["irradiance_source"] = src
            state["data_quality"] = qual

            db.execute("INSERT INTO telemetry_samples(received_at,source,payload) VALUES(?,?,?)", (timestamp, source, json.dumps(clean)))
            evaluate_electrolyzer(db, state)
            assess_mode(db, state)
            save_state(state, db)
            return jsonify({"accepted": True, "source": source, "state": state})

    @app.post("/api/telemetry")
    def telemetry():
        return ingest_telemetry(request.get_json(silent=True) or {}, request.headers.get("X-Device-Id", "dashboard"))

    @app.post("/api/simulink/telemetry")
    def simulink_telemetry():
        return ingest_telemetry(request.get_json(silent=True) or {}, request.headers.get("X-Device-Id", "simulink"))

    @app.post("/api/mode")
    def set_mode():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in MODES:
            return jsonify({"error": "mode must be eco, balanced, or boost"}), 400
        with lock, connect() as db:
            state = load_state(db)
            old_decision = state.get("pending_decision")
            if old_decision and old_decision.get("status") == "notice_period":
                old_decision["status"] = "cancelled_by_operator"
            state["mode_control"] = "manual"
            state["mode"] = mode
            state["recommendation"] = {"mode": mode, "reason": "Operator selected this mode.", "confidence": "operator"}
            command_id = queue_command(db, "mode", {"mode": mode, "source": "dashboard", "operator": body.get("operator", "user")})
            state["last_command_id"] = command_id
            save_state(state, db)
            return jsonify({"accepted": True, "command_id": command_id, "state": state})

    @app.post("/api/mode/auto")
    def set_mode_auto():
        with lock, connect() as db:
            state = load_state(db)
            state["mode_control"] = "auto"
            assess_mode(db, state)
            save_state(state, db)
            return jsonify({"accepted": True, "state": state})

    @app.post("/api/electrolyzer")
    def set_electrolyzer():
        body = request.get_json(silent=True) or {}
        control_mode = body.get("control_mode", "manual")
        if control_mode not in {"auto", "manual"}:
            return jsonify({"error": "control_mode must be auto or manual"}), 400
        if control_mode == "manual" and not isinstance(body.get("enabled"), bool):
            return jsonify({"error": "manual control requires a boolean enabled field"}), 400
        with lock, connect() as db:
            state = load_state(db)
            previous = state["electrolyzer_enabled"]
            state["electrolyzer_control_mode"] = control_mode
            if control_mode == "manual":
                state["electrolyzer_enabled"] = body["enabled"]
            else:
                state["electrolyzer_enabled"] = state["battery_soc"] >= 90
            if state["electrolyzer_enabled"] != previous or control_mode == "manual":
                reason = "operator_override" if control_mode == "manual" else "automatic_soc_rule"
                command_id = queue_command(db, "electrolyzer", {"enabled": state["electrolyzer_enabled"], "control_mode": control_mode, "reason": reason})
                state["last_command_id"] = command_id
            save_state(state, db)
            return jsonify({"accepted": True, "state": state})

    @app.post("/api/decisions/<decision_id>/cancel")
    def cancel_decision(decision_id: str):
        with lock, connect() as db:
            state = load_state(db)
            decision = state.get("pending_decision")
            if not decision or decision.get("id") != decision_id or decision.get("status") != "notice_period":
                return jsonify({"error": "pending decision not found"}), 404
            decision["status"] = "cancelled_by_operator"
            state["mode_control"] = "manual"
            add_alert(state, "info", "Planned mode change cancelled", "The current mode will remain active.")
            save_state(state, db)
            return jsonify({"accepted": True, "state": state})

    @app.get("/api/simulink/commands")
    def simulink_commands():
        after = request.args.get("after", default=0, type=int)
        with lock, connect() as db:
            state = load_state(db)
            assess_mode(db, state)
            evaluate_electrolyzer(db, state)
            save_state(state, db)
            rows = db.execute("SELECT id,kind,payload,status,created_at FROM commands WHERE status='pending' AND id>? ORDER BY id LIMIT 100", (max(after, 0),)).fetchall()
            return jsonify({"commands": [{"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]), "status": row["status"], "created_at": row["created_at"]} for row in rows]})

    @app.post("/api/simulink/commands/<int:command_id>/ack")
    def acknowledge_command(command_id: int):
        body = request.get_json(silent=True) or {}
        status = body.get("status", "applied")
        if status not in {"applied", "rejected", "failed"}:
            return jsonify({"error": "status must be applied, rejected, or failed"}), 400
        with connect() as db:
            cursor = db.execute("UPDATE commands SET status=?,acknowledged_at=? WHERE id=? AND status='pending'", (status, iso_now(), command_id))
            if cursor.rowcount == 0:
                return jsonify({"error": "pending command not found"}), 404
            return jsonify({"accepted": True, "command_id": command_id, "status": status})

    @app.get("/api/history/<kind>")
    def history(kind: str):
        limit = min(max(request.args.get("limit", default=100, type=int), 1), 500)
        with connect() as db:
            if kind == "weather":
                rows = db.execute("SELECT id,received_at,payload FROM weather_samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                values = [{"id": row["id"], "received_at": row["received_at"], **json.loads(row["payload"])} for row in rows]
            elif kind == "telemetry":
                rows = db.execute("SELECT id,received_at,source,payload FROM telemetry_samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                values = [{"id": row["id"], "received_at": row["received_at"], "source": row["source"], "telemetry": json.loads(row["payload"])} for row in rows]
            elif kind == "commands":
                rows = db.execute("SELECT id,kind,payload,status,created_at,acknowledged_at FROM commands ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                values = [{"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]), "status": row["status"], "created_at": row["created_at"], "acknowledged_at": row["acknowledged_at"]} for row in rows]
            else:
                return jsonify({"error": "kind must be weather, telemetry, or commands"}), 400
            return jsonify({"items": values})

    initialize_db()

    weather_svc = WeatherService(cfg, state_updater=update_from_weather_service)
    app.weather_service = weather_svc
    if not app.config.get("TESTING") and app.config.get("SCHEDULER_ENABLED", True):
        weather_svc.start()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=os.environ.get("AEGIS_HOST", "127.0.0.1"), port=int(os.environ.get("AEGIS_PORT", "5000")), debug=False)