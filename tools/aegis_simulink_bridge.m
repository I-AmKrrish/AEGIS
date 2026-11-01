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
