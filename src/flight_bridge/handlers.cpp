#include "handlers.hpp"

#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

using namespace mavsdk;
using lexaire::json;

namespace lexaire {

namespace {

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

ToolResult handle_arm(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->arm();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_disarm(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->disarm();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_takeoff(const ToolCall& c, FlightCtx& ctx) {
    double alt = c.args.value("altitude_m", 1.5);
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    ctx.action->set_takeoff_altitude(static_cast<float>(alt));
    auto r = ctx.action->takeoff();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_land(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->land();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_rtl(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->return_to_launch();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_hold(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->hold();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_goto_ned(const ToolCall& c, FlightCtx& ctx) {
    double n = c.args.value("n", 0.0);
    double e = c.args.value("e", 0.0);
    double d = c.args.value("d", 0.0);
    double yaw = c.args.value("yaw_deg", 0.0);
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");
    Offboard::PositionNedYaw p{static_cast<float>(n), static_cast<float>(e),
                                 static_cast<float>(d), static_cast<float>(yaw)};
    ctx.offboard->set_position_ned(p);
    auto r = ctx.offboard->start();
    if (r == Offboard::Result::Success || r == Offboard::Result::Busy) return ok(c.request_id);
    return err(c.request_id, offboard_result_to_string(r));
}

ToolResult handle_set_velocity_ned(const ToolCall& c, FlightCtx& ctx) {
    double vx = c.args.value("vx", 0.0);
    double vy = c.args.value("vy", 0.0);
    double vz = c.args.value("vz", 0.0);
    double yr = c.args.value("yaw_rate_deg_s", 0.0);
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");
    Offboard::VelocityNedYaw v{static_cast<float>(vx), static_cast<float>(vy),
                                 static_cast<float>(vz), static_cast<float>(yr)};
    ctx.offboard->set_velocity_ned(v);
    auto r = ctx.offboard->start();
    if (r == Offboard::Result::Success || r == Offboard::Result::Busy) return ok(c.request_id);
    return err(c.request_id, offboard_result_to_string(r));
}

// Pilot-voice safe-stop: controlled descent and disarm-on-touchdown.
// For an instant motor-cut, see `handle_kill`.
ToolResult handle_abort(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.safety) ctx.safety->aborted.store(true);
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->land();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

// Instant motor cut. Reserved for emergencies where a controlled descent
// is unsafe (e.g. drone about to strike a person).
ToolResult handle_kill(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.safety) ctx.safety->aborted.store(true);
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->kill();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

// Operator-only; not in tool_schemas() — the VLM cannot reach it.
ToolResult handle_set_param(const ToolCall& c, FlightCtx& ctx) {
    std::string name = c.args.value("name", "");
    if (name.empty()) return err(c.request_id, "missing_param_name");
    if (!ctx.param) return err(c.request_id, "param_not_initialized");

    if (c.args.contains("int_value")) {
        int v = c.args.value("int_value", 0);
        auto r = ctx.param->set_param_int(name, v);
        if (r == Param::Result::Success) {
            std::fprintf(stderr, "[flight-bridge] set_param %s=%d ok\n", name.c_str(), v);
            return ok(c.request_id);
        }
        return err(c.request_id, "param_set_failed:" + param_result_to_string(r));
    }
    if (c.args.contains("float_value")) {
        float v = c.args.value("float_value", 0.0f);
        auto r = ctx.param->set_param_float(name, v);
        if (r == Param::Result::Success) {
            std::fprintf(stderr, "[flight-bridge] set_param %s=%g ok\n", name.c_str(), v);
            return ok(c.request_id);
        }
        return err(c.request_id, "param_set_failed:" + param_result_to_string(r));
    }
    return err(c.request_id, "missing_value");
}

ToolResult handle_get_telemetry(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.telemetry) return err(c.request_id, "telemetry_not_initialized");
    auto pos = ctx.telemetry->position();
    auto att = ctx.telemetry->attitude_euler();
    auto vel = ctx.telemetry->velocity_ned();
    json data = {
        {"lat", pos.latitude_deg},
        {"lon", pos.longitude_deg},
        {"abs_alt_m", pos.absolute_altitude_m},
        {"rel_alt_m", pos.relative_altitude_m},
        {"roll_deg", att.roll_deg},
        {"pitch_deg", att.pitch_deg},
        {"yaw_deg", att.yaw_deg},
        {"vn_mps", vel.north_m_s},
        {"ve_mps", vel.east_m_s},
        {"vd_mps", vel.down_m_s},
        {"armed", ctx.telemetry->armed()},
        {"flight_mode", flight_mode_to_string(ctx.telemetry->flight_mode())},
    };
    return ok(c.request_id, std::move(data));
}

// Seed a zero-velocity setpoint, enter OFFBOARD, and hold for >1s so PX4's
// proof-of-life check passes before a follow-up arm.
// Ref: docs.px4.io/main/en/flight_modes/offboard
ToolResult handle_enable_offboard(const ToolCall& c, FlightCtx& ctx) {
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");

    Offboard::VelocityNedYaw zero{0.0f, 0.0f, 0.0f, 0.0f};
    ctx.offboard->set_velocity_ned(zero);
    auto r = ctx.offboard->start();
    if (r != Offboard::Result::Success && r != Offboard::Result::Busy) {
        return err(c.request_id, "offboard_start_failed:" + offboard_result_to_string(r));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    std::fprintf(stderr, "[flight-bridge] offboard active\n");
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
    auto [r2, v2] = ctx.param->get_param_float(name);
    if (r2 == Param::Result::Success) {
        return ok(c.request_id, json{{"float_value", v2}});
    }
    return err(c.request_id, "param_get_failed:" + param_result_to_string(r));
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
        {"get_telemetry",     &handle_get_telemetry},
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
