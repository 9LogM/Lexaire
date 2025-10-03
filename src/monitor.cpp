#include "monitor.hpp"

#include <iostream>

#include <mavsdk/plugins/telemetry/telemetry.h>

using namespace mavsdk;

static std::string flight_mode_to_str(Telemetry::FlightMode mode) {
    switch (mode) {
        case Telemetry::FlightMode::Unknown:        return "Unknown";
        case Telemetry::FlightMode::Ready:          return "Ready";
        case Telemetry::FlightMode::Takeoff:        return "Takeoff";
        case Telemetry::FlightMode::Hold:           return "Hold";
        case Telemetry::FlightMode::Mission:        return "Mission";
        case Telemetry::FlightMode::ReturnToLaunch: return "ReturnToLaunch";
        case Telemetry::FlightMode::Land:           return "Land";
        case Telemetry::FlightMode::Offboard:       return "Offboard";
        case Telemetry::FlightMode::FollowMe:       return "FollowMe";
        default:                                    return "Other";
    }
}

void setup_monitoring(Telemetry &telemetry) {
    telemetry.subscribe_flight_mode([](Telemetry::FlightMode flight_mode) {
        std::cout << "Flight Mode: " << flight_mode_to_str(flight_mode) << std::endl;
    });

    telemetry.subscribe_battery([](Telemetry::Battery battery) {
        std::cout << "Battery: " << battery.remaining_percent << " %" << std::endl;
    });
}