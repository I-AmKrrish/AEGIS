"""AEGIS supervisory API for the dashboard and Simulink adapter.

Simulink remains responsible for fast electrical control and safety interlocks.
This service stores telemetry, forecasts supervisory intent, and exposes durable
commands for a Simulink process to poll and acknowledge upon user approval.
"""

from __future__ import annotations

import json
import logging
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
from policy import (
    Proposal,
    evaluate_policy,
    get_mode_parameters,
)
from weather_service import (
    WeatherService,
    fetch_open_meteo_forecast,
    fetch_openweather_current,
    normalize_openweather_current,
    parse_open_meteo_payload,
    resolve_irradiance,
    solar_irradiance_estimate,
)

logger = logging.getLogger("aegis")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


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
        "requested_mode": "balanced",
        "mode": "balanced",  # Backward-compatible alias for requested_mode
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
        "active_setpoints": get_mode_parameters("balanced", None, cfg),
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
            db.execute("CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, expires_at TEXT, acknowledged_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, target_mode TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, responded_at TEXT)")

            # Lightweight migration: ensure expires_at exists in commands
            cmd_cols = [row[1] for row in db.execute("PRAGMA table_info(commands)").fetchall()]
            if "expires_at" not in cmd_cols:
                db.execute("ALTER TABLE commands ADD COLUMN expires_at TEXT")

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

    def queue_command(
        db: sqlite3.Connection,
        kind: str,
        payload: dict[str, Any],
        expires_in_seconds: float | None = None,
    ) -> int:
        now = utc_now()
        ttl = expires_in_seconds if expires_in_seconds is not None else cfg.command_ttl_seconds
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        cursor = db.execute(
            "INSERT INTO commands(kind,payload,status,created_at,expires_at) VALUES(?,?, 'pending', ?, ?)",
            (kind, json.dumps(payload, separators=(",", ":")), now.isoformat(), expires_at),
        )
        cmd_id = int(cursor.lastrowid)
        logger.info("[AUDIT] Command %d queued: kind=%s, expires_at=%s", cmd_id, kind, expires_at)
        return cmd_id

    def run_policy_assessment(db: sqlite3.Connection, state: dict[str, Any]) -> None:
        """Run policy engine pure function evaluation and update proposals / lifecycle."""
        outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
        outlook = json.loads(outlook_row["payload"]) if outlook_row else None
        daily_outlook = outlook.get("daily", []) if outlook else []

        active_proposal = state.get("pending_decision")

        # Get last declined decision for decline cooldown rule
        last_declined_row = db.execute(
            "SELECT target_mode, payload, responded_at FROM decisions WHERE status='declined' ORDER BY responded_at DESC LIMIT 1"
        ).fetchone()
        last_declined = None
        if last_declined_row:
            dec_payload = json.loads(last_declined_row["payload"])
            bad_days = 0
            for r in dec_payload.get("reasons", []):
                if "consecutive" in r:
                    parts = r.split()
                    if parts and parts[0].isdigit():
                        bad_days = int(parts[0])
                        break
            last_declined = {
                "target_mode": last_declined_row["target_mode"],
                "declined_at": last_declined_row["responded_at"],
                "bad_days": bad_days,
                "h2_level": state.get("h2_level", 100.0),
            }

        curr_mode = state.get("requested_mode", state.get("mode", "balanced"))
        eval_result = evaluate_policy(
            current_mode=curr_mode,
            mode_control=state.get("mode_control", "auto"),
            battery_soc=state.get("battery_soc"),
            h2_level=state.get("h2_level"),
            daily_outlook=daily_outlook,
            current_weather=state.get("weather"),
            config=cfg,
            active_proposal=active_proposal,
            last_declined=last_declined,
            now=utc_now(),
        )

        action = eval_result["action"]
        if action == "create":
            proposal: Proposal = eval_result["proposal"]
            db.execute(
                "INSERT OR REPLACE INTO decisions(id, target_mode, payload, status, created_at, expires_at) VALUES(?, ?, ?, 'pending', ?, ?)",
                (proposal.id, proposal.target_mode, json.dumps(proposal.to_dict()), proposal.created_at, proposal.expires_at),
            )
            state["pending_decision"] = proposal.to_dict()
            add_alert(state, "warn", proposal.title, proposal.question, key=f"decision:{proposal.id}")
            state["recommendation"] = {
                "mode": proposal.target_mode,
                "reason": proposal.reasons[0] if proposal.reasons else "Proposed mode change.",
                "confidence": "high",
            }
            logger.info("[AUDIT] Proposal %s created: mode=%s suggested_load=%.1fW", proposal.id, proposal.target_mode, proposal.suggested_load_limit_w)

        elif action == "withdraw":
            if active_proposal:
                p_id = active_proposal.get("id")
                if p_id:
                    db.execute("UPDATE decisions SET status='withdrawn', responded_at=? WHERE id=?", (iso_now(), p_id))
            state["pending_decision"] = None
            add_alert(state, "info", "Mode proposal withdrawn", eval_result["reason"])
            logger.info("[AUDIT] Proposal withdrawn: %s", eval_result["reason"])

        elif action == "expire":
            if active_proposal:
                p_id = active_proposal.get("id")
                if p_id:
                    db.execute("UPDATE decisions SET status='expired', responded_at=? WHERE id=?", (iso_now(), p_id))
            state["pending_decision"] = None
            logger.info("[AUDIT] Proposal expired")

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

            run_policy_assessment(db, state)
            save_state(state, db)

    def state_response(db: sqlite3.Connection) -> dict[str, Any]:
        state = load_state(db)
        state["weather_provider_configured"] = bool(app.config.get("OPENWEATHER_API_KEY"))

        last_telem = state.get("last_telemetry_at")
        if last_telem:
            try:
                telem_dt = datetime.fromisoformat(last_telem.replace("Z", "+00:00"))
                age_s = (utc_now() - telem_dt).total_seconds()
                is_connected = (age_s <= cfg.telemetry_stale_seconds)
                state["simulink_connected"] = is_connected
                if not is_connected:
                    add_alert(
                        state,
                        "warn",
                        "Simulink telemetry offline",
                        f"No telemetry received for {age_s:.0f} seconds (limit {cfg.telemetry_stale_seconds:.0f}s).",
                        key="simulink:offline",
                    )
                else:
                    state["alerts"] = [a for a in state["alerts"] if a.get("key") != "simulink:offline"]
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

        req_mode = state.get("requested_mode", "balanced")
        state["mode"] = req_mode
        state["active_setpoints"] = get_mode_parameters(req_mode, None, cfg)

        pending_commands = db.execute(
            "SELECT COUNT(*) AS count FROM commands WHERE status='pending' AND (expires_at IS NULL OR expires_at > ?)",
            (iso_now(),),
        ).fetchone()["count"]
        state["pending_command_count"] = pending_commands
        return state

    def check_request_auth(endpoint_type: str = "any") -> bool:
        """Validate request against X-AEGIS-Token or Bearer Authorization header."""
        has_any_token = bool(cfg.aegis_token or cfg.device_token or cfg.operator_token)
        if not has_any_token:
            return True  # Dev mode: open access when no token configured

        token = request.headers.get("X-AEGIS-Token", "").strip()
        if not token and "Authorization" in request.headers:
            auth_hdr = request.headers["Authorization"].strip()
            if auth_hdr.lower().startswith("bearer "):
                token = auth_hdr[7:].strip()

        if not token:
            return False

        if endpoint_type == "device":
            valid = {t for t in (cfg.device_token, cfg.aegis_token) if t}
        elif endpoint_type == "operator":
            valid = {t for t in (cfg.operator_token, cfg.aegis_token) if t}
        else:
            valid = {t for t in (cfg.aegis_token, cfg.device_token, cfg.operator_token) if t}

        return token in valid

    @app.before_request
    def authenticate():
        # Pre-flights: return 204 No Content directly
        if request.method == "OPTIONS":
            return Response("", status=204)
        if request.path in ("/api/health", "/api/weather/provider") or request.path.startswith("/api/weather/tiles/"):
            return None

        # Simulink endpoints (GET and POST) require device token
        if request.path.startswith("/api/simulink/"):
            if not check_request_auth("device"):
                logger.warning("Unauthorized access to %s from %s", request.path, request.remote_addr)
                return jsonify({"error": "Unauthorized: invalid or missing X-AEGIS-Token header"}), 401

        # All other POST endpoints require operator token
        elif request.method == "POST":
            if not check_request_auth("operator"):
                logger.warning("Unauthorized POST to %s from %s", request.path, request.remote_addr)
                return jsonify({"error": "Unauthorized: invalid or missing X-AEGIS-Token header"}), 401

    @app.after_request
    def cors(response):
        allowed_origins = cfg.cors_origins
        req_origin = request.headers.get("Origin")
        if req_origin and req_origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = req_origin
        elif "*" in allowed_origins or not allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        else:
            response.headers["Access-Control-Allow-Origin"] = allowed_origins[0]

        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Device-Id, X-AEGIS-Token, Authorization"
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

    @app.get("/api/simulink/setpoints")
    def simulink_setpoints():
        with lock, connect() as db:
            state = load_state(db)
            mode = state.get("requested_mode", "balanced")
            load_limit = None
            if mode == "boost":
                cmd_row = db.execute(
                    "SELECT payload FROM commands WHERE kind='mode' AND status IN ('pending', 'applied') ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if cmd_row:
                    load_limit = json.loads(cmd_row["payload"]).get("parameters", {}).get("load_limit_w")
            params = get_mode_parameters(mode, load_limit, cfg)
            return jsonify({
                "mode": mode,
                "parameters": params,
                "applied": state.get("applied_mode") == mode,
                "simulink_connected": state.get("simulink_connected", False),
                "timestamp": iso_now(),
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
            logger.warning("Weather provider rejected request: HTTP %d", error.code)
            return jsonify({"error": "Weather provider rejected the request", "provider_status": error.code}), 502
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            logger.warning("Weather provider unavailable: %s", type(error).__name__)
            return jsonify({"error": "Weather provider is temporarily unavailable"}), 502

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
            return jsonify({"error": "Weather tile provider is unavailable"}), 502

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

            # If forecast items contain daily-like data, convert for policy
            daily_converted = []
            for item in forecast:
                daily_converted.append({
                    "date": item.get("timestamp", "")[:10],
                    "shortwave_radiation_sum_kwh_m2": 0.5 if item.get("pop", 0) > 50 else 4.0,
                    "precipitation_sum_mm": item.get("rain_mm_3h", 0),
                    "precipitation_probability_max": item.get("pop", 0),
                })
            if daily_converted:
                outlook_payload = {"fetched_at": timestamp.isoformat(), "daily": daily_converted, "hourly": []}
                db.execute(
                    "INSERT OR REPLACE INTO weather_outlook(id, fetched_at, payload) VALUES(1, ?, ?)",
                    (timestamp.isoformat(), json.dumps(outlook_payload)),
                )
                state["outlook_daily"] = daily_converted

            outlook_row = db.execute("SELECT payload FROM weather_outlook WHERE id=1").fetchone()
            outlook = json.loads(outlook_row["payload"]) if outlook_row else None
            irradiance, source, quality = resolve_irradiance(state, outlook, cfg, timestamp)
            state["irradiance"] = irradiance
            state["irradiance_source"] = source
            state["data_quality"] = quality

            db.execute("INSERT INTO weather_samples(received_at,payload) VALUES(?,?)", (timestamp.isoformat(), json.dumps(snapshot)))
            run_policy_assessment(db, state)
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

            # Mirror electrolyzer state from telemetry
            if "electrolyzer_enabled" in clean:
                state["electrolyzer_available"] = True
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

            # Assess policy dynamically with new telemetry values
            run_policy_assessment(db, state)
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
            # Withdraw any active pending proposal
            if state.get("pending_decision"):
                p_id = state["pending_decision"].get("id")
                if p_id:
                    db.execute("UPDATE decisions SET status='withdrawn', responded_at=? WHERE id=?", (iso_now(), p_id))
                state["pending_decision"] = None

            state["mode_control"] = "manual"
            state["requested_mode"] = mode
            state["mode"] = mode
            state["recommendation"] = {"mode": mode, "reason": "Operator manually selected this mode.", "confidence": "operator"}

            params = get_mode_parameters(mode, None, cfg)
            state["active_setpoints"] = params
            cmd_payload = {
                "mode": mode,
                "parameters": params,
                "source": "dashboard_manual",
                "operator": body.get("operator", "user"),
            }
            cmd_id = queue_command(db, "mode", cmd_payload)
            state["last_command_id"] = cmd_id
            save_state(state, db)
            logger.info("[AUDIT] Operator selected mode %s directly (command %d)", mode, cmd_id)
            return jsonify({"accepted": True, "command_id": cmd_id, "state": state})

    @app.post("/api/mode/auto")
    def set_mode_auto():
        with lock, connect() as db:
            state = load_state(db)
            state["mode_control"] = "auto"
            run_policy_assessment(db, state)
            save_state(state, db)
            logger.info("[AUDIT] Operator enabled automatic mode control")
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
            state["electrolyzer_control_mode"] = control_mode
            if control_mode == "manual":
                state["electrolyzer_enabled"] = body["enabled"]
                cmd_payload = {"enabled": body["enabled"], "control_mode": "manual", "source": "operator_override"}
            else:
                cmd_payload = {"control_mode": "auto", "source": "operator_release_to_simulink"}

            cmd_id = queue_command(db, "electrolyzer", cmd_payload)
            state["last_command_id"] = cmd_id
            save_state(state, db)
            logger.info("[AUDIT] Electrolyzer command %d: control_mode=%s", cmd_id, control_mode)
            return jsonify({"accepted": True, "command_id": cmd_id, "state": state})

    @app.post("/api/decisions/<decision_id>/respond")
    def respond_decision(decision_id: str):
        body = request.get_json(silent=True) or {}
        if "approve" not in body or not isinstance(body["approve"], bool):
            return jsonify({"error": "approve must be boolean"}), 400
        approve = body["approve"]

        with lock, connect() as db:
            row = db.execute("SELECT id,target_mode,payload,status,expires_at FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if not row or row["status"] != "pending":
                return jsonify({"error": "pending decision not found"}), 404

            state = load_state(db)
            now = utc_now()
            now_iso = now.isoformat()
            target_mode = row["target_mode"]
            decision_payload = json.loads(row["payload"])

            if approve:
                db.execute("UPDATE decisions SET status='approved', responded_at=? WHERE id=?", (now_iso, decision_id))
                state["pending_decision"] = None
                state["requested_mode"] = target_mode
                state["mode"] = target_mode
                params = get_mode_parameters(target_mode, decision_payload.get("suggested_load_limit_w"), cfg)
                state["active_setpoints"] = params
                cmd_payload = {
                    "mode": target_mode,
                    "parameters": params,
                    "decision_id": decision_id,
                    "source": "user_approval",
                }
                cmd_id = queue_command(db, "mode", cmd_payload)
                state["last_command_id"] = cmd_id
                add_alert(state, "info", f"Mode switch to {target_mode.title()} approved", "Command queued for Simulink.")
                save_state(state, db)
                logger.info("[AUDIT] Proposal %s approved by operator -> mode=%s cmd=%d", decision_id, target_mode, cmd_id)
                return jsonify({"accepted": True, "status": "approved", "command_id": cmd_id, "state": state})
            else:
                db.execute("UPDATE decisions SET status='declined', responded_at=? WHERE id=?", (now_iso, decision_id))
                state["pending_decision"] = None
                add_alert(state, "info", "Mode proposal declined", "Operating mode remains unchanged.")
                save_state(state, db)
                logger.info("[AUDIT] Proposal %s declined by operator", decision_id)
                return jsonify({"accepted": True, "status": "declined", "state": state})

    @app.post("/api/decisions/<decision_id>/cancel")
    def cancel_decision(decision_id: str):
        # Alias to decline
        with lock, connect() as db:
            row = db.execute("SELECT id,status FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if not row or row["status"] != "pending":
                return jsonify({"error": "pending decision not found"}), 404
            now_iso = iso_now()
            db.execute("UPDATE decisions SET status='declined', responded_at=? WHERE id=?", (now_iso, decision_id))
            state = load_state(db)
            state["pending_decision"] = None
            state["mode_control"] = "manual"
            add_alert(state, "info", "Planned mode change cancelled", "The current mode will remain active.")
            save_state(state, db)
            logger.info("[AUDIT] Proposal %s cancelled by operator", decision_id)
            return jsonify({"accepted": True, "status": "declined", "state": state})

    @app.post("/api/alerts/clear")
    def clear_alerts():
        with lock, connect() as db:
            state = load_state(db)
            state["alerts"] = []
            save_state(state, db)
            logger.info("[AUDIT] Operator cleared all alerts")
            return jsonify({"accepted": True, "alerts": []})

    @app.post("/api/alerts/<alert_id>/dismiss")
    def dismiss_alert(alert_id: str):
        with lock, connect() as db:
            state = load_state(db)
            original_len = len(state.get("alerts", []))
            state["alerts"] = [a for a in state.get("alerts", []) if a.get("id") != alert_id]
            save_state(state, db)
            logger.info("[AUDIT] Operator dismissed alert %s", alert_id)
            return jsonify({"accepted": True, "dismissed": original_len != len(state["alerts"]), "alerts": state["alerts"]})

    @app.get("/api/alerts")
    def get_alerts():
        with lock, connect() as db:
            state = load_state(db)
            return jsonify({"alerts": state.get("alerts", [])})

    @app.get("/api/simulink/commands")
    def simulink_commands():
        after = request.args.get("after", default=0, type=int)
        now_iso = iso_now()
        with lock, connect() as db:
            # Mark expired commands
            db.execute("UPDATE commands SET status='expired' WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?", (now_iso,))
            rows = db.execute(
                "SELECT id,kind,payload,status,created_at,expires_at FROM commands WHERE status='pending' AND id>? ORDER BY id LIMIT 100",
                (max(after, 0),),
            ).fetchall()
            return jsonify({
                "commands": [
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                    }
                    for row in rows
                ]
            })

    @app.post("/api/simulink/commands/<int:command_id>/ack")
    def acknowledge_command(command_id: int):
        body = request.get_json(silent=True) or {}
        status = body.get("status", "applied")
        if status not in {"applied", "rejected", "failed"}:
            return jsonify({"error": "status must be applied, rejected, or failed"}), 400

        now_iso = iso_now()
        with lock, connect() as db:
            cmd_row = db.execute(
                "SELECT id,kind,payload,status,expires_at FROM commands WHERE id=? AND status='pending'",
                (command_id,),
            ).fetchone()
            if not cmd_row:
                return jsonify({"error": "pending command not found"}), 404

            if cmd_row["expires_at"] and cmd_row["expires_at"] <= now_iso:
                db.execute("UPDATE commands SET status='expired' WHERE id=?", (command_id,))
                logger.warning("[AUDIT] Attempt to ack expired command %d rejected", command_id)
                return jsonify({"error": "command expired and cannot be acknowledged", "command_id": command_id}), 410

            db.execute("UPDATE commands SET status=?, acknowledged_at=? WHERE id=?", (status, now_iso, command_id))
            state = load_state(db)

            cmd_payload = json.loads(cmd_row["payload"])
            if cmd_row["kind"] == "mode":
                if status == "applied":
                    state["applied_mode"] = cmd_payload["mode"]
                elif status in ("rejected", "failed"):
                    add_alert(
                        state,
                        "critical",
                        f"Simulink {status} {cmd_payload.get('mode', '')} mode",
                        "Safety interlock or physical plant constraint rejected supervisory mode setpoints.",
                    )

            save_state(state, db)
            logger.info("[AUDIT] Command %d acked: status=%s, applied_mode=%s", command_id, status, state.get("applied_mode"))
            return jsonify({
                "accepted": True,
                "command_id": command_id,
                "status": status,
                "applied_mode": state.get("applied_mode"),
            })

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
            elif kind == "decisions":
                rows = db.execute("SELECT id,target_mode,payload,status,created_at,expires_at,responded_at FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
                values = [{"id": row["id"], "target_mode": row["target_mode"], "status": row["status"], "created_at": row["created_at"], "expires_at": row["expires_at"], "responded_at": row["responded_at"], **json.loads(row["payload"])} for row in rows]
            else:
                return jsonify({"error": "kind must be weather, telemetry, commands, or decisions"}), 400
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