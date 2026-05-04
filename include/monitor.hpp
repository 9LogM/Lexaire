#pragma once

#include <string>

// Flat view of the bridge's telemetry payload. Populated by ServicesWatcher
// from the telemetry PUB; rendered by the header and Live telemetry monitor.

struct TelemetrySnapshot {
    bool        connected      = false;
    bool        qgc_connected  = false;
    bool        armed          = false;
    std::string flight_mode    = "N/A";
    bool        has_battery    = false;
    int         battery_pct    = 0;
    float       battery_v      = 0;
    bool        has_fix        = false;
    double      latitude       = 0;
    double      longitude      = 0;
    float       abs_alt_m      = 0;
    float       rel_alt_m      = 0;
    float       roll_deg       = 0;
    float       pitch_deg      = 0;
    float       yaw_deg        = 0;
    float       ground_spd     = 0;
};
