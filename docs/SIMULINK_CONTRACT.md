# AEGIS Supervisory Controller & Simulink Plant Interface Contract

This document defines the formal integration contract between the **AEGIS Supervisory Control Backend** (`Flask`) and the **Solar-Hydrogen Microgrid Plant Model** (`Simulink / MATLAB` or `tools/fake_plant.py`).

---

## 1. Architectural Roles & Boundary

```
+-----------------------------------------------------------+
|               AEGIS Flask Supervisory Backend             |
|  - Weather forecaster (OpenWeather / Open-Meteo)          |
|  - Multi-day decision engine (policy.py)                  |
|  - Operator approval dashboard (index.html)               |
|  - Irradiance arbitration & supervisory setpoint queue    |
+-----------------------------------------------------------+
                        ▲                  │
     HTTP POST Telemetry│                  │ HTTP GET Inputs / Commands / Setpoints
     HTTP POST Ack      │                  ▼
+-----------------------------------------------------------+
|              Simulink Microgrid Plant Model               |
|  - Hardware protection & electrical safety interlocks     |
|  - Solar panel power conversion (Area * Eff gain block)   |
|  - Battery backup dynamics (18 h, 1800 Wh)                |
|  - Autonomous electrolyzer hysteresis (90%/88% or 75%/73%)|
|  - Hydrogen storage tank (2400 Wh)                        |
|  - Fuel cell backup generation                            |
+-----------------------------------------------------------+
```

### Core Separation of Responsibilities
1. **Simulink Owns Safety & Interlocks**:
   - Simulink retains all autonomous electrical safety interlocks, over-current protection, thermal cutoffs, and battery low-voltage disconnects.
   - Simulink implements autonomous electrolyzer hysteresis. The supervisory controller never directly toggles high-frequency switching or protection circuits.
2. **Supervisory Role of Flask**:
   - Flask acts as a high-level operational planner: it computes multi-day solar energy forecasts, determines whether weather patterns require switching operational modes (`balanced`, `eco`, `boost`), solicits operator confirmation, queues setpoints, and feeds weather-derived irradiance.
3. **Fail-Safe Operation**:
   - If communication with Flask is lost, Simulink continues running autonomously using its last applied setpoints and internal safety rules.

---

## 2. Authentication & Headers

Every HTTP request from Simulink to Flask must include:

| Header Name | Type | Description |
|---|---|---|
| `Content-Type` | String | Must be `application/json` for requests with a payload (`POST`). |
| `X-AEGIS-Token` | String | Security token configured via `AEGIS_TOKEN` or `AEGIS_DEVICE_TOKEN`. Required if server authentication is enabled. |
| `X-Device-Id` | String | Identifier of the client (e.g. `simulink-microgrid`, `fake-plant`). Default: `simulink`. |

---

## 3. Recommended Polling Rates & Timers

| Endpoint | Direction | Recommended Interval | Purpose |
|---|---|---|---|
| `GET /api/simulink/inputs` | Simulink $\rightarrow$ Flask | **30 to 60 s** | Fetches external solar irradiance ($W/m^2$) derived from forecast/weather feeds. Irradiance varies slowly. |
| `GET /api/simulink/commands` | Simulink $\rightarrow$ Flask | **2 to 5 s** | Low-latency polling for queued operator or supervisor commands (e.g. mode switches, manual overrides). |
| `POST /api/simulink/commands/<id>/ack` | Simulink $\rightarrow$ Flask | **Immediate** | Immediately sent upon receiving a command to report `applied`, `rejected`, or `failed`. |
| `POST /api/simulink/telemetry` | Simulink $\rightarrow$ Flask | **2 to 5 s** | Streams plant physical state. If no telemetry is received for > `telemetry_stale_seconds` (default: 15 s), Flask flags the connection as offline and triggers a watchdog alert. |
| `GET /api/simulink/setpoints` | Simulink $\rightarrow$ Flask | **Startup / On Reconnect** | Recovers the active supervisory parameters after plant reboot or communication recovery. |

---

## 4. REST API Endpoint Specifications

### 4.1. Solar Irradiance Inputs: `GET /api/simulink/inputs`
Retrieves the external solar irradiance computed by the AEGIS weather arbitration engine.

- **Request**: `GET /api/simulink/inputs`
- **Response Status**: `200 OK`
- **Response Schema**:
```json
{
  "irradiance_w_m2": 642.5,
  "source": "open-meteo",
  "quality": "fresh",
  "timestamp": "2026-10-31T02:00:00Z"
}
```

#### Field Definitions
- `irradiance_w_m2` *(number, float)*: Solar global horizontal irradiance in Watts per square meter ($W/m^2$). Guaranteed $\ge 0.0$ and non-NaN.
- `source` *(string)*: Arbitration source:
  - `"measured"`: High-priority reading from a physical pyranometer reported via telemetry.
  - `"open-meteo"`: High-resolution hourly forecast from Open-Meteo.
  - `"cloud-cover-estimate"`: Fallback derived from OpenWeather cloud cover percentage.
  - `"default"`: Fixed daytime/nighttime baseline.
- `quality` *(string)*: Data quality flag:
  - `"fresh"`: Successfully updated within configured freshness intervals.
  - `"stale"`: Upstream network unreachable; cached value used.
  - `"fallback"`: Default backup value.
- `timestamp` *(string, ISO 8601)*: Timestamp when the irradiance reading was computed.

---

### 4.2. Active Setpoints: `GET /api/simulink/setpoints`
Retrieves the authoritative supervisory setpoints configured on the server. Used by Simulink during model initialization or after a communication outage.

- **Request**: `GET /api/simulink/setpoints`
- **Response Status**: `200 OK`
- **Response Schema**:
```json
{
  "mode": "boost",
  "source": "operator",
  "parameters": {
    "load_limit_w": 65.0,
    "electrolyzer_start_soc": 75.0,
    "electrolyzer_stop_soc": 73.0,
    "hydrogen_priority": true,
    "fuel_cell_night_use": "allowed_at_night"
  },
  "timestamp": "2026-10-31T02:00:00Z"
}
```

#### Field Definitions
- `mode` *(string)*: Current operating mode (`"balanced"`, `"eco"`, or `"boost"`).
- `source` *(string)*: `"automatic"` (decision engine approved) or `"operator"` (manual UI override).
- `parameters` *(object)*:
  - `load_limit_w` *(float)*: Maximum permitted load power consumption in Watts.
  - `electrolyzer_start_soc` *(float)*: Battery SOC (%) threshold above which electrolyzer starts hydrogen production.
  - `electrolyzer_stop_soc` *(float)*: Battery SOC (%) threshold below which electrolyzer ceases production.
  - `hydrogen_priority` *(boolean)*: When `true`, surplus solar is prioritized for hydrogen generation over battery charging.
  - `fuel_cell_night_use` *(string)*: `"allowed_at_night"` or `"only_if_battery_low"`.

---

### 4.3. Command Queue: `GET /api/simulink/commands`
Fetches pending commands queued for execution by Simulink.

- **Request**: `GET /api/simulink/commands?after=<last_id>`
  - Query parameter `after` *(optional, integer)*: Command ID cursor; returns only commands with `id > after`.
- **Response Status**: `200 OK`
- **Response Schema**:
```json
{
  "commands": [
    {
      "id": 142,
      "kind": "mode",
      "payload": {
        "mode": "boost",
        "parameters": {
          "load_limit_w": 60.0,
          "electrolyzer_start_soc": 75.0,
          "electrolyzer_stop_soc": 73.0,
          "hydrogen_priority": true,
          "fuel_cell_night_use": "allowed_at_night"
        },
        "decision_id": "dec-20261031-01",
        "source": "decision_engine"
      },
      "status": "pending",
      "created_at": "2026-10-31T02:00:00Z",
      "expires_at": "2026-10-31T02:05:00Z",
      "acknowledged_at": null
    }
  ],
  "total": 1
}
```

#### Command Kinds
1. **Mode Command (`kind: "mode"`)**: Instructs Simulink to update load limits and electrolyzer hysteresis thresholds.
2. **Electrolyzer Override Command (`kind: "electrolyzer"`)**:
   - `payload.control_mode`: `"auto"` (return to hysteresis) or `"manual"` (forced manual override).
   - `payload.enabled`: `true` or `false` when in manual control mode.

---

### 4.4. Command Acknowledgment: `POST /api/simulink/commands/<id>/ack`
Informs Flask whether a queued command was successfully applied, rejected by Simulink interlocks, or encountered a fault.

- **Request**: `POST /api/simulink/commands/<id>/ack`
- **Request Body**:
```json
{
  "status": "applied",
  "error": null
}
```
- **Acknowledgment Statuses**:
  - `"applied"`: Command validated and applied to plant control parameters. Updates Flask `state["applied_mode"]`.
  - `"rejected"`: Internal plant safety interlock prevented applying the command (e.g. invalid parameter combination). Does not change `applied_mode`; triggers an operator warning alert.
  - `"failed"`: Plant hardware or controller failure occurred. Triggers a critical operator alert.
- **Response Codes**:
  - `200 OK`: Acknowledgment processed.
  - `404 Not Found`: Unknown command ID.
  - `410 Gone`: Command exceeded TTL (`expires_at`) before acknowledgment was received. Simulink should discard expired commands.

---

### 4.5. Plant Telemetry: `POST /api/simulink/telemetry`
Transmits real-time physical plant state to Flask.

- **Request**: `POST /api/simulink/telemetry`
- **Request Body**:
```json
{
  "telemetry": {
    "battery_soc": 78.4,
    "h2_level": 62.0,
    "solar_power_w": 128.5,
    "load_power_w": 100.0,
    "irradiance_w_m2": 642.5,
    "operating_mode": "balanced",
    "electrolyzer_enabled": false,
    "sequence": 4182
  }
}
```
- **Response Status**: `200 OK`
- **Response Schema**:
```json
{
  "status": "ok",
  "telemetry_id": 4182
}
```

#### Telemetry Field Definitions & Units
- `battery_soc` *(float, 0.0 to 100.0 %)*: Battery state of charge.
- `h2_level` *(float, 0.0 to 100.0 %)*: Hydrogen storage tank fill level.
- `solar_power_w` *(float, $\ge 0.0$ W)*: Realized solar electrical power output.
- `load_power_w` *(float, $\ge 0.0$ W)*: Active electrical consumption of the microgrid load.
- `irradiance_w_m2` *(float, $\ge 0.0$ $W/m^2$)*: Measured or applied solar irradiance.
- `operating_mode` *(string)*: Currently active plant mode (`"balanced"`, `"eco"`, `"boost"`).
- `electrolyzer_enabled` *(boolean)*: True if the electrolyzer is actively drawing power to generate $H_2$.
- `sequence` *(integer, monotonically increasing)*: Telemetry frame sequence counter.

---

## 5. Physical Model Constants & Discrepancy Note

### 5.1. Microgrid Sizing Specifications
- **Nominal Load**: $100\text{ W}$ continuous electrical load.
- **Battery Backup**: $18\text{ hours}$ capacity at nominal load:
  $$E_{battery} = 100\text{ W} \times 18\text{ h} = 1800\text{ Wh}$$
- **Hydrogen Storage Tank**: $2400\text{ Wh}$ energy capacity equivalent.
- **Photovoltaic Collector**: $1.0\text{ m}^2$ panel area with $20\%$ electrical conversion efficiency ($\eta = 0.20$).

### 5.2. Energy Forecast vs. Instantaneous Power Conversion Note
> [!IMPORTANT]
> **Architectural Distinction: Forecast PR vs. Instantaneous Gain**
>
> 1. **In Simulink (Physical Plant Instantaneous Power)**:
>    The solar panel block calculates instantaneous power ($W$) as:
>    $$P_{solar}(t) = G(t) \cdot A \cdot \eta$$
>    where $G(t)$ is irradiance ($W/m^2$), $A = 1.0\text{ m}^2$, and $\eta = 0.20$. No arbitrary derating factor is applied inside the physical block because the electrical circuit models instantaneous conversion.
>
> 2. **In Flask Decision Engine (`policy.py` Multi-Day Yield)**:
>    The daily solar energy forecast ($kWh/day$) is computed as:
>    $$E_{daily} = H_{daily} \cdot A \cdot \eta \cdot PR$$
>    where $H_{daily}$ is the daily solar insolation ($kWh/m^2/day$), and $PR = 0.80$ is the **Performance Ratio**.
>
> **Rationale**: Over a multi-day forecast horizon, real-world solar installations experience temperature derating, dust/soiling losses, inverter/converter inefficiencies, and wiring resistance. Incorporating $PR = 0.80$ prevents the supervisory decision engine from overestimating multi-day energy yields and failing to recommend Boost mode in marginal weather.

---

## 6. Ready-to-Use MATLAB Integration Script

Below is a production-grade MATLAB integration script (`aegis_simulink_bridge.m`). It runs inside MATLAB, connects to a running Simulink model, exchanges inputs and setpoints, and posts telemetry.

```matlab
%% AEGIS Simulink Microgrid Bridge
% Synchronizes a running Simulink model with the AEGIS Flask Supervisory Server.
% Handles inputs, commands, acknowledgments, and telemetry streaming.

function aegis_simulink_bridge()
    clear; clc;
    fprintf('=== Initializing AEGIS Simulink Microgrid Bridge ===\n');

    % Configuration
    serverUrl = getenv_default('AEGIS_HOST', 'http://127.0.0.1:5000');
    token = getenv_default('AEGIS_TOKEN', getenv_default('AEGIS_DEVICE_TOKEN', ''));
    modelName = 'solar_hydrogen_microgrid';
    pollIntervalSec = 2.0;

    % HTTP Options Setup
    opts = weboptions(...
        'MediaType', 'application/json', ...
        'Timeout', 5, ...
        'CertificateFilename', '' ... % Add cert path if HTTPS in production
    );
    if ~isempty(token)
        opts.HeaderFields = {'X-AEGIS-Token', token; 'X-Device-Id', 'simulink'};
    else
        opts.HeaderFields = {'X-Device-Id', 'simulink'};
    end

    % Verify Simulink Model is Open
    if ~bdIsLoaded(modelName)
        open_system(modelName);
    end

    % 1. Fetch Initial Setpoints from Server
    fprintf('Fetching initial setpoints from %s...\n', serverUrl);
    try
        sp = webread([serverUrl '/api/simulink/setpoints'], opts);
        apply_setpoints_to_simulink(modelName, sp);
        fprintf('Active mode: %s (Load limit: %.1f W)\n', sp.mode, sp.parameters.load_limit_w);
    catch ME
        warning('Could not fetch setpoints: %s. Using Simulink model defaults.', ME.message);
    end

    lastCommandId = 0;
    sequenceCounter = 0;
    inputPollTimer = 0;

    fprintf('Starting synchronization loop (Ctrl+C to stop)...\n');

    % 2. Main Runtime Loop
    while true
        loopStart = tic;

        % A. Poll Irradiance Inputs every 30 seconds
        if inputPollTimer <= 0
            try
                inputs = webread([serverUrl '/api/simulink/inputs'], opts);
                if isfield(inputs, 'irradiance_w_m2')
                    set_param([modelName '/Solar_Irradiance_Input'], 'Value', num2str(inputs.irradiance_w_m2));
                end
                inputPollTimer = 30.0; % Reset 30s timer
            catch ME
                warning('Failed to fetch inputs: %s', ME.message);
            end
        else
            inputPollTimer = inputPollTimer - pollIntervalSec;
        end

        % B. Poll and Acknowledge Commands
        try
            cmdResp = webread(sprintf('%s/api/simulink/commands?after=%d', serverUrl, lastCommandId), opts);
            if isfield(cmdResp, 'commands') && ~isempty(cmdResp.commands)
                for i = 1:numel(cmdResp.commands)
                    cmd = cmdResp.commands(i);
                    lastCommandId = max(lastCommandId, cmd.id);
                    [success, errReason] = apply_command_to_simulink(modelName, cmd);

                    % Send Acknowledgment
                    ackPayload = struct();
                    if success
                        ackPayload.status = 'applied';
                        ackPayload.error = [];
                    else
                        ackPayload.status = 'rejected';
                        ackPayload.error = errReason;
                    end
                    webwrite(sprintf('%s/api/simulink/commands/%d/ack', serverUrl, cmd.id), ackPayload, opts);
                end
            end
        catch ME
            warning('Command polling/ack error: %s', ME.message);
        end

        % C. Read Model Telemetry Signals
        sequenceCounter = sequenceCounter + 1;
        try
            telemetryData = struct();
            telemetryData.battery_soc = read_signal(modelName, 'Battery_SOC', 60.0);
            telemetryData.h2_level = read_signal(modelName, 'H2_Tank_Level', 50.0);
            telemetryData.solar_power_w = read_signal(modelName, 'Solar_Power_W', 0.0);
            telemetryData.load_power_w = read_signal(modelName, 'Load_Power_W', 100.0);
            telemetryData.irradiance_w_m2 = read_signal(modelName, 'Solar_Irradiance_Input', 0.0);
            telemetryData.operating_mode = get_param([modelName '/Mode_Selector'], 'Value');
            telemetryData.electrolyzer_enabled = logical(read_signal(modelName, 'Electrolyzer_State', 0));
            telemetryData.sequence = sequenceCounter;

            body = struct('telemetry', telemetryData);
            webwrite([serverUrl '/api/simulink/telemetry'], body, opts);
        catch ME
            warning('Failed to transmit telemetry: %s', ME.message);
        end

        % Maintain Loop Cadence
        elapsed = toc(loopStart);
        pause(max(0.1, pollIntervalSec - elapsed));
    end
end

%% Helper: Apply Parameters to Simulink Blocks
function apply_setpoints_to_simulink(model, sp)
    p = sp.parameters;
    try
        set_param([model '/Load_Limit_Block'], 'Value', num2str(p.load_limit_w));
        set_param([model '/H2_Start_SOC'], 'Value', num2str(p.electrolyzer_start_soc));
        set_param([model '/H2_Stop_SOC'], 'Value', num2str(p.electrolyzer_stop_soc));
    catch ME
        warning('Failed to set block parameter: %s', ME.message);
    end
end

%% Helper: Apply Queued Command
function [success, errReason] = apply_command_to_simulink(model, cmd)
    success = true;
    errReason = '';
    try
        if strcmp(cmd.kind, 'mode')
            modeName = cmd.payload.mode;
            p = cmd.payload.parameters;
            set_param([model '/Load_Limit_Block'], 'Value', num2str(p.load_limit_w));
            set_param([model '/H2_Start_SOC'], 'Value', num2str(p.electrolyzer_start_soc));
            set_param([model '/H2_Stop_SOC'], 'Value', num2str(p.electrolyzer_stop_soc));
            fprintf('[CMD %d] Applied mode "%s" to Simulink.\n', cmd.id, modeName);
        elseif strcmp(cmd.kind, 'electrolyzer')
            ctrl = cmd.payload.control_mode;
            if strcmp(ctrl, 'manual')
                set_param([model '/Electrolyzer_Manual_Override'], 'Value', num2str(double(cmd.payload.enabled)));
            else
                set_param([model '/Electrolyzer_Manual_Override'], 'Value', '-1'); % Auto mode
            end
            fprintf('[CMD %d] Applied electrolyzer override.\n', cmd.id);
        end
    catch ME
        success = false;
        errReason = ME.message;
    end
end

%% Helper: Read Signal from Model Workspace / Outport
function val = read_signal(model, blockName, fallback)
    try
        val = str2double(get_param([model '/' blockName], 'Value'));
    catch
        val = fallback;
    end
end

%% Helper: Default Environment Variable
function val = getenv_default(key, defaultVal)
    env = getenv(key);
    if isempty(env)
        val = defaultVal;
    else
        val = env;
    end
end
```

---

## 7. Standalone Python Plant Simulator (`tools/fake_plant.py`)

For rapid testing, continuous integration, and automated acceptance verification without requiring a MATLAB license:

```bash
# Run simulator at 1x real-time
python tools/fake_plant.py

# Run simulator at 60x acceleration (1 minute simulated per real second)
python tools/fake_plant.py --accel 60 --duration 120

# Run simulator connecting with authentication token
python tools/fake_plant.py --url http://127.0.0.1:5000 --token my-secret-token
```

The simulator models the identical physical equations, setpoint updates, command acknowledgments, and telemetry schema specified in this document.
