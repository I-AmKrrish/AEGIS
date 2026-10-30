"""Unit tests for Phase 2: Weather acquisition and irradiance pipeline."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from app import create_app
from config import AegisConfig, load_config
from weather_service import (
    WeatherService,
    fetch_open_meteo_forecast,
    fetch_openweather_current,
    normalize_openweather_current,
    parse_open_meteo_payload,
    resolve_irradiance,
    solar_irradiance_estimate,
    utc_now,
)


class WeatherAndIrradianceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/test.sqlite3",
            "MODE_NOTICE_SECONDS": 0,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_simulink_inputs_endpoint_default(self):
        response = self.client.get("/api/simulink/inputs")
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertIn("irradiance_w_m2", data)
        self.assertIn("irradiance_source", data)
        self.assertIn("quality", data)
        self.assertIn("timestamp", data)
        self.assertEqual(data["valid_for_s"], 60)
        self.assertEqual(data["irradiance_source"], "cloud-cover-estimate")
        self.assertEqual(data["quality"], "fallback")

    def test_irradiance_priority_measured_over_all(self):
        # Send telemetry with measured irradiance
        response = self.client.post("/api/simulink/telemetry", json={
            "telemetry": {
                "battery_soc": 80,
                "irradiance_w_m2": 780.4,
                "solar_power_w": 1500,
                "load_power_w": 200,
            }
        })
        self.assertEqual(response.status_code, 200)

        # /api/simulink/inputs should return measured irradiance
        inputs = self.client.get("/api/simulink/inputs").json
        self.assertEqual(inputs["irradiance_w_m2"], 780.4)
        self.assertEqual(inputs["irradiance_source"], "measured")
        self.assertEqual(inputs["quality"], "fresh")

    def test_irradiance_priority_open_meteo_when_no_fresh_telemetry(self):
        # Insert cached Open-Meteo outlook into DB
        now = utc_now().isoformat()
        fake_outlook = {
            "fetched_at": now,
            "current_shortwave_radiation_w_m2": 612.3,
            "daily": [],
            "hourly": [],
        }
        with self.app.app_context():
            with self.app.test_client() as _:
                pass
            import sqlite3
            conn = sqlite3.connect(self.app.config["DATABASE"])
            conn.execute("INSERT OR REPLACE INTO weather_outlook(id, fetched_at, payload) VALUES(1, ?, ?)",
                         (now, json.dumps(fake_outlook)))
            conn.commit()
            conn.close()

        inputs = self.client.get("/api/simulink/inputs").json
        self.assertEqual(inputs["irradiance_w_m2"], 612.3)
        self.assertEqual(inputs["irradiance_source"], "open-meteo")
        self.assertEqual(inputs["quality"], "fresh")

    def test_irradiance_open_meteo_marked_stale_when_old(self):
        # Insert old Open-Meteo outlook (fetched 5 hours ago)
        old_time = (utc_now() - timedelta(hours=5)).isoformat()
        fake_outlook = {
            "fetched_at": old_time,
            "current_shortwave_radiation_w_m2": 540.0,
            "daily": [],
            "hourly": [],
        }
        import sqlite3
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.execute("INSERT OR REPLACE INTO weather_outlook(id, fetched_at, payload) VALUES(1, ?, ?)",
                     (old_time, json.dumps(fake_outlook)))
        conn.commit()
        conn.close()

        inputs = self.client.get("/api/simulink/inputs").json
        self.assertEqual(inputs["irradiance_w_m2"], 540.0)
        self.assertEqual(inputs["irradiance_source"], "open-meteo")
        self.assertEqual(inputs["quality"], "stale")

    def test_open_meteo_payload_parsing_and_unit_conversion(self):
        raw = {
            "daily": {
                "time": ["2026-10-31", "2026-11-01"],
                "shortwave_radiation_sum": [18.0, 7.2],  # MJ/m^2
                "precipitation_sum": [0.0, 4.5],
                "precipitation_probability_max": [10, 85],
            },
            "hourly": {
                "time": ["2026-10-31T00:00", "2026-10-31T01:00"],
                "shortwave_radiation": [0.0, 450.0],
                "cloud_cover": [20, 80],
                "precipitation_probability": [5, 40],
            },
            "current": {
                "shortwave_radiation": 450.0,
            },
        }
        parsed = parse_open_meteo_payload(raw)
        self.assertEqual(parsed["current_shortwave_radiation_w_m2"], 450.0)
        self.assertEqual(len(parsed["daily"]), 2)
        day0 = parsed["daily"][0]
        self.assertEqual(day0["date"], "2026-10-31")
        self.assertEqual(day0["shortwave_radiation_sum_mj_m2"], 18.0)
        # 18.0 MJ / 3.6 = 5.0 kWh/m^2
        self.assertEqual(day0["shortwave_radiation_sum_kwh_m2"], 5.0)

        day1 = parsed["daily"][1]
        # 7.2 MJ / 3.6 = 2.0 kWh/m^2
        self.assertEqual(day1["shortwave_radiation_sum_kwh_m2"], 2.0)
        self.assertEqual(day1["precipitation_sum_mm"], 4.5)
        self.assertEqual(day1["precipitation_probability_max"], 85)

    def test_openweather_normalization(self):
        raw = {
            "name": "Yelahanka",
            "main": {"temp": 28.5, "feels_like": 29.0, "temp_max": 30.0, "temp_min": 22.0, "humidity": 65, "pressure": 1014},
            "wind": {"speed": 4.5, "deg": 120},
            "clouds": {"all": 40},
            "rain": {"1h": 0.5},
            "weather": [{"description": "scattered clouds"}],
        }
        norm = normalize_openweather_current(raw)
        self.assertEqual(norm["city"], "Yelahanka")
        self.assertEqual(norm["temp"], 28.5)
        self.assertEqual(norm["wind_kmh"], round(4.5 * 3.6, 1))
        self.assertEqual(norm["clouds"], 40)
        self.assertEqual(norm["rain_mm_1h"], 0.5)
        self.assertEqual(norm["description"], "Scattered clouds")

    def test_weather_service_resilience_on_network_error(self):
        cfg = load_config(env={"OPENWEATHER_API_KEY": "dummy-key"})
        svc = WeatherService(cfg)
        with patch("weather_service.urlopen", side_effect=URLError("Network unreachable")):
            # Should not crash or throw unhandled exceptions
            svc.refresh_all()
            self.assertIsNotNone(svc.last_error)
            self.assertIn("Network unreachable", svc.last_error)


if __name__ == "__main__":
    unittest.main()
