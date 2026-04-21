#pragma once

// Flight bridge tool handlers.
//
// Each handler takes a ToolCall (JSON args) and returns a ToolResult. The
// flight bridge dispatches on ToolCall.name; the safety envelope has already
// been enforced by the time a handler is invoked.
//
// In dummy mode, handlers log the requested action and return ok without
// commanding the drone — useful for developing the orchestrator end-to-end
// without a live aircraft.

#include <memory>
#include <string>
#include <unordered_map>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/offboard/offboard.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

#include "lexaire/messages.hpp"
#include "lexaire/safety.hpp"

namespace lexaire {

struct FlightCtx {
    std::shared_ptr<mavsdk::System> system;
    std::unique_ptr<mavsdk::Action>    action;
    std::unique_ptr<mavsdk::Offboard>  offboard;
    std::unique_ptr<mavsdk::Telemetry> telemetry;
    bool dummy = false;
    SafetyState* safety = nullptr;
};

ToolResult dispatch(const ToolCall& call, FlightCtx& ctx);

}  // namespace lexaire
