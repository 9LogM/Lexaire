#include "handlers.hpp"

#include <chrono>
#include <cstdio>
#include <string>

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

ToolResult ok(const std::string& req, json data = json::object()) {
    return ToolResult{req, true, "", std::move(data)};
}

ToolResult err(const std::string& req, const std::string& msg) {
    return ToolResult{req, false, msg, json::object()};
}

// ---- Individual handlers ----

ToolResult handle_arm(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] arm\n"); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->arm();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_disarm(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] disarm\n"); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->disarm();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_takeoff(const ToolCall& c, FlightCtx& ctx) {
    double alt = c.args.value("altitude_m", 1.5);
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] takeoff alt=%.2f\n", alt); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    ctx.action->set_takeoff_altitude(static_cast<float>(alt));
    auto r = ctx.action->takeoff();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_land(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] land\n"); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->land();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_rtl(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] rtl\n"); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->return_to_launch();
    if (r == Action::Result::Success) return ok(c.request_id);
    return err(c.request_id, action_result_to_string(r));
}

ToolResult handle_hold(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] hold\n"); return ok(c.request_id); }
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
    if (ctx.dummy) {
        std::fprintf(stderr, "[flight-bridge DUMMY] goto_ned n=%.2f e=%.2f d=%.2f yaw=%.1f\n", n, e, d, yaw);
        return ok(c.request_id);
    }
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
    if (ctx.dummy) {
        std::fprintf(stderr, "[flight-bridge DUMMY] set_velocity_ned vx=%.2f vy=%.2f vz=%.2f yr=%.1f\n", vx, vy, vz, yr);
        return ok(c.request_id);
    }
    if (!ctx.offboard) return err(c.request_id, "offboard_not_initialized");
    Offboard::VelocityNedYaw v{static_cast<float>(vx), static_cast<float>(vy),
                                 static_cast<float>(vz), static_cast<float>(yr)};
    ctx.offboard->set_velocity_ned(v);
    auto r = ctx.offboard->start();
    if (r == Offboard::Result::Success || r == Offboard::Result::Busy) return ok(c.request_id);
    return err(c.request_id, offboard_result_to_string(r));
}

ToolResult handle_abort(const ToolCall& c, FlightCtx& ctx) {
    if (ctx.safety) ctx.safety->aborted.store(true);
    if (ctx.dummy) { std::fprintf(stderr, "[flight-bridge DUMMY] abort -> disarm\n"); return ok(c.request_id); }
    if (!ctx.action) return err(c.request_id, "action_not_initialized");
    auto r = ctx.action->kill();
    if (r == Action::Result::Success) return ok(c.request_id);
    // Fall back to land if kill is rejected.
    return err(c.request_id, action_result_to_string(r));
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
        {"flight_mode", to_string(ctx.telemetry->flight_mode())},
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
        {"set_velocity",      &handle_set_velocity_ned},
        {"abort",             &handle_abort},
        {"get_telemetry",     &handle_get_telemetry},
    };
    auto it = kHandlers.find(call.name);
    if (it == kHandlers.end()) return err(call.request_id, "unknown_tool: " + call.name);
    return it->second(call, ctx);
}

}  // namespace lexaire
