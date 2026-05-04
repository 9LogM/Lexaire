#pragma once

// Lexaire inter-service message schema (C++ view).
//
// This file mirrors python/lexaire/messages.py. Both sides serialize to the
// same wire format: ZMQ multipart with frame 0 = UTF-8 JSON header and an
// optional binary payload in frame 1. C++ services use nlohmann::json (which
// MAVSDK already depends on) to encode/decode headers.

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace lexaire {

using nlohmann::json;

inline std::int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

// Rewrite a tcp endpoint's host to the wildcard `*` so a service can bind on
// every interface regardless of what its own configured hostname resolves to.
// Lets one config string serve both bind and connect sides — useful inside
// container networks where the bind host (service name) may resolve only to
// a private bridge IP.
inline std::string bind_endpoint(const std::string& ep) {
    const std::string prefix = "tcp://";
    if (ep.rfind(prefix, 0) != 0) return ep;
    const auto colon = ep.rfind(':');
    if (colon == std::string::npos || colon <= prefix.size()) return ep;
    return prefix + "*" + ep.substr(colon);
}

// Telemetry snapshot broadcast by the flight bridge.
struct Telemetry {
    std::int64_t ts_ns = 0;
    bool        connected      = false;
    bool        qgc_connected  = false;
    bool        armed          = false;
    std::string flight_mode    = "N/A";
    std::optional<int>    battery_pct;
    std::optional<float>  battery_v;
    std::optional<double> lat;
    std::optional<double> lon;
    std::optional<float>  abs_alt_m;
    std::optional<float>  rel_alt_m;
    float roll_deg  = 0;
    float pitch_deg = 0;
    float yaw_deg   = 0;
    float ground_speed_mps = 0;

    json to_json() const {
        json j = {
            {"ts_ns", ts_ns},
            {"connected", connected},
            {"qgc_connected", qgc_connected},
            {"armed", armed},
            {"flight_mode", flight_mode},
            {"battery_pct", battery_pct ? json(*battery_pct) : json(nullptr)},
            {"battery_v",   battery_v   ? json(*battery_v)   : json(nullptr)},
            {"lat",         lat         ? json(*lat)         : json(nullptr)},
            {"lon",         lon         ? json(*lon)         : json(nullptr)},
            {"abs_alt_m",   abs_alt_m   ? json(*abs_alt_m)   : json(nullptr)},
            {"rel_alt_m",   rel_alt_m   ? json(*rel_alt_m)   : json(nullptr)},
            {"roll_deg", roll_deg},
            {"pitch_deg", pitch_deg},
            {"yaw_deg", yaw_deg},
            {"ground_speed_mps", ground_speed_mps},
        };
        return j;
    }
};

// Tool call request/response (orchestrator <-> flight bridge, REQ/REP).
struct ToolCall {
    std::string request_id;
    std::string name;
    json        args;

    static ToolCall from_json(const json& j) {
        return ToolCall{
            j.value("request_id", std::string{}),
            j.value("name", std::string{}),
            j.contains("args") ? j["args"] : json::object(),
        };
    }
};

struct ToolResult {
    std::string request_id;
    bool        ok = false;
    std::string error;
    json        data = json::object();

    // Emit null (not "" / {}) for empty error / empty data so the wire
    // shape matches the Python ToolResult dataclass, which uses
    // Optional[str]=None / Any=None. Without this, the orchestrator
    // sees error="" on success — harmless today but a footgun for
    // anyone writing `if result.error:`-style checks.
    json to_json() const {
        return {
            {"request_id", request_id},
            {"ok", ok},
            {"error", error.empty() ? json(nullptr) : json(error)},
            {"data",  data.empty()  ? json(nullptr) : data},
        };
    }
};

}  // namespace lexaire
