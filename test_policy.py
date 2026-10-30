"""Unit tests for Phase 3: policy.py decision engine."""

import unittest
from datetime import datetime, timedelta, timezone

from config import AegisConfig, load_config
from policy import (
    DailyEnergy,
    LowSunWindow,
    Proposal,
    calculate_sustainable_load,
    compute_daily_energy,
    evaluate_policy,
    find_consecutive_low_sun_runs,
    get_mode_parameters,
    is_in_decline_cooldown,
    utc_now,
)


class PolicyEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(env={}, overrides={})

    def test_compute_daily_energy_and_low_sun(self):
        # 100 W nominal load * 24 h = 2400 Wh daily load.
        # low_sun_ratio is 0.6 -> low-sun threshold is 1440 Wh.
        # Panel: 1 m^2, 20% eff, 0.8 PR -> multiplier = 1 * 0.2 * 0.8 * 1000 = 160 Wh per (kWh/m^2).
        daily = [
            {"date": "2026-11-01", "shortwave_radiation_sum_kwh_m2": 10.0},  # 1600 Wh -> normal
            {"date": "2026-11-02", "shortwave_radiation_sum_kwh_m2": 5.0},   # 800 Wh -> low sun
        ]
        results = compute_daily_energy(daily, self.config)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].solar_wh, 1600.0)
        self.assertFalse(results[0].is_low_sun)
        self.assertEqual(results[0].deficit_wh, 800.0)

        self.assertEqual(results[1].solar_wh, 800.0)
        self.assertTrue(results[1].is_low_sun)
        self.assertEqual(results[1].deficit_wh, 1600.0)

    def test_find_consecutive_low_sun_runs(self):
        daily = [
            DailyEnergy("2026-11-01", 10.0, 1600, 2400, 800, False, 0, 0),
            DailyEnergy("2026-11-02", 4.0, 640, 2400, 1760, True, 5, 80),
            DailyEnergy("2026-11-03", 3.0, 480, 2400, 1920, True, 10, 90),
            DailyEnergy("2026-11-04", 5.0, 800, 2400, 1600, True, 2, 70),
            DailyEnergy("2026-11-05", 10.0, 1600, 2400, 800, False, 0, 10),
        ]
        runs = find_consecutive_low_sun_runs(daily, min_length=2)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.start_day_idx, 1)
        self.assertEqual(run.length_days, 3)
        self.assertEqual(run.total_solar_wh, 640 + 480 + 800)

    def test_calculate_sustainable_load(self):
        # Battery Wh = 100 W * 18 h = 1800 Wh. 50% = 900 Wh.
        # Tank Wh = 2400 Wh. 60% = 1440 Wh.
        # Solar over 3 days = 1920 Wh.
        # Total energy = 900 + 1440 + 1920 = 4260 Wh.
        # Hours = 3 * 24 = 72 h.
        # Sustainable = 4260 / 72 = 59.16 W -> round to 59 W.
        # Min load clamp = 0.4 * 100 = 40 W. Max = 100 W.
        load = calculate_sustainable_load(
            battery_soc=50.0,
            h2_level=60.0,
            bad_window_solar_wh=1920.0,
            window_days=3,
            config=self.config,
        )
        self.assertEqual(load, 59.0)

    def test_calculate_sustainable_load_clamping(self):
        # Test lower clamp at 40% (40 W)
        low_load = calculate_sustainable_load(
            battery_soc=5.0,
            h2_level=5.0,
            bad_window_solar_wh=100.0,
            window_days=5,
            config=self.config,
        )
        self.assertEqual(low_load, 40.0)

        # Test upper clamp at 100% (100 W)
        high_load = calculate_sustainable_load(
            battery_soc=100.0,
            h2_level=100.0,
            bad_window_solar_wh=10000.0,
            window_days=1,
            config=self.config,
        )
        self.assertEqual(high_load, 100.0)

    def test_propose_boost_scenario(self):
        # 3 low sun days, tank 60%, mode balanced -> Propose Boost
        outlook = [
            {"date": f"2026-11-0{i+1}", "shortwave_radiation_sum_kwh_m2": 3.0, "precipitation_sum_mm": 5.0, "precipitation_probability_max": 80}
            for i in range(5)
        ]
        res = evaluate_policy(
            current_mode="balanced",
            mode_control="auto",
            battery_soc=60.0,
            h2_level=60.0,
            daily_outlook=outlook,
            current_weather={},
            config=self.config,
        )
        self.assertEqual(res["action"], "create")
        proposal: Proposal = res["proposal"]
        self.assertEqual(proposal.target_mode, "boost")
        self.assertIn("Boost", proposal.question)
        self.assertTrue(len(proposal.effects) >= 2)
        self.assertGreaterEqual(proposal.suggested_load_limit_w, 40.0)

    def test_no_boost_when_tank_full(self):
        # Tank at 99% (>= 98% threshold) -> No Boost proposal despite low sun
        outlook = [
            {"date": f"2026-11-0{i+1}", "shortwave_radiation_sum_kwh_m2": 2.0, "precipitation_sum_mm": 10.0, "precipitation_probability_max": 90}
            for i in range(5)
        ]
        res = evaluate_policy(
            current_mode="balanced",
            mode_control="auto",
            battery_soc=90.0,
            h2_level=99.0,
            daily_outlook=outlook,
            current_weather={},
            config=self.config,
        )
        # Does not propose Boost because tank >= 98%
        if res["action"] == "create":
            self.assertNotEqual(res["proposal"].target_mode, "boost")

    def test_propose_eco_on_mild_weather(self):
        # 1 day of high rain, then clear -> Eco proposed from balanced
        outlook = [
            {"date": "2026-11-01", "shortwave_radiation_sum_kwh_m2": 8.0, "precipitation_sum_mm": 15.0, "precipitation_probability_max": 85},
            {"date": "2026-11-02", "shortwave_radiation_sum_kwh_m2": 12.0, "precipitation_sum_mm": 0.0, "precipitation_probability_max": 10},
        ]
        res = evaluate_policy(
            current_mode="balanced",
            mode_control="auto",
            battery_soc=70.0,
            h2_level=75.0,
            daily_outlook=outlook,
            current_weather={},
            config=self.config,
        )
        self.assertEqual(res["action"], "create")
        self.assertEqual(res["proposal"].target_mode, "eco")

    def test_propose_return_to_balanced(self):
        # Currently in Boost, clear forecast, tank >= 90% -> Return to Balanced
        outlook = [
            {"date": f"2026-11-0{i+1}", "shortwave_radiation_sum_kwh_m2": 12.0, "precipitation_sum_mm": 0.0, "precipitation_probability_max": 5}
            for i in range(5)
        ]
        res = evaluate_policy(
            current_mode="boost",
            mode_control="auto",
            battery_soc=80.0,
            h2_level=92.0,
            daily_outlook=outlook,
            current_weather={},
            config=self.config,
        )
        self.assertEqual(res["action"], "create")
        self.assertEqual(res["proposal"].target_mode, "balanced")

    def test_decline_cooldown_prevents_repeat(self):
        now = utc_now()
        last_declined = {
            "target_mode": "boost",
            "declined_at": (now - timedelta(hours=2)).isoformat(),
            "bad_days": 3,
            "h2_level": 60.0,
        }
        in_cooldown = is_in_decline_cooldown(
            target_mode="boost",
            last_declined=last_declined,
            curr_bad_days=3,
            curr_h2_level=60.0,
            config=self.config,
            now=now,
        )
        self.assertTrue(in_cooldown)

    def test_decline_cooldown_bypassed_when_worsened_materially(self):
        now = utc_now()
        last_declined = {
            "target_mode": "boost",
            "declined_at": (now - timedelta(hours=2)).isoformat(),
            "bad_days": 3,
            "h2_level": 60.0,
        }
        # Situation worsened: bad window increased to 5 days
        in_cooldown_days = is_in_decline_cooldown(
            target_mode="boost",
            last_declined=last_declined,
            curr_bad_days=5,
            curr_h2_level=60.0,
            config=self.config,
            now=now,
        )
        self.assertFalse(in_cooldown_days)

        # Situation worsened: hydrogen tank dropped by 15% (to 45%)
        in_cooldown_tank = is_in_decline_cooldown(
            target_mode="boost",
            last_declined=last_declined,
            curr_bad_days=3,
            curr_h2_level=45.0,
            config=self.config,
            now=now,
        )
        self.assertFalse(in_cooldown_tank)

    def test_proposal_withdrawal_when_forecast_clears(self):
        active_prop = {
            "id": "test-prop-id",
            "target_mode": "boost",
            "status": "pending",
            "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
        }
        # Forecast is now completely sunny
        sunny_outlook = [
            {"date": f"2026-11-0{i+1}", "shortwave_radiation_sum_kwh_m2": 15.0, "precipitation_sum_mm": 0.0, "precipitation_probability_max": 0}
            for i in range(5)
        ]
        res = evaluate_policy(
            current_mode="balanced",
            mode_control="auto",
            battery_soc=80.0,
            h2_level=60.0,
            daily_outlook=sunny_outlook,
            current_weather={},
            config=self.config,
            active_proposal=active_prop,
        )
        self.assertEqual(res["action"], "withdraw")


if __name__ == "__main__":
    unittest.main()
