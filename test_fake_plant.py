"""Unit tests for the Simulink Plant Simulator (tools/fake_plant.py)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tools.fake_plant import FakePlant


class TestFakePlant(unittest.TestCase):
    def setUp(self) -> None:
        self.plant = FakePlant(
            base_url="http://127.0.0.1:5000",
            token="test-token",
            load_nominal_w=100.0,
            battery_hours=18.0,
            h2_tank_wh=2400.0,
            initial_battery_soc=60.0,
            initial_h2=50.0,
            panel_area_m2=1.0,
            panel_efficiency=0.20,
        )

    def test_initialization(self) -> None:
        self.assertEqual(self.plant.battery_wh, 1800.0)
        self.assertEqual(self.plant.h2_tank_wh, 2400.0)
        self.assertEqual(self.plant.battery_soc, 60.0)
        self.assertEqual(self.plant.h2_level, 50.0)
        self.assertEqual(self.plant.operating_mode, "balanced")
        self.assertFalse(self.plant.electrolyzer_enabled)

    def test_solar_power_and_battery_charging_during_surplus(self) -> None:
        self.plant.irradiance_w_m2 = 1000.0  # 1000 * 1.0 * 0.20 = 200 W solar
        self.plant.load_nominal_w = 100.0
        self.plant.load_limit_w = 100.0
        self.plant.electrolyzer_enabled = False

        # 3600 seconds = 1 hour. Net power = +100 W. Surplus = 100 Wh.
        # Battery capacity = 1800 Wh. Delta = (100 / 1800) * 100 = 5.555%
        self.plant.step_physical_model(3600.0)

        self.assertEqual(self.plant.solar_power_w, 200.0)
        self.assertEqual(self.plant.load_power_w, 100.0)
        self.assertAlmostEqual(self.plant.battery_soc, 60.0 + (100.0 / 1800.0) * 100.0, places=2)
        self.assertFalse(self.plant.fuel_cell_active)

    def test_electrolyzer_production_when_enabled(self) -> None:
        self.plant.battery_soc = 92.0
        self.plant.irradiance_w_m2 = 1000.0  # 200 W solar
        self.plant.load_limit_w = 100.0
        # Electrolyzer starts automatically at >= 90% SOC
        self.plant.step_physical_model(3600.0)

        self.assertTrue(self.plant.electrolyzer_enabled)
        # Surplus 100 Wh * 0.70 = 70 Wh into 2400 Wh tank
        # Delta H2 = (70 / 2400) * 100 = 2.9167%
        self.assertAlmostEqual(self.plant.h2_level, 50.0 + (70.0 / 2400.0) * 100.0, places=2)

    def test_battery_discharging_during_night_deficit(self) -> None:
        self.plant.irradiance_w_m2 = 0.0
        self.plant.battery_soc = 60.0
        self.plant.load_limit_w = 100.0

        # 1 hour deficit of 100 W = 100 Wh
        self.plant.step_physical_model(3600.0)

        self.assertEqual(self.plant.solar_power_w, 0.0)
        self.assertAlmostEqual(self.plant.battery_soc, 60.0 - (100.0 / 1800.0) * 100.0, places=2)
        self.assertFalse(self.plant.fuel_cell_active)

    def test_fuel_cell_activates_when_battery_critically_low(self) -> None:
        self.plant.irradiance_w_m2 = 0.0
        self.plant.battery_soc = 15.0  # < 20%
        self.plant.h2_level = 50.0
        self.plant.load_limit_w = 100.0

        # Deficit = 100 Wh. Fuel cell provides deficit at 50% efficiency -> 200 Wh H2 consumed
        self.plant.step_physical_model(3600.0)

        self.assertTrue(self.plant.fuel_cell_active)
        self.assertEqual(self.plant.battery_soc, 15.0)  # Battery protected
        # Delta H2 = (200 / 2400) * 100 = 8.333%
        self.assertAlmostEqual(self.plant.h2_level, 50.0 - (200.0 / 2400.0) * 100.0, places=2)

    def test_fuel_cell_night_use_in_boost_mode(self) -> None:
        self.plant.irradiance_w_m2 = 0.0
        self.plant.battery_soc = 70.0
        self.plant.h2_level = 50.0
        self.plant.fuel_cell_night_use = "allowed_at_night"
        self.plant.load_limit_w = 60.0

        self.plant.step_physical_model(3600.0)

        self.assertTrue(self.plant.fuel_cell_active)
        # 60 Wh deficit -> 120 Wh H2 used
        self.assertAlmostEqual(self.plant.h2_level, 50.0 - (120.0 / 2400.0) * 100.0, places=2)

    @patch.object(FakePlant, "_http_request")
    def test_poll_and_apply_mode_command(self, mock_http: MagicMock) -> None:
        mock_http.side_effect = [
            # Response for GET /api/simulink/commands
            {
                "commands": [
                    {
                        "id": 105,
                        "kind": "mode",
                        "payload": {
                            "mode": "boost",
                            "parameters": {
                                "load_limit_w": 65.0,
                                "electrolyzer_start_soc": 75.0,
                                "electrolyzer_stop_soc": 73.0,
                                "hydrogen_priority": True,
                                "fuel_cell_night_use": "allowed_at_night",
                            },
                        },
                    }
                ]
            },
            # Response for POST ack
            {"status": "ok"},
        ]

        self.plant.poll_and_apply_commands()

        self.assertEqual(self.plant.operating_mode, "boost")
        self.assertEqual(self.plant.load_limit_w, 65.0)
        self.assertEqual(self.plant.electrolyzer_start_soc, 75.0)
        self.assertEqual(self.plant.electrolyzer_stop_soc, 73.0)
        self.assertTrue(self.plant.hydrogen_priority)
        self.assertEqual(self.plant.fuel_cell_night_use, "allowed_at_night")
        self.assertEqual(self.plant.last_command_id, 105)

        # Verify ack call was made
        mock_http.assert_any_call("POST", "/api/simulink/commands/105/ack", {"status": "applied"})

    @patch.object(FakePlant, "_http_request")
    def test_poll_and_apply_electrolyzer_manual_command(self, mock_http: MagicMock) -> None:
        mock_http.side_effect = [
            {
                "commands": [
                    {
                        "id": 106,
                        "kind": "electrolyzer",
                        "payload": {
                            "control_mode": "manual",
                            "enabled": True,
                        },
                    }
                ]
            },
            {"status": "ok"},
        ]

        self.plant.poll_and_apply_commands()

        self.assertEqual(self.plant.manual_electrolyzer_override, "manual_on")
        self.assertTrue(self.plant.electrolyzer_enabled)
        mock_http.assert_any_call("POST", "/api/simulink/commands/106/ack", {"status": "applied"})

    @patch.object(FakePlant, "_http_request")
    def test_post_telemetry(self, mock_http: MagicMock) -> None:
        mock_http.return_value = {"status": "ok", "telemetry_id": 1}

        self.plant.post_telemetry()

        self.assertEqual(self.plant.sequence, 1)
        mock_http.assert_called_once()
        args, kwargs = mock_http.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/api/simulink/telemetry")
        self.assertIn("telemetry", args[2])
        self.assertEqual(args[2]["telemetry"]["operating_mode"], "balanced")

    @patch.object(FakePlant, "_http_request")
    def test_night_condition_fixed_irradiance(self, mock_http: MagicMock) -> None:
        night_plant = FakePlant(
            base_url="http://127.0.0.1:5000",
            fixed_irradiance=0.0,
            offline=True,
        )
        night_plant.fetch_inputs()
        mock_http.assert_not_called()
        self.assertEqual(night_plant.irradiance_w_m2, 0.0)

        # Step 1 hour: 100 W load at night drains battery
        initial_soc = night_plant.battery_soc
        night_plant.step_physical_model(3600.0)
        self.assertEqual(night_plant.solar_power_w, 0.0)
        self.assertLess(night_plant.battery_soc, initial_soc)


if __name__ == "__main__":
    unittest.main()
