import tempfile
import unittest
import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app


class AegisApiTests(unittest.TestCase):
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

    def test_weather_is_persisted_and_irradiance_is_estimated(self):
        response = self.client.post("/api/weather", json={
            "lat": 13.1,
            "lon": 77.6,
            "weather": {"temp": 27, "humidity": 60, "wind_kmh": 12, "clouds": 25, "rain_mm_1h": 0},
            "forecast": [],
        })
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json["irradiance_w_m2"], 0)
        self.assertEqual(response.json["irradiance_source"], "cloud-cover-estimate")
        state = self.client.get("/api/state").json
        self.assertEqual(state["location"], {"lat": 13.1, "lon": 77.6})
        self.assertEqual(state["weather"]["wind_kmh"], 12)
        self.assertEqual(len(self.client.get("/api/history/weather").json["items"]), 1)

    def test_forecast_alert_precedes_supervisory_mode_command(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        response = self.client.post("/api/weather", json={
            "lat": 13.1,
            "lon": 77.6,
            "weather": {"clouds": 80, "rain_mm_1h": 0},
            "forecast": [{"timestamp": future, "pop": 85, "clouds": 95, "rain_mm_3h": 3, "wind_kmh": 20}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["pending_decision"]["target_mode"], "eco")
        self.assertTrue(any("Mode change planned" in item["title"] for item in response.json["alerts"]))
        commands = self.client.get("/api/simulink/commands").json["commands"]
        self.assertEqual([item["kind"] for item in commands], ["mode"])
        self.assertEqual(commands[0]["payload"]["source"], "weather_policy")

    def test_electrolyzer_auto_threshold_and_manual_override(self):
        response = self.client.post("/api/simulink/telemetry", json={
            "telemetry": {"battery_soc": 90, "h2_level": 40, "irradiance_w_m2": 700, "solar_power_w": 1500, "load_power_w": 400},
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["state"]["electrolyzer_enabled"])
        self.assertEqual(response.json["state"]["electrolyzer_control_mode"], "auto")
        command = self.client.get("/api/simulink/commands").json["commands"][-1]
        self.assertEqual(command["kind"], "electrolyzer")
        manual = self.client.post("/api/electrolyzer", json={"control_mode": "manual", "enabled": False})
        self.assertEqual(manual.status_code, 200)
        self.assertFalse(manual.json["state"]["electrolyzer_enabled"])
        self.assertEqual(manual.json["state"]["electrolyzer_control_mode"], "manual")

    def test_invalid_telemetry_is_rejected(self):
        response = self.client.post("/api/telemetry", json={"battery_soc": 140})
        self.assertEqual(response.status_code, 400)

    def test_weather_provider_proxies_normalized_current_and_forecast(self):
        current = {
            "name": "Test City",
            "main": {"temp": 22.4, "feels_like": 22, "temp_max": 24, "temp_min": 18, "humidity": 70, "pressure": 1012},
            "weather": [{"description": "light rain"}],
            "wind": {"speed": 3, "deg": 45},
            "clouds": {"all": 80},
            "rain": {"1h": 0.4},
        }
        forecast = {"list": [{
            "dt": int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp()),
            "main": {"temp": 20}, "pop": 0.75, "clouds": {"all": 90},
            "rain": {"3h": 2.1}, "wind": {"speed": 4},
        }]}
        responses = [current, forecast]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(self.payload).encode()

        with self.app.app_context():
            self.app.config["OPENWEATHER_API_KEY"] = "test-key-not-returned"
            with patch("app.urlopen", side_effect=[FakeResponse(item) for item in responses]) as upstream:
                response = self.client.get("/api/weather/provider?lat=13.1&lon=77.6")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(response.json["weather"]["city"], "Test City")
        self.assertEqual(response.json["weather"]["wind_kmh"], 10.8)
        self.assertEqual(response.json["forecast"][0]["pop"], 75)
        self.assertNotIn("test-key-not-returned", response.get_data(as_text=True))

    def test_weather_provider_reports_missing_server_key(self):
        response = self.client.get("/api/weather/provider?lat=13.1&lon=77.6")
        self.assertEqual(response.status_code, 503)

    def test_weather_tile_proxy_hides_provider_key(self):
        class FakeTile:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b"png-tile"

        with self.app.app_context():
            self.app.config["OPENWEATHER_API_KEY"] = "tile-secret"
            with patch("app.urlopen", return_value=FakeTile()) as upstream:
                response = self.client.get("/api/weather/tiles/rain/5/10/12.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.data, b"png-tile")
        self.assertIn("tile-secret", upstream.call_args.args[0])
        self.assertNotIn(b"tile-secret", response.data)


if __name__ == "__main__":
    unittest.main()