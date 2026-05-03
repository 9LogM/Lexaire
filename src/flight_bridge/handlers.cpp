#include "handlers.hpp"

#include <cstdio>
#include <optional>
#include <string>
#include <unordered_map>

using namespace mavsdk;
using lexaire::json;

namespace lexaire {

namespace {

// `nlohmann::json::value(key, default)` returns the default when the key is
// missing OR when the stored value can't be converted to the requested type.
// That silently masks typos and wrong-typed args from the VLM (e.g. a
// hallucinated "alt_m" on a takeoff would have triggered a 1.5 m takeoff
// instead of erroring out). The helpers below give "either the value or a
// clear error string" semantics so wrong types fail loud at the handler.

// Required numeric arg. ok=false on missing OR wrong type.
struct DoubleArg { bool ok; double value; std::string error; };
DoubleArg require_double(const json& args, const char* key) {
    if (!args.contains(key)) return {false, 0.0, std::string("missing:") + key};
    const auto& v = args[key];
    if (!v.is_number())      return {false, 0.0, std::string("wrong_type:") + key};
    return {true, v.get<double>(), ""};
}

// Optional numeric arg. ok=true with present=false when absent (caller
// uses default). ok=false ONLY when present-but-wrong-typed.
struct OptDoubleArg { bool ok; bool present; double value; std::string error; };
OptDoubleArg optional_double(const json& args, const char* key) {
    if (!args.contains(key)) return {true, false, 0.0, ""};
    const auto& v = args[key];
    if (!v.is_number())      return {false, false, 0.0, std::string("wrong_type:") + key};
    return {true, true, v.get<double>(), ""};
}

std::string action_result_to_string(Action::Result r) {
    switch (r) {
        case Action::Result::Success: return "success";
        case Action::Result::NoSystem: return "no_system";
        case Action::Result::ConnectionError: return "connection_error";
        case Action::Result::Busy: return "busy";
        case Action::Result::CommandDenied: return "command_denied";
        case Action::Result::CommandDeniedLandedStateUnknown: return "command_denied_landed_state_unknown";
        case Action::Result::CommandDeniedNotLanded: return "command_denied_not_landed";
        case Action::Result::Timeout: return "timeout";
        case Action::Result::VtolTransitionSupportUnknown: return "vtol_unknown";
        case Action::Result::NoVtolTransitionSupport: return "no_vtol";
        case Action::Result::ParameterError: return "parameter_error";
        case Action::Result::Unsupported: return "unsupported";
        default: return "unknown";
    }
}

std::string offboard_result_to_string(Offboard::Result r) {
    switch (r) {
        case Offboard::Result::Success: return "success";
        case Offboard::Result::NoSystem: return "no_system";
        case Offboard::Result::ConnectionError: return "connection_error";
        case Offboard::Result::Busy: return "busy";
        case Offboard::Result::CommandDenied: return "command_denied";
        case Offboard::Result::Timeout: return "timeout";
        case Offboard::Result::NoSetpointSet: return "no_setpoint";
        default: return "unknown";
    }
}

std::string param_result_to_string(Param::Result r) {
    switch (r) {
        case Param::Result::Success: return "success";
        case Param::Result::Timeout: return "timeout";
        case Param::Result::ConnectionError: return "connection_error";
        case Param::Result::WrongType: return "wrong_type";
        case Param::Result::ParamNameTooLong: return "param_name_too_long";
        case Param::Result::NoSystem: return "no_system";
        case Param::Result::ParamValueTooLong: return "param_value_too_long";
        default: return "unknown";
    }
}

ToolResult ok(const std::string& req, json data = json::object()) {
    return ToolResult{req, true, "", std::move(data)};
}

ToolResult err(const std::string& req, const std::string& msg) {
    return ToolResult{req, false, msg, json::object()};
}

// ---- Individual handlers ----

// Common body for arg-less Action plugin calls (arm/disarm/land/RTL/hold).
// One thin handler per tool name (rather than a function template) keeps
// the dispatch table's value type uniform: `ToolResult(*)(...)`.
ToolResult call_action_method(const ToolCall& c, FlightCtx& ctx,
                                Action::Result (Action::*method)() const) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = (ctx.action.get()->*method)();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_arm   (const ToolCall& c, FlightCtx& ctx) { return call_action_method(c, ctx, &Action::arm); }
ToolResult handle_disarm(const ToolCall& c, FlightCtx& ctx) { return call_action_method(c, ctx, &Action::disarm); }
ToolResult handle_land  (const ToolCall& c, FlightCtx& ctx) { return call_action_method(c, ctx, &Action::land); }
ToolResult handle_rtl   (const ToolCall& c, FlightCtx& ctx) { return call_action_method(c, ctx, &Action::return_to_launch); }
ToolResult handle_hold  (const ToolCall& c, FlightCtx& ctx) { return call_action_method(c, ctx, &Action::hold); }

ToolResult handle_takeoff(const ToolCall& c, FlightCtx& ctx) {
    auto alt = require_double(c.args, "altitude_m");
    if (!alt.ok) return err(c.request_id, alt.error);
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    ctx.action->set_takeoff_altitude(static_cast<float>(alt.value));
    auto r = ctx.action->takeoff();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_goto_ned(const ToolCall& c, FlightCtx& ctx) {
    auto n = require_double(c.args, "n");
    if (!n.ok) return err(c.request_id, n.error);
    auto e = require_double(c.args, "e");
    if (!e.ok) return err(c.request_id, e.error);
    auto d = require_double(c.args, "d");
    if (!d.ok) return err(c.request_id, d.error);
    auto yaw = optional_double(c.args, "yaw_deg");
    if (!yaw.ok) return err(c.request_id, yaw.error);
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");
    // PositionNedYaw has no "hold heading" sentinel — an absent yaw_deg
    // without this default snaps to 0° (north) on every translate.
    float yaw_deg = yaw.present
        ? static_cast<float>(yaw.value)
        : (ctx.telemetry ? ctx.telemetry->attitude_euler().yaw_deg : 0.0f);
    Offboard::PositionNedYaw p{static_cast<float>(n.value), static_cast<float>(e.value),
                                 static_cast<float>(d.value), yaw_deg};
    ctx.offboard->set_position_ned(p);
    auto r = ctx.offboard->start();
    if (r == Offboard::Result::Success || r == Offboard::Result::Busy) return ok(c.request_id);
    return err(c.request_id, offboard_result_to_string(r));
}

ToolResult handle_set_velocity_ned(const ToolCall& c, FlightCtx& ctx) {
    // At least one of vx/vy/vz must be PRESENT in the args. The check
    // is "all three absent", not "all three zero" — an explicit
    // {vx:0, vy:0, vz:0} is a legitimate hover setpoint the operator
    // may want; the case we reject is the no-arg call that would
    // accidentally override an active autonomous mode without intent.
    auto vx = optional_double(c.args, "vx");
    if (!vx.ok) return err(c.request_id, vx.error);
    auto vy = optional_double(c.args, "vy");
    if (!vy.ok) return err(c.request_id, vy.error);
    auto vz = optional_double(c.args, "vz");
    if (!vz.ok) return err(c.request_id, vz.error);
    if (!vx.present && !vy.present && !vz.present) {
        return err(c.request_id, "missing_velocity_component");
    }
    auto yaw = optional_double(c.args, "yaw_deg");
    if (!yaw.ok) return err(c.request_id, yaw.error);
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");
    // Default heading to current yaw when absent — see handle_goto_ned.
    float yaw_deg = yaw.present
        ? static_cast<float>(yaw.value)
        : (ctx.telemetry ? ctx.telemetry->attitude_euler().yaw_deg : 0.0f);
    Offboard::VelocityNedYaw v{static_cast<float>(vx.value), static_cast<float>(vy.value),
                                 static_cast<float>(vz.value), yaw_deg};
    ctx.offboard->set_velocity_ned(v);
    auto r = ctx.offboard->start();
    if (r == Offboard::Result::Success || r == Offboard::Result::Busy) return ok(c.request_id);
    return err(c.request_id, offboard_result_to_string(r));
}

// Pilot-voice safe-stop: controlled descent and disarm-on-touchdown.
// For an instant motor-cut, see `handle_kill`.
ToolResult handle_abort(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    // Skip the latch on the ground — no armed falling-edge to clear it,
    // so it would block the next legitimate arm with abort_active.
    const bool in_air = ctx.telemetry && ctx.telemetry->in_air();
    if (in_air && ctx.safety) ctx.safety->aborted.store(true);
    auto r = ctx.action->land();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

// Instant motor cut. Reserved for emergencies where a controlled descent
// is unsafe (e.g. drone about to strike a person).
ToolResult handle_kill(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    const bool in_air = ctx.telemetry && ctx.telemetry->in_air();
    if (in_air && ctx.safety) ctx.safety->aborted.store(true);
    auto r = ctx.action->kill();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

// ---- Operator-only handlers ----
// The dispatch table includes the handlers below, but tool_schemas() in
// python/services/orchestrator/tools.py does NOT, so the VLM can't reach
// them. They're the bridge's escape hatch for talking to the REQ socket
// directly. Do not add to tool_schemas() without thinking through the
// safety implications.

ToolResult handle_set_param(const ToolCall& c, FlightCtx& ctx) {
    std::string name = c.args.value("name", "");
    if (name.empty()) return err(c.request_id, "missing_param_name");
    if (!ctx.param) return err(c.request_id, "param_not_initialized");

    // Use the typed helpers — a hallucinated `"int_value": "5"` would
    // otherwise coerce to 0 via `args.value()` and silently zero the
    // flight param.
    if (c.args.contains("int_value")) {
        auto v = require_double(c.args, "int_value");
        if (!v.ok) return err(c.request_id, v.error);
        int iv = static_cast<int>(v.value);
        auto r = ctx.param->set_param_int(name, iv);
        if (r == Param::Result::Success) {
            std::fprintf(stderr, "[flight-bridge] set_param %s=%d ok\n", name.c_str(), iv);
            return ok(c.request_id);
        }
        return err(c.request_id, "param_set_failed:" + param_result_to_string(r));
    }
    if (c.args.contains("float_value")) {
        auto v = require_double(c.args, "float_value");
        if (!v.ok) return err(c.request_id, v.error);
        float fv = static_cast<float>(v.value);
        auto r = ctx.param->set_param_float(name, fv);
        if (r == Param::Result::Success) {
            std::fprintf(stderr, "[flight-bridge] set_param %s=%g ok\n", name.c_str(), fv);
            return ok(c.request_id);
        }
        return err(c.request_id, "param_set_failed:" + param_result_to_string(r));
    }
    return err(c.request_id, "missing_value");
}

// Seeds a zero-velocity setpoint and enters OFFBOARD. MAVSDK keeps the
// setpoint streaming at ~50 Hz from here. Returns immediately; PX4 needs
// roughly a second of streamed setpoints before it'll accept a follow-up
// arm, so the operator/orchestrator should retry on the first
// command_denied if it races the mode transition.
// Ref: docs.px4.io/main/en/flight_modes/offboard
ToolResult handle_enable_offboard(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");

    Offboard::VelocityNedYaw zero{0.0f, 0.0f, 0.0f, 0.0f};
    ctx.offboard->set_velocity_ned(zero);
    auto r = ctx.offboard->start();
    if (r != Offboard::Result::Success && r != Offboard::Result::Busy) {
        return err(c.request_id, "offboard_start_failed:" + offboard_result_to_string(r));
    }
    std::fprintf(stderr, "[flight-bridge] offboard initiated\n");
    return ok(c.request_id);
}

ToolResult handle_get_param(const ToolCall& c, FlightCtx& ctx) {
    std::string name = c.args.value("name", "");
    if (name.empty()) return err(c.request_id, "missing_param_name");
    if (!ctx.param) return err(c.request_id, "param_not_initialized");
    auto [r, v] = ctx.param->get_param_int(name);
    if (r == Param::Result::Success) {
        return ok(c.request_id, json{{"int_value", v}});
    }
    // Only fall through on type mismatch — a timeout / connection error
    // would otherwise be masked by the float retry.
    if (r != Param::Result::WrongType) {
        return err(c.request_id, "param_get_failed:" + param_result_to_string(r));
    }
    auto [r2, v2] = ctx.param->get_param_float(name);
    if (r2 == Param::Result::Success) {
        return ok(c.request_id, json{{"float_value", v2}});
    }
    return err(c.request_id, "param_get_failed:" + param_result_to_string(r2));
}

// Operator-only diagnostic: dump MAVSDK's pre-arm health flags.
ToolResult handle_get_health(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.telemetry) return err(c.request_id, "telemetry_not_initialized");
    auto h = ctx.telemetry->health();
    json data = {
        {"is_armable", h.is_armable},
        {"is_gyrometer_calibration_ok", h.is_gyrometer_calibration_ok},
        {"is_accelerometer_calibration_ok", h.is_accelerometer_calibration_ok},
        {"is_magnetometer_calibration_ok", h.is_magnetometer_calibration_ok},
        {"is_local_position_ok", h.is_local_position_ok},
        {"is_global_position_ok", h.is_global_position_ok},
        {"is_home_position_ok", h.is_home_position_ok},
    };
    return ok(c.request_id, std::move(data));
}

}  // namespace

ToolResult dispatch(const ToolCall& call, FlightCtx& ctx) {
    static const std::unordered_map<std::string,
        ToolResult(*)(const ToolCall&, FlightCtx&)> kHandlers = {
        {"arm",               &handle_arm},
        {"disarm",            &handle_disarm},
        {"takeoff",           &handle_takeoff},
        {"land",              &handle_land},
        {"return_to_launch",  &handle_rtl},
        {"hold",              &handle_hold},
        {"goto_ned",          &handle_goto_ned},
        {"set_velocity_ned",  &handle_set_velocity_ned},
        {"abort",             &handle_abort},
        {"kill",              &handle_kill},
        {"set_param",         &handle_set_param},
        {"get_param",         &handle_get_param},
        {"get_health",        &handle_get_health},
        {"enable_offboard",   &handle_enable_offboard},
    };
    auto it = kHandlers.find(call.name);
    if (it == kHandlers.end()) return err(call.request_id, "unknown_tool: " + call.name);
    return it->second(call, ctx);
}

}  // namespace lexaire
