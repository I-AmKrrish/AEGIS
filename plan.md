# AEGIS – PLAN.md (Phase 0: Audit & Execution Plan)

## 0. Executive Summary & Ground Rules Alignment
This document establishes the verified baseline, gap analysis, contradictions, assumptions, architectural design, and phased execution plan for completing the AEGIS Solar-Hydrogen supervisory system.

**Ground Rules strictly adhered to:**
- No code modifications in Phase 0; coding begins only after review of this plan.
- All tunable parameters consolidated in `config.py`, overridable by environment variables. No magic numbers.
- Simulink owns safety interlocks, battery/electrolyzer hysteresis (90%/88%), and control logic. Flask supplies inputs (irradiance), proposes supervisory mode setpoints upon user approval, and dispatches approved commands.
- Security: API keys and auth tokens come from environment variables only. No secrets in git.
- Visual styling of `index.html` is preserved; only required functional UI additions (Phase 5) will be made.
- Strict test verification: `python -m unittest -v` executed and clean after every phase.

---

## 1. Verified Current State (Full Audit)

### 1.1 `app.py`
- **Structure**: Single Flask application using SQLite (`app_state`, `weather_samples`, `telemetry_samples`, `commands`). One global reentrant lock (`threading.RLock`).
- **Critical Flaw - Side Effects on GET**: Both `GET /api/state` and `GET /api/simulink/commands` call `assess_mode()` and `evaluate_electrolyzer()`, generating alerts and queuing commands as a side-effect of UI polling every 2 seconds.
- **Mode Logic**: Only checks short-term 6-hour OpenWeather POP/rain. If rain > 0 or POP >= 60%, automatically sets mode to `eco` after a 45-second notice timer (`MODE_NOTICE_SECONDS`).
- **Electrolyzer Logic**: Server implements 90%/88% battery SOC hysteresis and queues autonomous `electrolyzer` commands to Simulink.
- **Telemetry & State**: `POST /api/telemetry` and `POST /api/simulink/telemetry`. Directly updates `applied_mode` from telemetry `operating_mode` rather than from command acknowledgments.
- **Irradiance**: Statically estimated via sun-position/clouds algorithm (`solar_irradiance_estimate`) once upon receiving `POST /api/weather`. Irradiance source set to `cloud-cover-estimate` or device ID header, not priority-ranked or timestamp-validated.
- **Commands & Acks**: Commands lack `decision_id` and `expires_at`. `POST /api/simulink/commands/<id>/ack` updates command status in DB but never updates state or alerts on rejection/failure.
- **Security & Network**: CORS header is unrestricted `*`. No authentication tokens. No stale telemetry detection or Simulink heartbeat tracking.

### 1.2 `index.html`
- **Visual Design**: Liquid glassmorphism UI with SVG displacement refraction, animated canvas/gradients, and responsive phone mockup.
- **Defects & Hardcoded Placeholders**:
  - Line 141 references `bg_ready.mp4` which does not exist in the repository.
  - Hardcoded placeholders: UV fixed at `UV 9`, initial state has fake demo alerts and dummy values (battery `46%`, H₂ `68%`).
  - Line 173 has hardcoded text: "Automatic at 90% battery".
  - Mode page descriptions: Boost is described as "Full power, use hydrogen when needed" (must be "Boost hydrogen reserves with reduced load").
  - "Clear all" button in alert modal (`#clear`) only clears local JS array `S.alerts = []`, resurrecting on next poll.
  - Telemetry: Does not display live `solar_power_w`, `load_power_w`, or Simulink connection status.
  - Decision alerts: No card on home screen with plain-language question and "Yes, switch" / "Not now" action buttons.
- **Weather Relaying**: Browser polls OpenWeather proxy, calculates forecast, and posts back to `/api/weather` every 10 minutes. Phone geolocation directly overwrites the plant location.

### 1.3 `test_app.py`
- Contains 7 tests validating the legacy implementation:
  - `test_weather_is_persisted_and_irradiance_is_estimated`: expects browser `POST /api/weather` to update `state["location"]`.
  - `test_forecast_alert_precedes_supervisory_mode_command`: expects automatic queueing of `eco` mode.
  - `test_electrolyzer_auto_threshold_and_manual_override`: expects Flask to generate automatic electrolyzer commands at 90% SOC.
  - `test_invalid_telemetry_is_rejected`
  - `test_weather_provider_proxies_normalized_current_and_forecast`
  - `test_weather_provider_reports_missing_server_key`
  - `test_weather_tile_proxy_hides_provider_key`

### 1.4 `test_engine.py`
- Manual MATLAB engine script that attempts to import `matlab.engine` and run `sim('solar_hydrogen_microgrid')`.
- Running standard `python -m unittest -v` automatically discovers `test_engine.py` (matching `test_*.py`), causing the runner to hang indefinitely when MATLAB engine is not running or installed.
- **Action**: Relocate to `tools/test_engine.py` so standard unittest discovery executes cleanly.

### 1.5 Missing Files in Workspace
- `README.md`: Does not exist yet. Must be authored with architecture, API contracts, Simulink integration guide, environment variables, and contract change log.
- `requirements.txt`: Does not exist yet. Needs to be created with minimal dependencies (`Flask`, `requests` or `urllib`).

---

## 2. Gaps vs. Requirements

| Requirement Area | Current State | Required State |
|---|---|---|
| **Configuration** | Scattered hardcoded constants in `app.py` | Single `config.py` module, frozen defaults, comprehensive env var overrides, input range validation. |
| **Plant Location** | Overwritten by browser phone geolocation | Fixed in config (13.1007 N, 77.5963 E). Browser location only used for map view. |
| **Weather Acquisition** | Browser-relayed calls | Server-side scheduled worker (OpenWeather current + Open-Meteo 10-day outlook). Network timeouts & retry backoff. |
| **Irradiance Pipeline** | Single estimated number | Source hierarchy: measured > Open-Meteo shortwave > cloud-cover estimate. Fresh/stale/fallback quality flags. Endpoint: `GET /api/simulink/inputs`. |
| **Modes & Parameters** | Plain string set `{eco, balanced, boost}` | Unified mode table with setpoints (`load_limit_w`, `electrolyzer_start_soc`, `electrolyzer_stop_soc`, `hydrogen_priority`, `fuel_cell_night_use`). Boost load limit dynamically computed. |
| **Decision Engine** | Auto-timer (45s notice) then auto-queue | Pure policy engine (`policy.py`). Propose Boost (consecutive bad days, tank < 98%) or Eco or Return to Balanced. Plain English yes/no proposals. Approved via user response only. |
| **Electrolyzer Logic** | Autonomous hysteresis in Flask | 90%/88% logic belongs exclusively in Simulink. Flask mirrors state from telemetry. Manual operator toggle supported as user-approved command. |
| **Simulink Command & State** | Telemetry sets `applied_mode`; no command expiry | State tracks `requested_mode` vs `applied_mode`. `applied_mode` updates ONLY on `applied` ack. Rejected/failed acks trigger alerts. Commands have TTL. Endpoint: `GET /api/simulink/setpoints`. |
| **Security & Stale State** | No auth, CORS `*`, no telemetry timeout | `X-AEGIS-Token` authentication, configurable CORS origin list, LAN bind opt-in, telemetry staleness watchdog (`simulink_connected = false`). |
| **UI** | Client-side alert clearing, fake values, missing live powers | Decision card with "Yes, switch" / "Not now", live solar/load power, Simulink connection indicator, server-backed alert dismissal, clean `--` states. |
| **Simulation & Docs** | None | `tools/fake_plant.py` with time acceleration; `docs/SIMULINK_CONTRACT.md` with complete payload schemas and MATLAB scripts. |

---

## 3. Contradictions & Assumptions

### A1. Open-Meteo Radiation Units
- *Issue*: Requirement 4.2 states `solar_wh[d] = shortwave_radiation_sum[d] (kWh/m²) × panel_area × efficiency × performance_ratio × 1000`. However, the Open-Meteo API returns `shortwave_radiation_sum` in **MJ/m²** (MegaJoules per m²), not kWh/m².
- *Assumption*: Open-Meteo's `shortwave_radiation_sum` (MJ/m²) will be converted to kWh/m² via `kWh/m² = MJ/m² / 3.6` (since 1 kWh = 3.6 MJ). Then the formula `solar_wh = (MJ/m² / 3.6) * panel_area * efficiency * performance_ratio * 1000` accurately yields Watt-hours. Unit tests will explicitly verify this unit conversion.

### A2. Performance Ratio
- *Assumption*: Flask uses `performance_ratio = 0.8` for conservative forecast calculations. Simulink's physical gain block remains `panel_area * panel_efficiency`. This discrepancy will be prominently documented in `docs/SIMULINK_CONTRACT.md` and `README.md`.

### A3. Sustainable Load Calculation Window
- *Requirement*: `sustainable_w = (tank_wh_now + battery_wh_now + sum of solar_wh over the bad window) / (hours in window)`.
- *Assumption*: "Hours in window" is calculated as `24 * number_of_bad_days_in_streak`. Clamped to `[min_load_ratio, 1.0] * load_nominal_w` (default min ratio 0.4, i.e., 40% of nominal load).

### A4. Return to Balanced Criteria
- *Requirement*: Propose returning to Balanced when in boost/eco, the bad weather streak clears, and hydrogen is at or above a configurable target.
- *Assumption*: Default return threshold is `h2_return_target_pct = 90.0%` with configurable setting in `config.py`.

### A5. Manual Mode & Decision Generation
- *Assumption*: When the operator manually switches modes (`mode_control = "manual"`), any active pending proposal is immediately withdrawn. While in manual mode, no automated mode change proposals will be issued until the operator clicks "Return to automatic" (`POST /api/mode/auto`).

### A6. Authentication Tokens
- *Requirement*: Require shared token header `X-AEGIS-Token` on all POST endpoints and Simulink endpoints.
- *Assumption*: Supported via `AEGIS_TOKEN` environment variable. If `AEGIS_TOKEN` is unset or blank in development/testing, authentication is bypassed with an informative log warning to ensure effortless local developer onboarding while remaining fully secure in production.

### A7. Database Migrations
- *Assumption*: Add a `decisions` table and expand `commands` with `expires_at`, `decision_id`, and `parameters`. A lightweight migration helper will inspect schema columns and automatically run non-destructive `ALTER TABLE` operations on startup.

---

## 4. Architecture & Technical Design

### 4.1 Module Layout
```
SolarHydrogen/
├── config.py              # Single source of truth: modes, setpoints, physical constants, env overrides
├── policy.py              # Pure-function decision engine: energy forecasts, proposal generation, lifecycle
├── weather_service.py     # Resilient background weather poller (Open-Meteo + OpenWeather)
├── app.py                 # Flask REST API, SQLite persistence, command queue & ack state machine
├── index.html             # Vanilla CSS/JS glassmorphic dashboard
├── requirements.txt       # Dependencies
├── README.md              # Project documentation, API contract, configuration guide
├── tools/
│   ├── fake_plant.py      # Standalone simulator mimicking Simulink plant with time acceleration
│   └── test_engine.py     # Relocated MATLAB engine runner
├── docs/
│   └── SIMULINK_CONTRACT.md # Exact API specs, JSON schemas, polling intervals, MATLAB code snippets
└── tests/ (or test_*.py)  # Comprehensive automated test suite
```

### 4.2 Modes & Setpoint Specification (`config.py`)
| Mode | `load_limit_ratio` | `electrolyzer_start_soc` | `electrolyzer_stop_soc` | `hydrogen_priority` | `fuel_cell_night_use` | Description |
|---|---|---|---|---|---|---|
| **balanced** | 1.00 (100%) | 90% | 88% | `False` | `only_if_battery_low` | Normal days: solar by day, battery by night |
| **eco** | 0.85 (85%) | 90% | 88% | `False` | `only_if_battery_low` | Mild caution: conserve power, tank adequate |
| **boost** | Dynamic (min 0.40) | 75% | 73% | `True` | `allowed_at_night` | Boost hydrogen reserves with reduced load |

### 4.3 Command Lifecycle & State Transitions
- Command schema:
  ```json
  {
    "id": 101,
    "kind": "mode",
    "payload": {
      "mode": "boost",
      "parameters": {
        "load_limit_w": 65.0,
        "electrolyzer_start_soc": 75.0,
        "electrolyzer_stop_soc": 73.0,
        "hydrogen_priority": true,
        "fuel_cell_night_use": "allowed_at_night"
      },
      "decision_id": "d-83b1a2",
      "source": "decision_engine"
    },
    "status": "pending",
    "created_at": "2026-10-31T01:00:00Z",
    "expires_at": "2026-10-31T01:10:00Z",
    "acknowledged_at": null
  }
  ```
- Expiry: If `now >= expires_at`, command status transitions to `expired` and is never delivered to Simulink.
- Acknowledgment:
  - `status = "applied"`: updates `state["applied_mode"] = command.payload.mode`.
  - `status = "rejected"` or `"failed"`: leaves `applied_mode` unchanged and generates a critical alert for the operator.

---

## 5. Ordered Implementation Phases

### Phase 0: System Audit & Plan (Current)
- Complete code inspection of all files.
- Deliver comprehensive `PLAN.md` resolving ambiguities and establishing testable architecture.
- Stop for user review before writing code.

### Phase 1: Unified Configuration (`config.py`)
- Create `config.py` defining all plant parameters, weather thresholds, proposal rules, and the single mode dictionary.
- Support environment variable overrides with type parsing and boundary validation (rejecting NaN, inf, and out-of-bound values).
- Relocate `test_engine.py` to `tools/test_engine.py` to prevent test runner collisions.
- Author unit tests `test_config.py`. Verify `python -m unittest -v`.

### Phase 2: Server-Side Weather & Irradiance Pipeline
- Implement `weather_service.py`:
  - OpenWeather poller for current conditions (10 min schedule).
  - Open-Meteo poller for 10-day hourly & daily outlook (60 min schedule).
  - Robust thread management, exponential backoff, stale data caching, and network fault tolerance.
- Irradiance arbitration logic: `measured` (fresh Simulink telemetry) > `open-meteo` > `cloud-cover-estimate`. Quality flags: `fresh`, `stale`, `fallback`.
- Expose `GET /api/simulink/inputs`.
- Update `index.html` to eliminate browser weather relaying.
- Author unit tests for weather parsing, irradiance ranking, and mocked upstream failures. Verify `python -m unittest -v`.

### Phase 3: Pure Decision Engine & Command Machine (`policy.py` & `app.py`)
- Implement `policy.py` pure functions:
  - Daily solar energy calculation from Open-Meteo radiation sum.
  - Consecutive low-sun day detector.
  - Sustainable load limit computation.
  - Plain English proposal generator (title, question, effects, suggested load).
  - Proposal lifecycle state transitions (`pending`, `approved`, `declined`, `expired`, `withdrawn`) with decline cooldown logic.
- Remove Flask automatic electrolyzer command generator.
- Implement `POST /api/decisions/<id>/respond` (`{"approve": true|false}`).
- Implement `GET /api/simulink/setpoints` and update command queue with `expires_at`, `decision_id`, and `applied` ack state transitions.
- Author comprehensive unit tests covering all rules in Section 4 of the specification. Verify `python -m unittest -v`.

### Phase 4: Robustness, Security, Stale Watchdog & Logging
- Shared token authorization via `X-AEGIS-Token` header.
- Configurable CORS origin whitelist and Host binding (default 127.0.0.1).
- Simulink connection monitoring: Telemetry older than `telemetry_stale_seconds` sets `simulink_connected = false` and raises an alert.
- Implement server-side alert dismissal: `POST /api/alerts/clear` and `POST /api/alerts/<id>/dismiss`.
- Input validation sanitization across all endpoints; secret-scrubbed structured logging.
- Author unit tests for security, stale detection, and alert management. Verify `python -m unittest -v`.

### Phase 5: UI Enhancements (`index.html`)
- Integrate Decision Card on home view with plain language prompt and "Yes, switch" / "Not now" actions.
- Mode page: Display Auto/Manual status, requested vs applied mode ("waiting for system" pill), "Return to automatic" action, and updated mode descriptions.
- Wire alert dismissals and "Clear all" to server endpoints.
- Display live `solar_power_w`, `load_power_w`, and Simulink connection badge.
- Remove dead `bg_ready.mp4` reference, replace hardcoded UV/placeholders with `--` fallbacks.
- Replace hardcoded "Automatic at 90% battery" with live server configuration.
- Manually verify UI styling and layout integrity.

### Phase 6: Plant Simulator & Integration Documentation
- Build `tools/fake_plant.py`: Simulates the Simulink plant model (battery 18h Wh, solar gain, electrolyzer, fuel cell, tank) with polling of inputs/setpoints, telemetry emission, command acknowledgment, and time-acceleration (`--accel`).
- Author `docs/SIMULINK_CONTRACT.md`:
  - Complete endpoint specifications (`inputs`, `setpoints`, `commands`, `ack`, `telemetry`).
  - Polling frequency recommendations.
  - Ready-to-use MATLAB scripts using `webread`, `webwrite`, and `weboptions` with authentication headers.

### Phase 7: Verification, Documentation & Final Polish
- Create `requirements.txt` and author comprehensive `README.md` detailing architecture, configuration, API contract, and contract changelog.
- Execute full automated test suite covering all 9 acceptance scenarios.
- Deliver summary report with git commit history.

---

## 6. Acceptance Scenarios Test Matrix

| Scenario | Condition | Expected Behavior | Verification Phase |
|---|---|---|---|
| **1. Balanced Clear Day** | Clear 10-day forecast, balanced mode | No mode proposal generated; `GET /api/simulink/inputs` delivers `open-meteo` irradiance with quality `fresh`. | Phase 2, Phase 3 |
| **2. Boost Trigger** | 3 rain days + 7 low-sun days, tank at 60% | Exactly one Boost proposal created with plain-language question and suggested load limit; no command queued. | Phase 3 |
| **3. Proposal Approval** | Operator clicks "Yes, switch" | Proposal transitions to `approved`; mode command queued with Boost setpoints; plant acks `applied`; `applied_mode = "boost"`. | Phase 3, Phase 6 |
| **4. Proposal Decline** | Operator clicks "Not now" | Proposal transitions to `declined`; no command queued; no repeated proposal within cooldown window unless conditions worsen. | Phase 3 |
| **5. Full Tank Gate** | Bad weather streak, but hydrogen tank >= 98% | No Boost proposal generated despite bad weather. | Phase 3 |
| **6. Return to Balanced** | Weather clears while in Boost, tank >= 90% | "Return to Balanced" proposal generated requiring user confirmation. | Phase 3 |
| **7. Upstream Weather Fault** | OpenWeather / Open-Meteo network timeout | Server flags cached weather data as `stale`, continues scheduler retries without crashing, and avoids generating invalid proposals. | Phase 2 |
| **8. Telemetry Loss** | No telemetry received for > 15 seconds | `simulink_connected` set to `false`; telemetry stale alert raised; expired commands not delivered. | Phase 4 |
| **9. Manual Mode Override** | Operator manually selects mode on Mode page | Mode command queued immediately without second confirmation; any pending decision proposal is withdrawn. | Phase 3, Phase 5 |