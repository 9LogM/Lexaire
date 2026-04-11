#pragma once
#include <functional>
#include <string>
#include <memory>

namespace mavsdk { class Telemetry; }

struct TelemetrySnapshot {
    // Status
    bool        armed        = false;
    std::string flight_mode  = "N/A";
    // Power
    bool        has_battery  = false;
    int         battery_pct  = 0;
    float       battery_v    = 0;
    // Position
    bool        has_fix      = false;
    double      latitude     = 0;
    double      longitude    = 0;
    float       abs_alt_m    = 0;
    float       rel_alt_m    = 0;
    // Attitude
    float       roll_deg     = 0;
    float       pitch_deg    = 0;
    float       yaw_deg      = 0;
    // Speed
    float       ground_spd   = 0;  // m/s
};

void setup_monitoring(mavsdk::Telemetry& telemetry,
                      std::shared_ptr<TelemetrySnapshot> snap,
                      std::function<void()> on_update);
