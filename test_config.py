"""Unit tests for config.py."""

import math
import unittest

from config import MODES, AegisConfig, load_config, validate_number


class ConfigTests(unittest.TestCase):
    def test_default_config_matches_system_spec(self):
        cfg = load_config(env={}, overrides={})
        self.assertEqual(cfg.plant_lat, 13.1007)
        self.assertEqual(cfg.plant_lon, 77.5963)
        self.assertEqual(cfg.plant_city, "Yelahanka")
        self.assertEqual(cfg.load_nominal_w, 100.0)
        self.assertEqual(cfg.battery_hours, 18.0)
        self.assertEqual(cfg.battery_wh, 1800.0)
        self.assertEqual(cfg.lookahead_days, 10)
        self.assertEqual(cfg.bad_days_min, 2)
        self.assertEqual(cfg.performance_ratio, 0.80)
        self.assertEqual(cfg.decline_cooldown_hours, 6.0)
        self.assertEqual(cfg.proposal_ttl_minutes, 60.0)
        self.assertEqual(cfg.h2_boost_threshold_pct, 98.0)
        self.assertEqual(cfg.h2_return_target_pct, 90.0)
        self.assertEqual(cfg.min_load_ratio, 0.40)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 5000)

    def test_modes_table_contains_all_required_setpoints(self):
        self.assertEqual(set(MODES.keys()), {"balanced", "eco", "boost"})

        balanced = MODES["balanced"]
        self.assertEqual(balanced.load_limit_ratio, 1.0)
        self.assertEqual(balanced.electrolyzer_start_soc, 90.0)
        self.assertEqual(balanced.electrolyzer_stop_soc, 88.0)
        self.assertFalse(balanced.hydrogen_priority)
        self.assertEqual(balanced.fuel_cell_night_use, "only_if_battery_low")

        eco = MODES["eco"]
        self.assertEqual(eco.load_limit_ratio, 0.85)
        self.assertEqual(eco.electrolyzer_start_soc, 90.0)
        self.assertEqual(eco.electrolyzer_stop_soc, 88.0)
        self.assertFalse(eco.hydrogen_priority)
        self.assertEqual(eco.fuel_cell_night_use, "only_if_battery_low")

        boost = MODES["boost"]
        self.assertIsNone(boost.load_limit_ratio)  # Dynamically computed
        self.assertEqual(boost.electrolyzer_start_soc, 75.0)
        self.assertEqual(boost.electrolyzer_stop_soc, 73.0)
        self.assertTrue(boost.hydrogen_priority)
        self.assertEqual(boost.fuel_cell_night_use, "allowed_at_night")

    def test_environment_variable_overrides(self):
        env = {
            "AEGIS_PLANT_LAT": "12.9716",
            "AEGIS_PLANT_LON": "77.5946",
            "AEGIS_PLANT_CITY": "Bengaluru",
            "AEGIS_LOOKAHEAD_DAYS": "7",
            "AEGIS_BAD_DAYS_MIN": "3",
            "AEGIS_TOKEN": "secret-test-token",
            "AEGIS_CORS_ORIGINS": "http://localhost:3000, https://aegis.local",
            "AEGIS_HOST": "0.0.0.0",
            "AEGIS_PORT": "8080",
        }
        cfg = load_config(env=env)
        self.assertEqual(cfg.plant_lat, 12.9716)
        self.assertEqual(cfg.plant_lon, 77.5946)
        self.assertEqual(cfg.plant_city, "Bengaluru")
        self.assertEqual(cfg.lookahead_days, 7)
        self.assertEqual(cfg.bad_days_min, 3)
        self.assertEqual(cfg.aegis_token, "secret-test-token")
        self.assertEqual(cfg.cors_origins, ["http://localhost:3000", "https://aegis.local"])
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8080)

    def test_programmatic_overrides(self):
        overrides = {
            "load_nominal_w": 250.0,
            "battery_hours": 24.0,
            "DATABASE": ":memory:",
        }
        cfg = load_config(env={}, overrides=overrides)
        self.assertEqual(cfg.load_nominal_w, 250.0)
        self.assertEqual(cfg.battery_hours, 24.0)
        self.assertEqual(cfg.battery_wh, 6000.0)
        self.assertEqual(cfg.database_path, ":memory:")

    def test_validation_rejects_nan_and_infinity(self):
        with self.assertRaises(ValueError):
            validate_number(float("nan"), "test_nan")
        with self.assertRaises(ValueError):
            validate_number(float("inf"), "test_inf")
        with self.assertRaises(ValueError):
            validate_number(float("-inf"), "test_neg_inf")

    def test_validation_rejects_booleans_and_invalid_types(self):
        with self.assertRaises(ValueError):
            validate_number(True, "test_bool")
        with self.assertRaises(ValueError):
            validate_number("not-a-number", "test_str")

    def test_validation_rejects_out_of_bounds_values(self):
        with self.assertRaises(ValueError):
            AegisConfig(plant_lat=95.0).validate()
        with self.assertRaises(ValueError):
            AegisConfig(panel_efficiency=1.5).validate()
        with self.assertRaises(ValueError):
            AegisConfig(battery_hours=-2.0).validate()
        with self.assertRaises(ValueError):
            AegisConfig(lookahead_days=0).validate()
        with self.assertRaises(ValueError):
            AegisConfig(port=70000).validate()

    def test_to_dict_conversion_includes_modes_and_legacy_keys(self):
        cfg = load_config(env={"OPENWEATHER_API_KEY": "test-key"})
        d = cfg.to_dict()
        self.assertIn("MODES", d)
        self.assertIn("balanced", d["MODES"])
        self.assertEqual(d["OPENWEATHER_API_KEY"], "test-key")
        self.assertEqual(d["battery_wh"], 1800.0)
        self.assertEqual(d["DATABASE"], cfg.database_path)


if __name__ == "__main__":
    unittest.main()
