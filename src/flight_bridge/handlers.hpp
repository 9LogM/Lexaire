#pragma once

// Flight bridge tool handlers.
//
// Each handler takes a ToolCall (JSON args) and returns a ToolResult. The
// flight bridge dispatches on ToolCall.name; the safety envelope has already
// been enforced by the time a handler is invoked.

#include <memory>
#include <sstream>
#include <string>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/offboard/offboard.h>
#include <mavsdk/plugins/param/param.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

#include "lexaire/messages.hpp"
#include "lexaire/safety.hpp"

namespace lexaire {

struct FlightCtx {
    std::shared_ptr<mavsdk::System> system;
    std::unique_ptr<mavsdk::Action>    action;
    std::unique_ptr<mavsdk::Offboard>  offboard;
    std::unique_ptr<mavsdk::Param>     param;
    std::unique_ptr<mavsdk::Telemetry> telemetry;
    // Subscription handles — must outlive the ctx for callbacks to keep firing.
    mavsdk::Telemetry::StatusTextHandle status_text_handle{};
    mavsdk::Telemetry::ArmedHandle      armed_handle{};
    SafetyState* safety = nullptr;
};

inline std::string flight_mode_to_string(mavsdk::Telemetry::FlightMode m) {
    std::ostringstream oss;
    oss << m;
    return oss.str();
}

ToolResult dispatch(const ToolCall& call, FlightCtx& ctx);

}  // namespace lexaire
