#include "monitor.hpp"
#include <mavsdk/plugins/telemetry/telemetry.h>
#include <cmath>

using namespace mavsdk;

static std::string flight_mode_str(Telemetry::FlightMode m) {
    switch (m) {
        case Telemetry::FlightMode::Ready:          return "Ready";
        case Telemetry::FlightMode::Takeoff:        return "Takeoff";
        case Telemetry::FlightMode::Hold:           return "Hold";
        case Telemetry::FlightMode::Mission:        return "Mission";
        case Telemetry::FlightMode::ReturnToLaunch: return "Return to Launch";
        case Telemetry::FlightMode::Land:           return "Land";
        case Telemetry::FlightMode::Offboard:       return "Offboard";
        case Telemetry::FlightMode::FollowMe:       return "Follow Me";
        default:                                    return "Unknown";
    }
}

void setup_monitoring(Telemetry& telemetry,
                      std::shared_ptr<TelemetrySnapshot> snap,
                      std::function<void()> on_update) {
    telemetry.subscribe_armed([snap, on_update](bool armed) {
        snap->armed = armed;
        on_update();
    });

    telemetry.subscribe_flight_mode([snap, on_update](Telemetry::FlightMode m) {
        snap->flight_mode = flight_mode_str(m);
        on_update();
    });

    telemetry.subscribe_battery([snap, on_update](Telemetry::Battery b) {
        snap->has_battery = b.remaining_percent >= 0;
        if (snap->has_battery) {
            snap->battery_pct = static_cast<int>(b.remaining_percent);
            snap->battery_v   = b.voltage_v;
        }
        on_update();
    });

    telemetry.subscribe_position([snap, on_update](Telemetry::Position p) {
        snap->latitude   = p.latitude_deg;
        snap->longitude  = p.longitude_deg;
        snap->abs_alt_m  = p.absolute_altitude_m;
        snap->rel_alt_m  = p.relative_altitude_m;
        snap->has_fix    = true;
        on_update();
    });

    telemetry.subscribe_attitude_euler([snap, on_update](Telemetry::EulerAngle a) {
        snap->roll_deg  = a.roll_deg;
        snap->pitch_deg = a.pitch_deg;
        snap->yaw_deg   = a.yaw_deg;
        on_update();
    });

    telemetry.subscribe_velocity_ned([snap, on_update](Telemetry::VelocityNed v) {
        snap->ground_spd = std::sqrt(v.north_m_s * v.north_m_s +
                                     v.east_m_s  * v.east_m_s);
        on_update();
    });
}
