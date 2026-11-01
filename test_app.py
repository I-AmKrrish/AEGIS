import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
        # In the new architecture, forecast creates a pending proposal;
        # NO command is queued until user approval.
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        response = self.client.post("/api/weather", json={
            "lat": 13.1,
            "lon": 77.6,
            "weather": {"clouds": 80, "rain_mm_1h": 0},
            "forecast": [{"timestamp": future, "pop": 85, "clouds": 95, "rain_mm_3h": 3, "wind_kmh": 20}],
        })
        self.assertEqual(response.status_code, 200)
        pending = response.json["pending_decision"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["target_mode"], "eco")
        self.assertTrue(any("Rain forecast" in item["title"] for item in response.json["alerts"]))

        # Verified: No command sent without user approval
        commands = self.client.get("/api/simulink/commands").json["commands"]
        self.assertEqual(len(commands), 0)

        # User approves proposal via /api/decisions/<id>/respond
        decision_id = pending["id"]
        res = self.client.post(f"/api/decisions/{decision_id}/respond", json={"approve": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["status"], "approved")

        # Command is now queued with parameters and decision_id
        commands = self.client.get("/api/simulink/commands").json["commands"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["kind"], "mode")
        self.assertEqual(commands[0]["payload"]["mode"], "eco")
        self.assertEqual(commands[0]["payload"]["decision_id"], decision_id)
        self.assertIn("parameters", commands[0]["payload"])
        self.assertIn("expires_at", commands[0])

    def test_electrolyzer_auto_threshold_and_manual_override(self):
        # Simulink owns automatic hysteresis (Decision 6); Flask mirrors state from telemetry.
        # Sending telemetry does not automatically generate a command.
        response = self.client.post("/api/simulink/telemetry", json={
            "telemetry": {
                "battery_soc": 90,
                "h2_level": 40,
                "irradiance_w_m2": 700,
                "solar_power_w": 1500,
                "load_power_w": 400,
                "electrolyzer_enabled": True,
            },
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["state"]["electrolyzer_enabled"])
        self.assertEqual(response.json["state"]["electrolyzer_control_mode"], "auto")

        # Verify no auto-command was queued by Flask
        commands = self.client.get("/api/simulink/commands").json["commands"]
        self.assertEqual(len(commands), 0)

        # Manual override queues an operator command
        manual = self.client.post("/api/electrolyzer", json={"control_mode": "manual", "enabled": False})
        self.assertEqual(manual.status_code, 200)
        self.assertFalse(manual.json["state"]["electrolyzer_enabled"])
        self.assertEqual(manual.json["state"]["electrolyzer_control_mode"], "manual")

        command = self.client.get("/api/simulink/commands").json["commands"][-1]
        self.assertEqual(command["kind"], "electrolyzer")
        self.assertFalse(command["payload"]["enabled"])

    def test_invalid_telemetry_is_rejected(self):
        response = self.client.post("/api/telemetry", json={"battery_soc": 140})
        self.assertEqual(response.status_code, 400)

    def test_decision_decline_does_not_queue_command(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        res = self.client.post("/api/weather", json={
            "lat": 13.1, "lon": 77.6,
            "weather": {"clouds": 80, "rain_mm_1h": 0},
            "forecast": [{"timestamp": future, "pop": 85, "clouds": 95, "rain_mm_3h": 3, "wind_kmh": 20}],
        })
        pending = res.json["pending_decision"]
        self.assertIsNotNone(pending)

        # Operator declines
        dec_res = self.client.post(f"/api/decisions/{pending['id']}/respond", json={"approve": False})
        self.assertEqual(dec_res.status_code, 200)
        self.assertEqual(dec_res.json["status"], "declined")

        commands = self.client.get("/api/simulink/commands").json["commands"]
        self.assertEqual(len(commands), 0)
        state = self.client.get("/api/state").json
        self.assertIsNone(state["pending_decision"])

    def test_command_acknowledgment_lifecycle_and_alerts(self):
        # Queue manual mode change
        res = self.client.post("/api/mode", json={"mode": "boost"})
        self.assertEqual(res.status_code, 200)
        cmd_id = res.json["command_id"]

        state = self.client.get("/api/state").json
        self.assertEqual(state["requested_mode"], "boost")
        self.assertIsNone(state["applied_mode"])  # Not applied until ack

        # Acknowledge applied
        ack_res = self.client.post(f"/api/simulink/commands/{cmd_id}/ack", json={"status": "applied"})
        self.assertEqual(ack_res.status_code, 200)
        self.assertEqual(ack_res.json["applied_mode"], "boost")

        state = self.client.get("/api/state").json
        self.assertEqual(state["applied_mode"], "boost")

        # Now test rejected ack raises alert without modifying applied_mode
        res2 = self.client.post("/api/mode", json={"mode": "eco"})
        cmd_id2 = res2.json["command_id"]
        ack_rej = self.client.post(f"/api/simulink/commands/{cmd_id2}/ack", json={"status": "rejected"})
        self.assertEqual(ack_rej.status_code, 200)

        state2 = self.client.get("/api/state").json
        self.assertEqual(state2["applied_mode"], "boost")  # Unchanged
        self.assertTrue(any("rejected" in a["title"] for a in state2["alerts"]))

    def test_simulink_setpoints_endpoint(self):
        self.client.post("/api/mode", json={"mode": "eco"})
        setpoints = self.client.get("/api/simulink/setpoints").json
        self.assertEqual(setpoints["mode"], "eco")
        self.assertIn("parameters", setpoints)
        self.assertEqual(setpoints["parameters"]["load_limit_w"], 85.0)
        self.assertEqual(setpoints["parameters"]["electrolyzer_start_soc"], 90.0)

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

    def test_root_serves_index_html(self):
        with self.client.get("/") as res:
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/html", res.mimetype)
            self.assertIn(b"<title>AEGIS</title>", res.data)

        with self.client.get("/index.html") as res_direct:
            self.assertEqual(res_direct.status_code, 200)
            self.assertIn("text/html", res_direct.mimetype)


if __name__ == "__main__":
    unittest.main()