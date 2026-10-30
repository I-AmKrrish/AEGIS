"""Unit tests for Phase 4: Security, tokens, CORS, stale detection, and alert endpoints."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import create_app
from config import AegisConfig, load_config


class SecurityAndRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_token_authentication_enforced_when_configured(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/sec_test.sqlite3",
            "aegis_token": "master-secret-123",
        })
        client = app.test_client()

        # 1. Unauthenticated POST must be rejected with 401
        res = client.post("/api/mode", json={"mode": "eco"})
        self.assertEqual(res.status_code, 401)
        self.assertIn("Unauthorized", res.json["error"])

        # 2. Unauthenticated Simulink endpoint must be rejected with 401
        res = client.get("/api/simulink/inputs")
        self.assertEqual(res.status_code, 401)

        # 3. Authenticated POST with X-AEGIS-Token must succeed
        res = client.post(
            "/api/mode",
            json={"mode": "eco"},
            headers={"X-AEGIS-Token": "master-secret-123"},
        )
        self.assertEqual(res.status_code, 200)

        # 4. Authenticated Simulink endpoint with Bearer header must succeed
        res = client.get(
            "/api/simulink/inputs",
            headers={"Authorization": "Bearer master-secret-123"},
        )
        self.assertEqual(res.status_code, 200)

        # 5. Public health check works without token
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)

        # 6. CORS preflight OPTIONS works without token
        res = client.options("/api/mode")
        self.assertEqual(res.status_code, 204)

    def test_device_and_operator_tokens_isolation(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/iso_test.sqlite3",
            "device_token": "simulink-device-token",
            "operator_token": "operator-ui-token",
        })
        client = app.test_client()

        # Device token can access Simulink endpoints
        res = client.get("/api/simulink/inputs", headers={"X-AEGIS-Token": "simulink-device-token"})
        self.assertEqual(res.status_code, 200)

        # Device token CANNOT access Operator POST endpoint
        res = client.post("/api/mode", json={"mode": "eco"}, headers={"X-AEGIS-Token": "simulink-device-token"})
        self.assertEqual(res.status_code, 401)

        # Operator token CAN access Operator POST endpoint
        res = client.post("/api/mode", json={"mode": "eco"}, headers={"X-AEGIS-Token": "operator-ui-token"})
        self.assertEqual(res.status_code, 200)

        # Operator token CANNOT access Simulink endpoint
        res = client.get("/api/simulink/inputs", headers={"X-AEGIS-Token": "operator-ui-token"})
        self.assertEqual(res.status_code, 401)

    def test_cors_headers_and_origin_whitelist(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/cors_test.sqlite3",
            "cors_origins": ["https://microgrid.local", "http://localhost:3000"],
        })
        client = app.test_client()

        res = client.get("/api/health", headers={"Origin": "https://microgrid.local"})
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "https://microgrid.local")
        self.assertIn("X-AEGIS-Token", res.headers.get("Access-Control-Allow-Headers", ""))

    def test_stale_telemetry_watchdog_raises_alert(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/stale_test.sqlite3",
            "telemetry_stale_seconds": 5.0,
        })
        client = app.test_client()

        # Ingest telemetry with timestamp 10 seconds ago
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        import sqlite3
        conn = sqlite3.connect(app.config["DATABASE"])
        state = json.loads(conn.execute("SELECT payload FROM app_state WHERE id=1").fetchone()[0])
        state["last_telemetry_at"] = old_time
        conn.execute("UPDATE app_state SET payload=? WHERE id=1", (json.dumps(state),))
        conn.commit()
        conn.close()

        # Query state -> should detect stale telemetry and raise alert
        state_resp = client.get("/api/state").json
        self.assertFalse(state_resp["simulink_connected"])
        self.assertTrue(any("offline" in a["title"].lower() for a in state_resp["alerts"]))

    def test_alert_clearing_and_dismissal_endpoints(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/alert_test.sqlite3",
        })
        client = app.test_client()

        # Trigger an alert by posting invalid telemetry
        client.post("/api/telemetry", json={"battery_soc": 150})

        # Add manual alert to state
        import sqlite3
        conn = sqlite3.connect(app.config["DATABASE"])
        state = json.loads(conn.execute("SELECT payload FROM app_state WHERE id=1").fetchone()[0])
        state["alerts"] = [
            {"id": "alert-1", "severity": "warn", "title": "Test Alert 1", "message": "Msg 1"},
            {"id": "alert-2", "severity": "info", "title": "Test Alert 2", "message": "Msg 2"},
        ]
        conn.execute("UPDATE app_state SET payload=? WHERE id=1", (json.dumps(state),))
        conn.commit()
        conn.close()

        # Test GET /api/alerts
        alerts = client.get("/api/alerts").json["alerts"]
        self.assertEqual(len(alerts), 2)

        # Test POST /api/alerts/<id>/dismiss
        dis_res = client.post("/api/alerts/alert-1/dismiss")
        self.assertEqual(dis_res.status_code, 200)
        remaining = client.get("/api/alerts").json["alerts"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], "alert-2")

        # Test POST /api/alerts/clear
        clear_res = client.post("/api/alerts/clear")
        self.assertEqual(clear_res.status_code, 200)
        self.assertEqual(len(client.get("/api/alerts").json["alerts"]), 0)

    def test_expired_command_cannot_be_acknowledged(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": f"{self.temp_dir.name}/exp_cmd_test.sqlite3",
        })
        client = app.test_client()

        # Queue a mode command with 0.1 second TTL
        res = client.post("/api/mode", json={"mode": "eco"})
        cmd_id = res.json["command_id"]

        # Backdate the command expiry in DB
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        import sqlite3
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.execute("UPDATE commands SET expires_at=? WHERE id=?", (expired_time, cmd_id))
        conn.commit()
        conn.close()

        # Attempt to ack -> must return 410 Gone / expired
        ack_res = client.post(f"/api/simulink/commands/{cmd_id}/ack", json={"status": "applied"})
        self.assertEqual(ack_res.status_code, 410)
        self.assertIn("expired", ack_res.json["error"])


if __name__ == "__main__":
    unittest.main()
