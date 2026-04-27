#pragma once

// Safety envelope enforced by the flight bridge below the tool-call layer.
// Regardless of what the VLM decides, a tool call that violates the envelope
// is rejected.

#include <atomic>
#include <cmath>
#include <string>

#include <nlohmann/json.hpp>

#include "lexaire/config.hpp"

namespace lexaire {

struct SafetyEnvelope {
    double max_altitude_m     = 5.0;
    double geofence_radius_m  = 10.0;
    double max_velocity_mps   = 1.5;
    bool   require_spoken_arm = true;

    static SafetyEnvelope from_config(const Config& cfg) {
        SafetyEnvelope e;
        e.max_altitude_m     = cfg.get_or<double>("safety.max_altitude_m",     e.max_altitude_m);
        e.geofence_radius_m  = cfg.get_or<double>("safety.geofence_radius_m",  e.geofence_radius_m);
        e.max_velocity_mps   = cfg.get_or<double>("safety.max_velocity_mps",   e.max_velocity_mps);
        e.require_spoken_arm = cfg.get_or<bool>  ("safety.require_spoken_arm", e.require_spoken_arm);
        return e;
    }
};

struct SafetyState {
    std::atomic<bool> aborted{false};
    std::atomic<bool> armed_with_voice{false};
};

struct SafetyDecision {
    bool allow = true;
    std::string reason;
};

inline SafetyDecision check_tool(const std::string& name,
                                  const nlohmann::json& args,
                                  const SafetyEnvelope& env,
                                  const SafetyState& state) {
    // Tools that must reach the bridge even when the abort latch is set:
    //   kill / land — the strictly-worse escalations of abort.
    //   disarm      — the operator's "I'm done, clear the latch" path; if
    //                 we deny it, the latch never clears (the dispatch's
    //                 on-success clear runs only when the gate passes).
    const bool is_emergency_followup =
        (name == "kill" || name == "land" || name == "disarm");
    if (state.aborted.load() && !is_emergency_followup) {
        return {false, "abort_active"};
    }

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

    if (name == "goto_ned") {
        // Contract: n/e/d are PX4 local NED, origin at home (the arm
        // location). If a future tool ever introduces "delta from current"
        // semantics, the geofence math here silently becomes wrong.
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

    if (name == "set_velocity_ned") {
        double vx = as_d("vx"), vy = as_d("vy"), vz = as_d("vz");
        double s = std::sqrt(vx * vx + vy * vy + vz * vz);
        if (s > env.max_velocity_mps)
            return {false, "velocity_exceeds_max"};
    }

    return {true, ""};
}

}  // namespace lexaire
