"""Pure-function decision engine for the AEGIS Solar-Hydrogen supervisory microgrid.

Evaluates 10-day weather outlooks, energy yields, battery state, and hydrogen storage
to formulate plain-language yes/no supervisory mode recommendations.
Adheres strictly to the decision lifecycle:
pending -> approved | declined | expired | withdrawn.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import MODES, AegisConfig, ModeSpec


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


@dataclass(frozen=True)
class DailyEnergy:
    """Calculated solar energy and deficit for a forecast day."""

    date: str
    shortwave_radiation_kwh_m2: float
    solar_wh: float
    load_wh: float
    deficit_wh: float
    is_low_sun: bool
    precipitation_sum_mm: float
    precipitation_probability_max: float


@dataclass(frozen=True)
class LowSunWindow:
    """A consecutive run of low-sun days."""

    start_day_idx: int
    length_days: int
    total_solar_wh: float
    total_deficit_wh: float


@dataclass
class Proposal:
    """Supervisory mode change proposal requiring user yes/no approval."""

    id: str
    type: str  # "mode_change"
    target_mode: str
    title: str
    question: str
    reasons: list[str]
    effects: list[str]
    suggested_load_limit_w: float
    created_at: str
    expires_at: str
    bad_days: int = 0
    h2_level_at_proposal: float = 0.0
    status: str = "pending"  # pending, approved, declined, expired, withdrawn
    status_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_daily_energy(
    daily_items: list[dict[str, Any]],
    config: AegisConfig,
) -> list[DailyEnergy]:
    """Compute solar energy yield and deficit for each forecast day."""
    nominal_daily_load_wh = config.load_nominal_w * 24.0
    low_sun_threshold_wh = config.low_sun_ratio * nominal_daily_load_wh

    results: list[DailyEnergy] = []
    for day in daily_items[:config.lookahead_days]:
        rad_kwh = float(day.get("shortwave_radiation_sum_kwh_m2", 0.0))
        precip = float(day.get("precipitation_sum_mm", 0.0))
        pop_max = float(day.get("precipitation_probability_max", 0.0))

        # solar_wh[d] = shortwave_radiation_sum[d] (kWh/m^2) * panel_area * efficiency * performance_ratio * 1000
        solar_wh = (
            rad_kwh
            * config.panel_area_m2
            * config.panel_efficiency
            * config.performance_ratio
            * 1000.0
        )
        is_low_sun = solar_wh < low_sun_threshold_wh
        deficit_wh = max(0.0, nominal_daily_load_wh - solar_wh)

        results.append(
            DailyEnergy(
                date=day.get("date", ""),
                shortwave_radiation_kwh_m2=rad_kwh,
                solar_wh=round(solar_wh, 1),
                load_wh=round(nominal_daily_load_wh, 1),
                deficit_wh=round(deficit_wh, 1),
                is_low_sun=is_low_sun,
                precipitation_sum_mm=precip,
                precipitation_probability_max=pop_max,
            )
        )
    return results


def find_consecutive_low_sun_runs(
    daily_energy: list[DailyEnergy],
    min_length: int = 2,
) -> list[LowSunWindow]:
    """Detect all runs of consecutive low-sun days meeting min_length."""
    runs: list[LowSunWindow] = []
    curr_start = None
    curr_len = 0
    curr_solar = 0.0
    curr_deficit = 0.0

    for i, d in enumerate(daily_energy):
        if d.is_low_sun:
            if curr_start is None:
                curr_start = i
            curr_len += 1
            curr_solar += d.solar_wh
            curr_deficit += d.deficit_wh
        else:
            if curr_start is not None:
                if curr_len >= min_length:
                    runs.append(
                        LowSunWindow(
                            start_day_idx=curr_start,
                            length_days=curr_len,
                            total_solar_wh=curr_solar,
                            total_deficit_wh=curr_deficit,
                        )
                    )
                curr_start = None
                curr_len = 0
                curr_solar = 0.0
                curr_deficit = 0.0

    if curr_start is not None and curr_len >= min_length:
        runs.append(
            LowSunWindow(
                start_day_idx=curr_start,
                length_days=curr_len,
                total_solar_wh=curr_solar,
                total_deficit_wh=curr_deficit,
            )
        )

    return runs


def calculate_sustainable_load(
    battery_soc: float,
    h2_level: float,
    bad_window_solar_wh: float,
    window_days: int,
    config: AegisConfig,
) -> float:
    """Compute sustainable load limit in Watts clamped to [min_load_ratio, 1.0] * load_nominal_w."""
    tank_wh_now = (max(0.0, min(100.0, h2_level)) / 100.0) * config.h2_tank_wh
    battery_wh_now = (max(0.0, min(100.0, battery_soc)) / 100.0) * config.battery_wh
    total_hours = max(24.0, float(window_days * 24))

    available_energy = tank_wh_now + battery_wh_now + bad_window_solar_wh
    sustainable_w = available_energy / total_hours

    # Clamp to [min_load_ratio, 1.0] * load_nominal_w
    min_w = config.min_load_ratio * config.load_nominal_w
    max_w = config.load_nominal_w
    clamped = max(min_w, min(max_w, sustainable_w))
    return round(clamped, 0)


def get_mode_parameters(
    mode_name: str,
    suggested_load_limit_w: float | None,
    config: AegisConfig,
) -> dict[str, Any]:
    """Retrieve full setpoint parameters payload for a given mode."""
    mode_spec = config.modes.get(mode_name, MODES["balanced"])

    if mode_name == "boost":
        load_limit = suggested_load_limit_w or (config.load_nominal_w * config.min_load_ratio)
    elif mode_spec.load_limit_ratio is not None:
        load_limit = config.load_nominal_w * mode_spec.load_limit_ratio
    else:
        load_limit = config.load_nominal_w

    return {
        "load_limit_w": round(load_limit, 1),
        "electrolyzer_start_soc": mode_spec.electrolyzer_start_soc,
        "electrolyzer_stop_soc": mode_spec.electrolyzer_stop_soc,
        "hydrogen_priority": mode_spec.hydrogen_priority,
        "fuel_cell_night_use": mode_spec.fuel_cell_night_use,
    }


def is_in_decline_cooldown(
    target_mode: str,
    last_declined: dict[str, Any] | None,
    curr_bad_days: int,
    curr_h2_level: float,
    config: AegisConfig,
    now: datetime,
) -> bool:
    """Check if re-proposing target_mode is prevented by decline cooldown,
    unless situation has worsened materially.
    """
    if not last_declined or last_declined.get("target_mode") != target_mode:
        return False

    declined_at_str = last_declined.get("declined_at")
    if not declined_at_str:
        return False

    try:
        declined_at = datetime.fromisoformat(declined_at_str.replace("Z", "+00:00"))
    except ValueError:
        return False

    elapsed_hours = (now - declined_at).total_seconds() / 3600.0
    if elapsed_hours >= config.decline_cooldown_hours:
        return False

    # Check if situation has worsened materially:
    # 1. Bad window became longer
    declined_bad_days = int(last_declined.get("bad_days", 0))
    if curr_bad_days > declined_bad_days:
        return False  # Worsened materially -> can re-propose

    # 2. Hydrogen tank dropped materially (> 10% lower than when declined)
    declined_h2 = float(last_declined.get("h2_level", 100.0))
    if (declined_h2 - curr_h2_level) >= 10.0:
        return False  # Worsened materially -> can re-propose

    return True  # Still in cooldown


def evaluate_policy(
    current_mode: str,
    mode_control: str,
    battery_soc: float | None,
    h2_level: float | None,
    daily_outlook: list[dict[str, Any]],
    current_weather: dict[str, Any] | None,
    config: AegisConfig,
    active_proposal: dict[str, Any] | None = None,
    last_declined: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate energy forecasts and plant state to generate or update proposals.
    
    Returns dict:
      {
        "proposal": Proposal or None,
        "action": "create" | "retain" | "withdraw" | "none",
        "reason": str,
      }
    """
    if now is None:
        now = utc_now()

    # Rule: If operator is in manual mode, no new proposals are made.
    if mode_control == "manual":
        if active_proposal and active_proposal.get("status") == "pending":
            return {"proposal": None, "action": "withdraw", "reason": "Operator switched to manual mode."}
        return {"proposal": None, "action": "none", "reason": "System is in manual mode."}

    # Telemetry gate: cannot propose without valid tank and battery levels
    if battery_soc is None or h2_level is None:
        return {"proposal": None, "action": "none", "reason": "Telemetry is not yet available."}

    # 1. Compute daily energy yields from outlook
    daily_energy = compute_daily_energy(daily_outlook, config)
    if not daily_energy:
        return {"proposal": None, "action": "none", "reason": "No forecast days available."}

    bad_runs = find_consecutive_low_sun_runs(daily_energy, min_length=config.bad_days_min)
    has_qualifying_bad_run = len(bad_runs) > 0
    longest_bad_run = max(bad_runs, key=lambda r: r.length_days) if bad_runs else None

    # Rain indicators
    has_high_rain_day = any(d.precipitation_probability_max >= config.eco_rain_prob_pct for d in daily_energy[:3])
    mild_low_sun_day = any(d.is_low_sun for d in daily_energy[:3])

    # -------------------------------------------------------------
    # Case A: Check conditions for BOOST proposal
    # -------------------------------------------------------------
    # Boost requires:
    # 1. A run of >= bad_days_min consecutive low-sun days
    # 2. Hydrogen tank < h2_boost_threshold_pct (98%)
    # 3. Current mode is not already boost
    if has_qualifying_bad_run and h2_level < config.h2_boost_threshold_pct and current_mode != "boost":
        run = longest_bad_run
        assert run is not None

        # Check decline cooldown
        if not is_in_decline_cooldown("boost", last_declined, run.length_days, h2_level, config, now):
            sustainable_w = calculate_sustainable_load(battery_soc, h2_level, run.total_solar_wh, run.length_days, config)

            proposal = Proposal(
                id=str(uuid.uuid4()),
                type="mode_change",
                target_mode="boost",
                title="Prolonged low sun forecast",
                question=(
                    f"A stretch of {run.length_days} low-sun days is forecast and the hydrogen tank is at {h2_level:.0f}%. "
                    f"Switch to Boost mode to save energy and fill the tank?"
                ),
                reasons=[
                    f"{run.length_days} consecutive low-sun days detected starting in {run.start_day_idx} days",
                    f"Hydrogen reserve is at {h2_level:.0f}% (below {config.h2_boost_threshold_pct:.0f}% target)",
                    f"Daily solar generation will fall short of nominal {config.load_nominal_w:.0f} W load",
                ],
                effects=[
                    f"Load limited to {sustainable_w:.0f} W (normally {config.load_nominal_w:.0f} W)",
                    "Hydrogen production starts earlier (75% battery charge instead of 90%)",
                    "You can switch back any time",
                ],
                suggested_load_limit_w=sustainable_w,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=config.proposal_ttl_minutes)).isoformat(),
                bad_days=run.length_days,
                h2_level_at_proposal=round(h2_level, 1),
                status="pending",
            )
            return {"proposal": proposal, "action": "create", "reason": "Boost conditions satisfied."}

    # -------------------------------------------------------------
    # Case B: Check conditions for ECO proposal
    # -------------------------------------------------------------
    # Eco requires:
    # 1. Milder bad weather (short run < bad_days_min, or high rain probability for 1 day)
    # 2. Not already qualifying for Boost
    # 3. Current mode is balanced
    if (
        not has_qualifying_bad_run
        and (has_high_rain_day or mild_low_sun_day)
        and current_mode == "balanced"
    ):
        if not is_in_decline_cooldown("eco", last_declined, 1, h2_level, config, now):
            eco_load = round(config.load_nominal_w * 0.85, 0)
            proposal = Proposal(
                id=str(uuid.uuid4()),
                type="mode_change",
                target_mode="eco",
                title="Rain forecast ahead",
                question="Rain is forecast soon and solar output will drop. Switch to Eco mode to conserve stored power?",
                reasons=[
                    "Moderate rain or reduced solar generation forecast in the next 3 days",
                    "Battery charge should be conserved as caution",
                ],
                effects=[
                    f"Load limited to {eco_load:.0f} W (normally {config.load_nominal_w:.0f} W)",
                    "Prioritizes charging the battery",
                    "You can switch back any time",
                ],
                suggested_load_limit_w=eco_load,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=config.proposal_ttl_minutes)).isoformat(),
                bad_days=1,
                h2_level_at_proposal=round(h2_level, 1),
                status="pending",
            )
            return {"proposal": proposal, "action": "create", "reason": "Eco conditions satisfied."}

    # -------------------------------------------------------------
    # Case C: Check conditions for RETURN TO BALANCED proposal
    # -------------------------------------------------------------
    # Return to Balanced requires:
    # 1. Current mode is boost or eco
    # 2. No qualifying bad window exists in forecast
    # 3. Hydrogen tank is at or above h2_return_target_pct (90%)
    if (
        current_mode in ("boost", "eco")
        and not has_qualifying_bad_run
        and not has_high_rain_day
        and h2_level >= config.h2_return_target_pct
    ):
        if not is_in_decline_cooldown("balanced", last_declined, 0, h2_level, config, now):
            proposal = Proposal(
                id=str(uuid.uuid4()),
                type="mode_change",
                target_mode="balanced",
                title="Clear weather returning",
                question=f"The forecast has cleared and the hydrogen tank is at {h2_level:.0f}%. Return to Balanced mode?",
                reasons=[
                    "No prolonged bad weather forecast in the outlook window",
                    f"Hydrogen tank is healthy at {h2_level:.0f}% (target: {config.h2_return_target_pct:.0f}%)",
                ],
                effects=[
                    f"Load returns to 100% nominal ({config.load_nominal_w:.0f} W)",
                    "Normal day solar / night battery operation resumes",
                    "You can switch back any time",
                ],
                suggested_load_limit_w=config.load_nominal_w,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=config.proposal_ttl_minutes)).isoformat(),
                bad_days=0,
                h2_level_at_proposal=round(h2_level, 1),
                status="pending",
            )
            return {"proposal": proposal, "action": "create", "reason": "Return to Balanced conditions satisfied."}

    # -------------------------------------------------------------
    # Case D: Check if active proposal should be WITHDRAWN
    # -------------------------------------------------------------
    if active_proposal and active_proposal.get("status") == "pending":
        target = active_proposal.get("target_mode")
        should_withdraw = False
        withdraw_reason = ""

        # Check expiry
        exp_str = active_proposal.get("expires_at")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                if now >= exp_dt:
                    return {"proposal": None, "action": "expire", "reason": "Proposal expired."}
            except ValueError:
                pass

        if target == "boost":
            if not has_qualifying_bad_run:
                should_withdraw = True
                withdraw_reason = "Bad weather conditions in forecast cleared."
            elif h2_level >= config.h2_boost_threshold_pct:
                should_withdraw = True
                withdraw_reason = f"Hydrogen tank reached {h2_level:.0f}%."
            elif current_mode == "boost":
                should_withdraw = True
                withdraw_reason = "System already switched to Boost mode."

        elif target == "eco":
            if not (has_high_rain_day or mild_low_sun_day):
                should_withdraw = True
                withdraw_reason = "Weather forecast cleared."
            elif current_mode != "balanced":
                should_withdraw = True
                withdraw_reason = f"Current mode is now {current_mode}."

        elif target == "balanced":
            if has_qualifying_bad_run or has_high_rain_day:
                should_withdraw = True
                withdraw_reason = "Forecast worsened with new bad weather."
            elif h2_level < config.h2_return_target_pct:
                should_withdraw = True
                withdraw_reason = f"Hydrogen tank decreased below {config.h2_return_target_pct:.0f}%."
            elif current_mode == "balanced":
                should_withdraw = True
                withdraw_reason = "System is already in Balanced mode."

        if should_withdraw:
            return {"proposal": None, "action": "withdraw", "reason": withdraw_reason}

        return {"proposal": None, "action": "retain", "reason": "Active proposal remains valid."}

    return {"proposal": None, "action": "none", "reason": "No policy change required."}
