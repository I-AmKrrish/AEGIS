"""Simulink Plant Simulator (fake_plant.py).

Mimics the physical behavior of the Solar-Hydrogen Microgrid modelled in Simulink:
  - Polls inputs (GET /api/simulink/inputs) to fetch real-time irradiance.
  - Polls setpoints (GET /api/simulink/setpoints) and commands (GET /api/simulink/commands).
  - Applies mode setpoints and manual overrides, acknowledging commands (POST .../ack).
  - Simulates physical components: solar panel gain block, nominal load, battery backup (18 h),
    electrolyzer hydrogen generation, hydrogen storage tank, and fuel cell backup.
  - Posts real-time telemetry (POST /api/simulink/telemetry).
  - Supports time acceleration (--accel) for fast automated verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


class FakePlant:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5000",
        token: str = "",
        accel: float = 1.0,
        load_nominal_w: float = 100.0,
        battery_hours: float = 18.0,
        h2_tank_wh: float = 2400.0,
        initial_battery_soc: float = 60.0,
        initial_h2: float = 50.0,
        panel_area_m2: float = 1.0,
        panel_efficiency: float = 0.20,
        fixed_irradiance: float | None = None,
        offline: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.accel = max(0.1, accel)
        self.fixed_irradiance = fixed_irradiance
        self.offline = offline

        # Physical constants
        self.load_nominal_w = load_nominal_w
        self.battery_hours = battery_hours
        self.battery_wh = load_nominal_w * battery_hours
        self.h2_tank_wh = h2_tank_wh
        self.panel_area_m2 = panel_area_m2
        self.panel_efficiency = panel_efficiency

        # Dynamic state variables
        self.battery_soc: float = initial_battery_soc  # %
        self.h2_level: float = initial_h2  # %
        self.solar_power_w: float = 0.0
        self.load_power_w: float = load_nominal_w
        self.irradiance_w_m2: float = 0.0
        self.operating_mode: str = "balanced"
        self.electrolyzer_enabled: bool = False
        self.fuel_cell_active: bool = False

        # Active supervisory setpoints from Flask
        self.load_limit_w: float = load_nominal_w
        self.electrolyzer_start_soc: float = 90.0
        self.electrolyzer_stop_soc: float = 88.0
        self.hydrogen_priority: bool = False
        self.fuel_cell_night_use: str = "only_if_battery_low"
        self.manual_electrolyzer_override: str = "auto"  # "auto", "manual_on", "manual_off"

        self.last_command_id: int = 0
        self.sequence: int = 0

    def _http_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "X-Device-Id": "simulink-fake-plant",
        }
        if self.token:
            headers["X-AEGIS-Token"] = self.token

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_data = {"error": f"HTTP {e.code}"}
            print(f"[ERROR] {method} {path} returned HTTP {e.code}: {err_data}")
            return None
        except (URLError, TimeoutError) as e:
            print(f"[WARN] {method} {path} connection failed: {e}")
            return None

    def fetch_inputs(self) -> None:
        """Poll /api/simulink/inputs to get current irradiance, unless fixed."""
        if self.fixed_irradiance is not None:
            self.irradiance_w_m2 = float(self.fixed_irradiance)
            return
        if self.offline:
            return
        res = self._http_request("GET", "/api/simulink/inputs")
        if res and "irradiance_w_m2" in res:
            self.irradiance_w_m2 = float(res["irradiance_w_m2"])

    def fetch_setpoints(self) -> None:
        """Poll /api/simulink/setpoints to recover supervisory parameters."""
        if self.offline:
            return
        res = self._http_request("GET", "/api/simulink/setpoints")
        if res and "parameters" in res:
            p = res["parameters"]
            self.operating_mode = res.get("mode", self.operating_mode)
            self.load_limit_w = float(p.get("load_limit_w", self.load_limit_w))
            self.electrolyzer_start_soc = float(p.get("electrolyzer_start_soc", self.electrolyzer_start_soc))
            self.electrolyzer_stop_soc = float(p.get("electrolyzer_stop_soc", self.electrolyzer_stop_soc))
            self.hydrogen_priority = bool(p.get("hydrogen_priority", self.hydrogen_priority))
            self.fuel_cell_night_use = str(p.get("fuel_cell_night_use", self.fuel_cell_night_use))

    def poll_and_apply_commands(self) -> None:
        """Poll /api/simulink/commands and acknowledge applied actions."""
        if self.offline:
            return
        res = self._http_request("GET", f"/api/simulink/commands?after={self.last_command_id}")
        if not res or "commands" not in res:
            return

        for cmd in res["commands"]:
            cmd_id = cmd["id"]
            kind = cmd["kind"]
            payload = cmd.get("payload", {})
            self.last_command_id = max(self.last_command_id, cmd_id)

            if kind == "mode":
                new_mode = payload.get("mode", self.operating_mode)
                p = payload.get("parameters", {})
                self.operating_mode = new_mode
                self.load_limit_w = float(p.get("load_limit_w", self.load_limit_w))
                self.electrolyzer_start_soc = float(p.get("electrolyzer_start_soc", self.electrolyzer_start_soc))
                self.electrolyzer_stop_soc = float(p.get("electrolyzer_stop_soc", self.electrolyzer_stop_soc))
                self.hydrogen_priority = bool(p.get("hydrogen_priority", self.hydrogen_priority))
                self.fuel_cell_night_use = str(p.get("fuel_cell_night_use", self.fuel_cell_night_use))

                print(f"[CMD] Applied mode command {cmd_id}: mode={new_mode}, load_limit={self.load_limit_w:.1f}W")
                self.acknowledge_command(cmd_id, "applied")

            elif kind == "electrolyzer":
                ctrl = payload.get("control_mode", "auto")
                if ctrl == "manual":
                    enabled = bool(payload.get("enabled", False))
                    self.manual_electrolyzer_override = "manual_on" if enabled else "manual_off"
                    self.electrolyzer_enabled = enabled
                else:
                    self.manual_electrolyzer_override = "auto"

                print(f"[CMD] Applied electrolyzer command {cmd_id}: override={self.manual_electrolyzer_override}")
                self.acknowledge_command(cmd_id, "applied")

    def acknowledge_command(self, cmd_id: int, status: str) -> None:
        self._http_request("POST", f"/api/simulink/commands/{cmd_id}/ack", {"status": status})

    def step_physical_model(self, dt_seconds: float) -> None:
        """Simulate microgrid electrical dynamics for dt_seconds."""
        dt_hours = dt_seconds / 3600.0

        # Solar power = Irradiance * Panel Area * Efficiency
        self.solar_power_w = round(
            self.irradiance_w_m2 * self.panel_area_m2 * self.panel_efficiency, 1
        )

        # Load takes min(nominal_load, active_load_limit_w)
        target_load = min(self.load_nominal_w, self.load_limit_w)
        self.load_power_w = target_load

        # Electrolyzer logic (internal Simulink hysteresis 90/88 or mode-directed 75/73)
        if self.manual_electrolyzer_override == "manual_on":
            self.electrolyzer_enabled = True
        elif self.manual_electrolyzer_override == "manual_off":
            self.electrolyzer_enabled = False
        else:
            if self.battery_soc >= self.electrolyzer_start_soc:
                self.electrolyzer_enabled = True
            elif self.battery_soc <= self.electrolyzer_stop_soc:
                self.electrolyzer_enabled = False

        net_power = self.solar_power_w - self.load_power_w
        self.fuel_cell_active = False

        if net_power >= 0:
            # Power surplus: charges battery or produces hydrogen
            surplus_wh = net_power * dt_hours

            if self.electrolyzer_enabled:
                # If battery is full or electrolyzer priority, surplus energy produces H2 (70% efficiency)
                h2_energy_gain = surplus_wh * 0.70
                delta_h2_pct = (h2_energy_gain / self.h2_tank_wh) * 100.0
                self.h2_level = min(100.0, self.h2_level + delta_h2_pct)
            else:
                # Surplus charges battery
                delta_bat_pct = (surplus_wh / self.battery_wh) * 100.0
                self.battery_soc = min(100.0, self.battery_soc + delta_bat_pct)

        else:
            # Power deficit: supplied by battery or fuel cell
            deficit_wh = abs(net_power) * dt_hours

            # Fuel cell logic: allowed at night if in Boost or if battery low (<20%)
            use_fuel_cell = False
            if self.battery_soc <= 20.0 and self.h2_level > 5.0:
                use_fuel_cell = True
            elif self.fuel_cell_night_use == "allowed_at_night" and self.solar_power_w < 5.0 and self.h2_level > 10.0:
                use_fuel_cell = True

            if use_fuel_cell:
                self.fuel_cell_active = True
                # Fuel cell converts stored H2 to electricity (50% efficiency)
                h2_energy_used = deficit_wh / 0.50
                delta_h2_pct = (h2_energy_used / self.h2_tank_wh) * 100.0
                self.h2_level = max(0.0, self.h2_level - delta_h2_pct)
            else:
                # Battery supplies the deficit
                delta_bat_pct = (deficit_wh / self.battery_wh) * 100.0
                self.battery_soc = max(0.0, self.battery_soc - delta_bat_pct)

    def post_telemetry(self) -> None:
        """Post telemetry snapshot to /api/simulink/telemetry."""
        self.sequence += 1
        if self.offline:
            return
        payload = {
            "telemetry": {
                "battery_soc": round(self.battery_soc, 1),
                "h2_level": round(self.h2_level, 1),
                "irradiance_w_m2": round(self.irradiance_w_m2, 1),
                "solar_power_w": round(self.solar_power_w, 1),
                "load_power_w": round(self.load_power_w, 1),
                "operating_mode": self.operating_mode,
                "electrolyzer_enabled": self.electrolyzer_enabled,
                "sequence": self.sequence,
            }
        }
        self._http_request("POST", "/api/simulink/telemetry", payload)

    def run_loop(self, duration_s: float | None = None) -> None:
        """Main simulator loop executing with time acceleration."""
        print(f"=== AEGIS Simulink Fake Plant Started ===")
        if self.offline:
            print(f"Mode: Standalone / Offline Simulation (no HTTP backend required)")
        else:
            print(f"Target URL: {self.base_url}")
        if self.fixed_irradiance is not None:
            if self.fixed_irradiance == 0.0:
                print(f"Condition: Forced NIGHT TIME (Irradiance = 0.0 W/m2, Solar = 0 W)")
            else:
                print(f"Condition: Fixed Irradiance = {self.fixed_irradiance:.1f} W/m2")
        print(f"Acceleration: {self.accel:.1f}x (1 real sec = {self.accel:.1f} sim sec)")
        print(f"Nominal Load: {self.load_nominal_w} W, Battery: {self.battery_hours} h ({self.battery_wh:.0f} Wh), Tank: {self.h2_tank_wh:.0f} Wh")
        if not self.offline:
            print("Polling setpoints...")
        self.fetch_setpoints()

        step_real_sec = 1.0
        elapsed_real = 0.0

        try:
            while True:
                t0 = time.time()
                dt_sim = step_real_sec * self.accel

                # 1. Fetch real-time irradiance input from Flask
                self.fetch_inputs()

                # 2. Check and apply commands from Flask
                self.poll_and_apply_commands()

                # 3. Simulate physical microgrid components
                self.step_physical_model(dt_sim)

                # 4. Report telemetry
                self.post_telemetry()

                # Print status summary
                print(
                    f"[{time.strftime('%H:%M:%S')}] Mode: {self.operating_mode:<8} | "
                    f"Irr: {self.irradiance_w_m2:5.1f} W/m2 | "
                    f"Solar: {self.solar_power_w:5.1f} W | "
                    f"Load: {self.load_power_w:5.1f} W | "
                    f"Batt: {self.battery_soc:4.1f}% | "
                    f"H2: {self.h2_level:4.1f}% | "
                    f"Electrolyzer: {'ON ' if self.electrolyzer_enabled else 'OFF'} | "
                    f"FC: {'ON ' if self.fuel_cell_active else 'OFF'}"
                )

                elapsed_real += step_real_sec
                if duration_s is not None and elapsed_real >= duration_s:
                    print(f"Simulation duration of {duration_s:.1f}s reached.")
                    break

                sleep_time = max(0.01, step_real_sec - (time.time() - t0))
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nSimulation stopped by user.")


def main() -> None:
    parser = argparse.ArgumentParser(description="AEGIS Simulink Fake Plant Simulator")
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="Flask server URL")
    parser.add_argument("--token", default=os.environ.get("AEGIS_TOKEN", os.environ.get("AEGIS_DEVICE_TOKEN", "")), help="X-AEGIS-Token header")
    parser.add_argument("--accel", type=float, default=1.0, help="Time acceleration multiplier (e.g. 60.0)")
    parser.add_argument("--duration", type=float, default=None, help="Duration in seconds (default runs indefinitely)")
    parser.add_argument("--battery-soc", type=float, default=60.0, help="Initial battery SOC %")
    parser.add_argument("--h2-level", type=float, default=50.0, help="Initial hydrogen level %")
    parser.add_argument("--nominal-load", type=float, default=100.0, help="Nominal load in Watts")
    parser.add_argument("--night", action="store_true", help="Simulate night time condition (irradiance = 0 W/m2)")
    parser.add_argument("--irradiance", type=float, default=None, help="Explicit irradiance override in W/m2")
    parser.add_argument("--offline", action="store_true", help="Run standalone without contacting the AEGIS web server")

    args = parser.parse_args()
    irr_override = 0.0 if args.night else args.irradiance
    plant = FakePlant(
        base_url=args.url,
        token=args.token,
        accel=args.accel,
        initial_battery_soc=args.battery_soc,
        initial_h2=args.h2_level,
        load_nominal_w=args.nominal_load,
        fixed_irradiance=irr_override,
        offline=args.offline,
    )
    plant.run_loop(duration_s=args.duration)


if __name__ == "__main__":
    main()
