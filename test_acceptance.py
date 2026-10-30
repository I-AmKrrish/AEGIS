"""Acceptance Tests for AEGIS Supervisory Controller.

Directly tests all 9 core operational scenarios specified in Section 8 of the
system engineering requirements.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import ClosingConnection, create_app


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


class AcceptanceScenariosTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self.tmp_dir.name}/test_acceptance.sqlite3"
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SCHEDULER_ENABLED": False,
        })
        self.client = self.app.test_client()
        self.weather_svc = self.app.weather_service

    def tearDown(self) -> None:
        if self.weather_svc:
            self.weather_svc.stop()
        import gc
        gc.collect()
        self.tmp_dir.cleanup()

    def _db(self) -> ClosingConnection:
        conn = sqlite3.connect(self.db_path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def _set_outlook(self, daily: list[dict], current_irr: float = 0.0, fetched_at: str | None = None) -> None:
        if fetched_at is None:
            fetched_at = iso_now()
        outlook = {
            "fetched_at": fetched_at,
            "current_shortwave_radiation_w_m2": current_irr,
            "daily": daily,
            "hourly": [{"time": iso_now(), "shortwave_radiation_w_m2": current_irr}],
        }
        with self.weather_svc._lock:
            self.weather_svc.outlook = outlook
        self.weather_svc.state_updater(outlook=outlook)

    def test_scenario_1_balanced_clear_day(self) -> None:
        """Scenario 1: Clear 10-day forecast in Balanced mode.

        Expected: No mode proposal created. Irradiance inputs delivered with quality fresh.
        """
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 12.0,
                "shortwave_radiation_sum_mj_m2": 43.2,
                "precipitation_sum_mm": 0.0,
                "precipitation_probability_max": 5.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=750.0)

        # Check state: no pending decision
        state = self.client.get("/api/state").get_json()
        self.assertIsNone(state["pending_decision"])

        # Check /api/simulink/inputs
        inp = self.client.get("/api/simulink/inputs").get_json()
        self.assertEqual(inp["irradiance_source"], "open-meteo")
        self.assertEqual(inp["quality"], "fresh")
        self.assertAlmostEqual(inp["irradiance_w_m2"], 750.0, places=1)

    def test_scenario_2_boost_trigger_without_auto_command(self) -> None:
        """Scenario 2: 3 rain days + 7 low-sun days, tank at 60%.

        Expected: Exactly one Boost proposal created with plain-language prompt.
        NO command queued yet (operator approval required!).
        """
        # Post initial telemetry with tank at 60%
        self.client.post(
            "/api/simulink/telemetry",
            json={
                "telemetry": {
                    "battery_soc": 70.0,
                    "h2_level": 60.0,
                    "operating_mode": "balanced",
                    "solar_power_w": 20.0,
                    "load_power_w": 100.0,
                    "irradiance_w_m2": 100.0,
                    "sequence": 1,
                }
            },
        )

        # 3 rain days + 7 low-sun days
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 0.4,
                "shortwave_radiation_sum_mj_m2": 1.44,
                "precipitation_sum_mm": 15.0 if i < 3 else 0.0,
                "precipitation_probability_max": 80.0 if i < 3 else 20.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=100.0)

        # Verify proposal created in state
        state = self.client.get("/api/state").get_json()
        self.assertIsNotNone(state["pending_decision"])
        prop = state["pending_decision"]
        self.assertEqual(prop["target_mode"], "boost")
        self.assertIn("Boost", prop["question"])
        self.assertIn("effects", prop)

        # CRITICAL: Verify NO command is queued yet
        cmd_resp = self.client.get("/api/simulink/commands").get_json()
        self.assertEqual(len(cmd_resp["commands"]), 0)

    def test_scenario_3_proposal_approval_and_applied_ack(self) -> None:
        """Scenario 3: Operator clicks 'Yes, switch'.

        Expected: Mode command queued with Boost setpoints; plant acks applied;
        applied_mode updates to boost.
        """
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 70.0, "h2_level": 60.0, "operating_mode": "balanced", "sequence": 1}},
        )
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 0.4,
                "shortwave_radiation_sum_mj_m2": 1.44,
                "precipitation_sum_mm": 15.0,
                "precipitation_probability_max": 80.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=100.0)

        state = self.client.get("/api/state").get_json()
        decision_id = state["pending_decision"]["id"]

        # 1. Operator approves
        approve_resp = self.client.post(f"/api/decisions/{decision_id}/respond", json={"approve": True})
        self.assertEqual(approve_resp.status_code, 200)

        # 2. Mode command queued
        cmds = self.client.get("/api/simulink/commands").get_json()["commands"]
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertEqual(cmd["kind"], "mode")
        self.assertEqual(cmd["payload"]["mode"], "boost")
        self.assertEqual(cmd["payload"]["parameters"]["electrolyzer_start_soc"], 75.0)

        # Prior to plant ack, applied_mode is not boost
        state_pre = self.client.get("/api/state").get_json()
        self.assertEqual(state_pre["requested_mode"], "boost")
        self.assertNotEqual(state_pre["applied_mode"], "boost")

        # 3. Plant acknowledges applied
        ack_resp = self.client.post(f"/api/simulink/commands/{cmd['id']}/ack", json={"status": "applied"})
        self.assertEqual(ack_resp.status_code, 200)

        # 4. applied_mode is now boost
        state_post = self.client.get("/api/state").get_json()
        self.assertEqual(state_post["applied_mode"], "boost")
        self.assertEqual(state_post["requested_mode"], "boost")

    def test_scenario_4_proposal_decline_and_cooldown(self) -> None:
        """Scenario 4: Operator clicks 'Not now'.

        Expected: Proposal transitions to declined; no command queued; cooldown
        prevents repeated proposal.
        """
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 70.0, "h2_level": 60.0, "operating_mode": "balanced", "sequence": 1}},
        )
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 0.4,
                "shortwave_radiation_sum_mj_m2": 1.44,
                "precipitation_sum_mm": 15.0,
                "precipitation_probability_max": 80.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=100.0)

        state = self.client.get("/api/state").get_json()
        decision_id = state["pending_decision"]["id"]

        # Operator declines
        decline_resp = self.client.post(f"/api/decisions/{decision_id}/respond", json={"approve": False})
        self.assertEqual(decline_resp.status_code, 200)

        # Verify no command queued
        cmds = self.client.get("/api/simulink/commands").get_json()["commands"]
        self.assertEqual(len(cmds), 0)

        # Telemetry updates with same conditions -> cooldown prevents duplicate proposal
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 70.0, "h2_level": 60.0, "operating_mode": "balanced", "sequence": 2}},
        )
        state2 = self.client.get("/api/state").get_json()
        self.assertIsNone(state2["pending_decision"])

    def test_scenario_5_full_tank_gate_prevents_boost_proposal(self) -> None:
        """Scenario 5: Bad weather streak, but hydrogen tank is >= 98%.

        Expected: No Boost proposal generated despite bad weather.
        """
        # Set hydrogen tank to 99.0% first
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 70.0, "h2_level": 99.0, "operating_mode": "balanced", "sequence": 1}},
        )

        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 0.3,
                "shortwave_radiation_sum_mj_m2": 1.08,
                "precipitation_sum_mm": 20.0,
                "precipitation_probability_max": 90.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=50.0)

        state = self.client.get("/api/state").get_json()
        self.assertIsNone(state["pending_decision"])

    def test_scenario_6_forecast_clears_in_boost_proposes_return_to_balanced(self) -> None:
        """Scenario 6: Weather clears while in Boost mode, tank >= 90%.

        Expected: 'Return to Balanced' proposal generated requiring confirmation.
        """
        # Set state to Boost mode
        with self._db() as db:
            row = db.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
            state = json.loads(row["payload"])
            state["requested_mode"] = "boost"
            state["applied_mode"] = "boost"
            state["mode"] = "boost"
            db.execute("UPDATE app_state SET payload=? WHERE id=1", (json.dumps(state),))
            db.commit()

        # Telemetry in Boost with tank at 92%
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 85.0, "h2_level": 92.0, "operating_mode": "boost", "sequence": 1}},
        )

        # Sunny forecast (12.0 kWh/m2 daily)
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 12.0,
                "shortwave_radiation_sum_mj_m2": 43.2,
                "precipitation_sum_mm": 0.0,
                "precipitation_probability_max": 5.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=700.0)

        state = self.client.get("/api/state").get_json()
        self.assertIsNotNone(state["pending_decision"])
        prop = state["pending_decision"]
        self.assertEqual(prop["target_mode"], "balanced")
        self.assertIn("Return to Balanced", prop["question"])

    def test_scenario_7_upstream_weather_fault_marked_stale(self) -> None:
        """Scenario 7: Weather API network fault/timeout.

        Expected: Data marked stale, server does not crash, no invalid proposals.
        """
        # Seed cache with old timestamp (3 hours ago) and clear weather
        old_time = (utc_now() - timedelta(hours=3)).isoformat()
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 12.0,
                "shortwave_radiation_sum_mj_m2": 43.2,
                "precipitation_sum_mm": 0.0,
                "precipitation_probability_max": 0.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=500.0, fetched_at=old_time)

        inp = self.client.get("/api/simulink/inputs").get_json()
        self.assertEqual(inp["quality"], "stale")

        state = self.client.get("/api/state").get_json()
        self.assertIsNone(state["pending_decision"])

    def test_scenario_8_telemetry_loss_watchdog_and_expired_commands(self) -> None:
        """Scenario 8: No telemetry received for > 15s.

        Expected: simulink_connected becomes false; watchdog alert raised;
        expired commands cannot be acknowledged.
        """
        old_ts = (utc_now() - timedelta(seconds=20)).isoformat()
        expired_ts = (utc_now() - timedelta(seconds=5)).isoformat()

        with self._db() as db:
            row = db.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
            state = json.loads(row["payload"])
            state["last_telemetry_at"] = old_ts
            state["simulink_connected"] = True
            db.execute("UPDATE app_state SET payload=? WHERE id=1", (json.dumps(state),))

            cursor = db.execute(
                """INSERT INTO commands (kind, payload, status, created_at, expires_at)
                   VALUES ('mode', '{"mode":"eco"}', 'pending', ?, ?)""",
                (old_ts, expired_ts),
            )
            cmd_id = cursor.lastrowid
            db.commit()

        # Telemetry watchdog check triggered by GET /api/state
        state_resp = self.client.get("/api/state").get_json()
        self.assertFalse(state_resp["simulink_connected"])

        # Check alert was raised
        alerts = state_resp["alerts"]
        self.assertTrue(any(a.get("severity") in ("warn", "warning") and "offline" in a.get("title", "").lower() for a in alerts))

        # Attempt to acknowledge the expired command -> rejected with 410 Gone
        ack_resp = self.client.post(f"/api/simulink/commands/{cmd_id}/ack", json={"status": "applied"})
        self.assertEqual(ack_resp.status_code, 410)

    def test_scenario_9_manual_mode_override_withdraws_pending_proposal(self) -> None:
        """Scenario 9: Operator manually selects mode on Mode page.

        Expected: Mode command queued immediately without confirmation; any
        pending decision proposal is automatically withdrawn.
        """
        # 1. Post telemetry then bad weather to create a pending proposal
        self.client.post(
            "/api/simulink/telemetry",
            json={"telemetry": {"battery_soc": 70.0, "h2_level": 60.0, "operating_mode": "balanced", "sequence": 1}},
        )
        base = utc_now().date()
        daily = [
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "shortwave_radiation_sum_kwh_m2": 0.4,
                "shortwave_radiation_sum_mj_m2": 1.44,
                "precipitation_sum_mm": 15.0,
                "precipitation_probability_max": 80.0,
            }
            for i in range(10)
        ]
        self._set_outlook(daily, current_irr=100.0)

        state = self.client.get("/api/state").get_json()
        self.assertIsNotNone(state["pending_decision"])
        proposal_id = state["pending_decision"]["id"]

        # 2. Operator manually overrides mode to eco
        override_resp = self.client.post("/api/mode", json={"mode": "eco"})
        self.assertEqual(override_resp.status_code, 200)

        # 3. Mode command queued immediately
        cmds = self.client.get("/api/simulink/commands").get_json()["commands"]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["payload"]["mode"], "eco")

        # 4. Pending proposal withdrawn in state
        state_post = self.client.get("/api/state").get_json()
        self.assertIsNone(state_post["pending_decision"])

        # Verify in database that proposal status is "withdrawn"
        with self._db() as db:
            row = db.execute("SELECT status FROM decisions WHERE id = ?", (proposal_id,)).fetchone()
            self.assertEqual(row["status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
