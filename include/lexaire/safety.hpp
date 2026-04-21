#pragma once

// C++ mirror of python/lexaire/safety.py. The flight bridge applies these
// checks below the tool-call layer — regardless of what the VLM decides, a
// tool call that violates the envelope is rejected.

#include <atomic>
#include <cmath>
#include <limits>
#include <string>

#include <nlohmann/json.hpp>

#include "lexaire/config.hpp"

namespace lexaire {

struct SafetyEnvelope {
    double max_altitude_m          = 5.0;
    double geofence_radius_m       = 10.0;
    double max_velocity_mps        = 1.5;
    double min_obstacle_distance_m = 0.5;
    bool   require_spoken_arm      = true;
    std::string abort_keyword      = "abort";

    static SafetyEnvelope from_config(const Config& cfg) {
        SafetyEnvelope e;
        e.max_altitude_m          = cfg.get_or<double>("safety.max_altitude_m",          e.max_altitude_m);
        e.geofence_radius_m       = cfg.get_or<double>("safety.geofence_radius_m",       e.geofence_radius_m);
        e.max_velocity_mps        = cfg.get_or<double>("safety.max_velocity_mps",        e.max_velocity_mps);
        e.min_obstacle_distance_m = cfg.get_or<double>("safety.min_obstacle_distance_m", e.min_obstacle_distance_m);
        e.require_spoken_arm      = cfg.get_or<bool>  ("safety.require_spoken_arm",      e.require_spoken_arm);
        e.abort_keyword           = cfg.get_or<std::string>("stt.abort_keyword",         e.abort_keyword);
        return e;
    }
};

struct SafetyState {
    std::atomic<bool>  aborted{false};
    std::atomic<bool>  armed_with_voice{false};
    std::atomic<double> current_rel_alt_m{0.0};
    std::atomic<double> min_obstacle_m{std::numeric_limits<double>::infinity()};
};

struct SafetyDecision {
    bool allow = true;
    std::string reason;
};

inline SafetyDecision check_tool(const std::string& name,
                                  const nlohmann::json& args,
                                  const SafetyEnvelope& env,
                                  const SafetyState& state) {
    if (state.aborted.load()) return {false, "abort_active"};

    if ((name == "arm" || name == "takeoff") &&
        env.require_spoken_arm && !state.armed_with_voice.load()) {
        return {false, "spoken_arm_required"};
    }

    auto as_d = [&](const char* k, double def = 0.0) -> double {
        if (!args.contains(k)) return def;
        const auto& v = args[k];
        if (v.is_number()) return v.get<double>();
        return def;
    };

    if (name == "takeoff") {
        double alt = as_d("altitude_m", 0.0);
        if (alt > env.max_altitude_m)
            return {false, "altitude_exceeds_max"};
    }

    if (name == "goto_ned" || name == "goto_global") {
        if (args.contains("d")) {
            double rel_alt = -as_d("d");
            if (rel_alt > env.max_altitude_m)
                return {false, "altitude_exceeds_max"};
        }
        double n = as_d("n"), e = as_d("e");
        double r = std::sqrt(n * n + e * e);
        if (r > env.geofence_radius_m)
            return {false, "geofence_breach"};
    }

    if (name == "set_velocity" || name == "set_velocity_ned") {
        double vx = as_d("vx"), vy = as_d("vy"), vz = as_d("vz");
        double s = std::sqrt(vx * vx + vy * vy + vz * vz);
        if (s > env.max_velocity_mps)
            return {false, "velocity_exceeds_max"};
    }

    if (state.min_obstacle_m.load() < env.min_obstacle_distance_m &&
        (name == "goto_ned" || name == "goto_global" ||
         name == "set_velocity" || name == "set_velocity_ned" ||
         name == "takeoff")) {
        return {false, "obstacle_too_close"};
    }

    return {true, ""};
}

}  // namespace lexaire
