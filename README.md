# AEGIS: Solar-Hydrogen Microgrid Supervisory Control System

AEGIS (**Adaptive Energy & Grid Intelligence Supervisor**) is a supervisory control platform for islanded renewable microgrids integrating solar photovoltaics (PV), battery energy storage, proton exchange membrane (PEM) water electrolysis for hydrogen generation, high-pressure hydrogen storage, and fuel cell backup generation.

The system interfaces with a physical plant simulation modeled in **Simulink** (`solar_hydrogen_microgrid.slx`) or the included standalone simulator ([tools/fake_plant.py](file:///c:/Users/rabbu/OneDrive/Desktop/Projects/SolarHydrogen/tools/fake_plant.py)).

---

## 1. System Architecture & Physical Plant Model

```
                                  [External Weather Services]
                                   OpenWeather & Open-Meteo
                                              │
                                              ▼
                                 +─────────────────────────+
                                 │     AEGIS Supervisor    │
                                 │       (Flask Backend)   │
                                 │  - Weather scheduler    │
                                 │  - Irradiance arbiter   │
                                 │  - Pure decision engine │
                                 │  - Operator dashboard   │
                                 +─────────────────────────+
                                              ▲
                           REST API / Telemetry & Setpoints
                                              ▼
    +───────────────────────────────────────────────────────────────────────────+
    │                        SIMULINK MICROGRID PLANT MODEL                     │
    │                                                                           │
    │   [Solar PV Gain]            [Microgrid Load]          [Battery Backup]   │
    │   Irradiance * Area * Eff ──> 100 W (Nominal)  <─────> 1800 Wh (18 hours) │
    │         │                                                     │           │
    │         ▼ (Surplus)                                           ▼ (Deficit) │
    │   [PEM Electrolyzer]                                   [Fuel Cell]        │
    │   H2 Generation (70% eff)                              Backup (50% eff)   │
    │         │                                                     ▲           │
    │         ▼                                                     │           │
    │   +───────────────────────────────────────────────────────────────+       │
    │   │                 Hydrogen Storage Tank (2400 Wh)               │       │
    │   +───────────────────────────────────────────────────────────────+       │
    +───────────────────────────────────────────────────────────────────────────+
```

### Core Responsibilities
* **Simulink Owns Protection & Autonomous Loops**: Simulink retains all autonomous electrical safety interlocks, over-current protection, thermal cutoffs, battery low-voltage disconnects, and autonomous electrolyzer hysteresis.
* **Supervisory Role of Flask**: Computes multi-day solar energy forecasts, identifies upcoming streaks of low-sun days, formulates plain-language mode proposals (`balanced`, `eco`, `boost`) requiring operator confirmation, queues setpoints, and arbitrates solar irradiance.
* **Fail-Safe Operation**: If communication with Flask is lost, Simulink continues running autonomously using its active parameters and internal safety rules.

---

## 2. Microgrid Specifications & Modes

### 2.1. Physical Constants
* **Nominal Load**: $100\text{ W}$ continuous electrical load.
* **Battery Energy Storage**: $18\text{ hours}$ capacity at nominal load ($E_{battery} = 100\text{ W} \times 18\text{ h} = 1800\text{ Wh}$).
* **Hydrogen Storage Tank**: $2400\text{ Wh}$ energy capacity equivalent.
* **Photovoltaic Collector**: $1.0\text{ m}^2$ panel area with $20\%$ electrical efficiency ($\eta = 0.20$).

### 2.2. Operating Modes Matrix
| Mode | Load Limit | Electrolyzer Start SOC | Electrolyzer Stop SOC | Hydrogen Priority | Fuel Cell Night Use | Description |
|---|---|---|---|---|---|---|
| **Balanced** | $100\text{ W}$ (100%) | $90\%$ | $88\%$ | `False` | `only_if_battery_low` | Standard operation. Solar powers load and charges battery by day; battery supplies nighttime deficit. |
| **Eco** | $85\text{ W}$ (85%) | $90\%$ | $88\%$ | `False` | `only_if_battery_low` | Precautionary conservation during mild weather or isolated rain. |
| **Boost** | Dynamic ($[40, 100]\text{ W}$) | $75\%$ | $73\%$ | `True` | `allowed_at_night` | High-reserve preparedness before multi-day low-sun streaks. Lowers load and prioritizes filling $H_2$ tank. |

> [!NOTE]
> **Energy Forecast vs. Instantaneous Power Conversion**:
> In Simulink, instantaneous solar power is modeled directly as $P(t) = G(t) \cdot A \cdot \eta$. In the decision engine ([policy.py](file:///c:/Users/rabbu/OneDrive/Desktop/Projects/SolarHydrogen/policy.py)), daily energy yield forecasts apply a **Performance Ratio** of $PR = 0.80$ ($E_{daily} = H_{daily} \cdot A \cdot \eta \cdot PR$) to account for real-world inverter losses, dust/soiling, and thermal derating over multi-day outlooks.

---

## 3. Quickstart

### Prerequisites
* Python 3.10+
* (Optional) MATLAB / Simulink 2022b+ (or use the built-in plant simulator)

### 3.1. Installation
```bash
# Clone repository
git clone <repo_url>
cd SolarHydrogen

# Install Python dependencies
pip install -r requirements.txt
```

### 3.2. Running the AEGIS Supervisor
```bash
python app.py
```
By default, the server starts on `http://127.0.0.1:5000`. Navigate to `http://127.0.0.1:5000` in any web browser to view the real-time operational dashboard.

### 3.3. Running the Plant Simulator
Without needing MATLAB, run the included hardware simulator:
```bash
# Run simulator at 1x real-time speed
python tools/fake_plant.py

# Run simulator at 60x acceleration (1 minute simulated per real second)
python tools/fake_plant.py --accel 60

# Run simulator with custom initial battery and tank levels
python tools/fake_plant.py --accel 60 --battery-soc 65 --h2-level 45
```

### 3.4. Running the Test Suite
Run all unit and acceptance tests:
```bash
python -m unittest -v
```
To run only the 9 end-to-end acceptance scenarios:
```bash
python -m unittest -v test_acceptance.py
```

### 3.5. Connecting the Real Simulink Model (MATLAB)
1. Open MATLAB and navigate to the repository directory (`SolarHydrogen`).
2. Open the Simulink model:
   ```matlab
   open_system('solar_hydrogen_microgrid');
   ```
3. Run the synchronization bridge script:
   ```matlab
   run('tools/aegis_simulink_bridge.m');
   ```
   The bridge communicates with the AEGIS supervisor on `http://127.0.0.1:5000` via REST API, automatically applies setpoints to your Simulink blocks (`set_param`), polls and executes mode commands, and streams model telemetry to the web dashboard.

---

## 4. Configuration & Environment Variables

All parameters can be tuned in [config.py](file:///c:/Users/rabbu/OneDrive/Desktop/Projects/SolarHydrogen/config.py) or overridden via environment variables without editing code:

| Variable | Type | Default | Description |
|---|---|---|---|
| `AEGIS_HOST` | String | `127.0.0.1` | Network interface to bind. Use `0.0.0.0` only on secured private LANs. |
| `AEGIS_PORT` | Integer | `5000` | Port for Flask web server. |
| `AEGIS_DB` | String | `data/aegis.sqlite3` | SQLite database file path. |
| `AEGIS_TOKEN` | String | `""` (empty) | Master authentication token (enables auth if non-empty). |
| `AEGIS_DEVICE_TOKEN` | String | `""` | Restricted token for Simulink plant / fake plant telemetry. |
| `AEGIS_OPERATOR_TOKEN` | String | `""` | Restricted token for operator dashboard actions. |
| `AEGIS_CORS_ORIGINS` | String | `*` | Comma-separated list of allowed CORS origins. |
| `OPENWEATHER_API_KEY` | String | `""` | API key for OpenWeather current conditions (optional). |
| `PLANT_LAT` | Float | `13.0827` | Microgrid geographic latitude. |
| `PLANT_LON` | Float | `80.2707` | Microgrid geographic longitude. |
| `NOMINAL_LOAD_W` | Float | `100.0` | Nominal electrical continuous load in Watts. |
| `BATTERY_HOURS` | Float | `18.0` | Hours of continuous battery backup at nominal load. |
| `H2_TANK_WH` | Float | `2400.0` | Hydrogen storage tank capacity in Watt-hours. |
| `COMMAND_TTL_SECONDS` | Float | `300.0` | Command expiration window (seconds). |
| `TELEMETRY_STALE_SECONDS`| Float | `15.0` | Watchdog timeout before plant is flagged offline. |
| `DECISION_COOLDOWN_HOURS`| Float | `6.0` | Cooldown period after operator declines a proposal. |

---

## 5. API Contract & Changelog

### 5.1. Summary of Contract Endpoints

| Endpoint | Method | Role | Description |
|---|---|---|---|
| `/api/state` | GET | Dashboard | Complete system state: telemetry, active mode, applied mode, and pending decision. |
| `/api/mode` | POST | Operator | Directly select mode (`balanced`, `eco`, `boost`). Queues command and withdraws pending decision. |
| `/api/mode/auto` | POST | Operator | Returns system to automatic supervisory mode assessment. |
| `/api/decisions/<id>/respond`| POST | Operator | Approves (`{"approve": true}`) or declines (`{"approve": false}`) a pending proposal. |
| `/api/simulink/inputs` | GET | Simulink | Solar irradiance ($W/m^2$), data source, and freshness quality flag. |
| `/api/simulink/setpoints` | GET | Simulink | Active supervisory setpoints (`load_limit_w`, electrolyzer start/stop SOC). |
| `/api/simulink/commands` | GET | Simulink | Polls pending commands cursor-based with `?after=<id>`. |
| `/api/simulink/commands/<id>/ack`| POST | Simulink | Acknowledges execution: `{"status": "applied"\|"rejected"\|"failed"}`. |
| `/api/simulink/telemetry` | POST | Simulink | Ingests real-time plant state (`battery_soc`, `h2_level`, `solar_power_w`, etc.). |
| `/api/alerts` | GET | Dashboard | Active system alerts. |
| `/api/alerts/clear` | POST | Operator | Clears all active alerts. |
| `/api/alerts/<id>/dismiss` | POST | Operator | Dismisses a specific alert. |

### 5.2. Formal Contract Changes from Legacy System
1. **Decoupled State Evaluation from GET Requests**:
   * *Legacy*: `GET /api/state` executed proposal generation and side-effects on every 2-second UI poll.
   * *AEGIS*: State evaluations only execute when new telemetry arrives, weather updates, or an operator responds.
2. **Human-in-the-Loop Supervisory Approvals**:
   * *Legacy*: Automatic switching queued mode commands directly without confirmation.
   * *AEGIS*: The decision engine produces plain-language `pending_decision` proposals. No mode command is queued until an operator explicitly approves (`POST /api/decisions/<id>/respond`).
3. **Simulink Owns Electrolyzer Hysteresis**:
   * *Legacy*: Flask automatically issued high-frequency electrolyzer start/stop commands at 90%/88% battery charge.
   * *AEGIS*: Hysteresis control loops are owned natively by Simulink. Flask only dispatches manual overrides when requested by an operator.
4. **Command Time-To-Live (TTL) & Acknowledgment State Machine**:
   * *Legacy*: Commands remained pending indefinitely and `applied_mode` assumed instantaneous execution.
   * *AEGIS*: Commands carry an `expires_at` timestamp. Expired commands return HTTP 410 Gone. The server tracks `requested_mode` separately from `applied_mode`; `applied_mode` updates *only* when Simulink returns an `applied` acknowledgment.
5. **Server-Side Weather Ingestion**:
   * *Legacy*: The browser frontend fetched external weather and relayed it back to Flask.
   * *AEGIS*: Flask includes a resilient background `WeatherService` polling OpenWeather and Open-Meteo directly.
6. **Token-Based Authentication & Host Isolation**:
   * *Legacy*: Open endpoints binding to `0.0.0.0` with universal wildcard CORS.
   * *AEGIS*: Defaults to `127.0.0.1` binding, configurable CORS origin whitelist, and optional `X-AEGIS-Token` authentication with role isolation.

---

## 6. Security Notice

> [!WARNING]
> **Deployment Notice: Not Internet-Facing**
> 
> AEGIS is engineered for standalone, islanded industrial microgrid controllers operating on isolated local area networks (LAN).
> * By default, the server binds strictly to `127.0.0.1` (localhost).
> * When connecting across a physical microgrid LAN, set `AEGIS_HOST=0.0.0.0` only behind a firewall or VPN, configure strong secrets via `AEGIS_TOKEN` or `AEGIS_DEVICE_TOKEN`, and restrict `AEGIS_CORS_ORIGINS`.
> * Never expose AEGIS directly to the public internet without an upstream reverse proxy (e.g. NGINX) enforcing TLS/HTTPS and authentication.

---

## 7. Automated Acceptance Verification Matrix

All 9 acceptance scenarios specified in Section 8 of the system requirements are verified in [test_acceptance.py](file:///c:/Users/rabbu/OneDrive/Desktop/Projects/SolarHydrogen/test_acceptance.py):

| Scenario | Condition | Verified Behavior |
|---|---|---|
| **1. Balanced Clear Day** | Clear 10-day forecast in Balanced mode | No proposal created; irradiance delivered from Open-Meteo with quality `fresh`. |
| **2. Boost Trigger** | 3 rain days + 7 low-sun days, tank at 60% | Exactly one Boost proposal created with plain-language prompt; no command queued. |
| **3. Proposal Approval** | Operator clicks "Yes, switch" | Proposal transitions to `approved`; mode command queued; plant acks `applied`; `applied_mode = "boost"`. |
| **4. Proposal Decline** | Operator clicks "Not now" | Proposal transitions to `declined`; no command queued; 6-hour cooldown prevents repeat. |
| **5. Full Tank Gate** | Bad weather streak, but hydrogen tank $\ge 98\%$ | No Boost proposal generated despite low-sun streak. |
| **6. Return to Balanced** | Weather clears in Boost mode, tank $\ge 90\%$ | "Return to Balanced" proposal created requiring confirmation. |
| **7. Upstream Weather Fault** | Weather API timeout / unreachable | Cached data marked `stale`; scheduler retries without crash; no bogus proposals. |
| **8. Telemetry Loss** | No telemetry received for $> 15\text{ s}$ | `simulink_connected = false`; watchdog warning alert raised; expired commands rejected. |
| **9. Manual Mode Override** | Operator manually selects mode on Mode page | Mode command queued immediately without confirmation; pending proposal withdrawn. |
