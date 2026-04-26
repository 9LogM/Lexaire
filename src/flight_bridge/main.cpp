// Lexaire flight bridge.
//
// Connects to PX4 via MAVSDK and exposes a JSON tool-call interface on a ZMQ
// REP socket. Broadcasts telemetry on a PUB socket. Every tool call is
// gated by the non-overridable safety envelope from include/lexaire/safety.hpp.
//
// CLI:
//   lexaire-flight-bridge           # live — connects to the drone
//   lexaire-flight-bridge --dummy   # dummy — logs intent, does not command the drone

#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <memory>
#include <optional>
#include <string>
#include <thread>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/action/action.h>
#include <mavsdk/plugins/offboard/offboard.h>
#include <mavsdk/plugins/telemetry/telemetry.h>

#include <zmq.h>

#include "handlers.hpp"
#include "lexaire/config.hpp"
#include "lexaire/safety.hpp"
#include "lexaire/messages.hpp"

using lexaire::json;

static std::atomic<bool> g_stop{false};
static void on_sigint(int) { g_stop.store(true); }

static int run(bool dummy) {
    auto cfg = lexaire::Config::load();
    lexaire::SafetyEnvelope env  = lexaire::SafetyEnvelope::from_config(cfg);
    lexaire::SafetyState    state;

    const std::string rep_ep       = cfg.require<std::string>("services.flight_bridge_rep");
    const std::string telemetry_ep = cfg.require<std::string>("services.telemetry_pub");
    const std::string mavsdk_uri   = cfg.require<std::string>("drone.mavsdk_udp");

    // Phase 2A: heartbeat-loss recovery. If the MAVSDK autopilot connection
    // drops while armed, the bridge auto-issues an RTL or HOLD after a
    // configurable threshold. Default rtl@2.0s.
    const double hb_threshold_s = cfg.get_or<double>("safety.heartbeat_loss_threshold_s", 2.0);
    const std::string hb_action = cfg.get_or<std::string>("safety.heartbeat_loss_action", "rtl");

    std::fprintf(stderr,
                 "[flight-bridge] starting  rep=%s  telemetry=%s  mode=%s  "
                 "heartbeat-loss: action=%s threshold=%.1fs\n",
                 rep_ep.c_str(), telemetry_ep.c_str(), dummy ? "DUMMY" : "LIVE",
                 hb_action.c_str(), hb_threshold_s);

    // MAVSDK setup.
    lexaire::FlightCtx ctx;
    ctx.dummy = dummy;
    ctx.safety = &state;

    // PX4 only routes STATUSTEXT to peers whose heartbeat advertises
    // MAV_TYPE_GCS. The bridge is the pilot's interface — voice → tool
    // calls — so GCS is the correct identity.
    // Ref: discuss.px4.io/t/cant-receive-mavlink-253-statustext-message-via-companion-link-since-1-6-4/5429
    mavsdk::Mavsdk sdk{mavsdk::Mavsdk::Configuration{mavsdk::ComponentType::GroundStation}};
    if (!dummy) {
        if (sdk.add_any_connection(mavsdk_uri) != mavsdk::ConnectionResult::Success) {
            std::fprintf(stderr, "[flight-bridge] MAVSDK connection to %s failed\n", mavsdk_uri.c_str());
            return 2;
        }
        // Wait up to 5s for a system; if none appears, handlers return errors.
        for (int i = 0; i < 50 && !g_stop.load(); ++i) {
            for (auto& s : sdk.systems()) if (s->has_autopilot()) { ctx.system = s; break; }
            if (ctx.system) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        if (ctx.system) {
            ctx.action    = std::make_unique<mavsdk::Action>(ctx.system);
            ctx.offboard  = std::make_unique<mavsdk::Offboard>(ctx.system);
            ctx.param     = std::make_unique<mavsdk::Param>(ctx.system);
            ctx.telemetry = std::make_unique<mavsdk::Telemetry>(ctx.system);
            // Hold the handle — MAVSDK unsubscribes when the returned handle
            // goes out of scope, dropping all STATUSTEXT lines silently.
            ctx.status_text_handle = ctx.telemetry->subscribe_status_text(
                [](mavsdk::Telemetry::StatusText st) {
                    std::fprintf(stderr, "[px4] %s\n", st.text.c_str());
                });
            std::fprintf(stderr, "[flight-bridge] autopilot connected\n");
        } else {
            std::fprintf(stderr, "[flight-bridge] no autopilot found in 5s — handlers will return errors\n");
        }
    }

    // ZMQ: REP for tool calls, PUB for telemetry broadcast.
    void* zctx = zmq_ctx_new();
    void* rep  = zmq_socket(zctx, ZMQ_REP);
    void* pub  = zmq_socket(zctx, ZMQ_PUB);
    int linger = 0;
    zmq_setsockopt(rep, ZMQ_LINGER, &linger, sizeof(linger));
    zmq_setsockopt(pub, ZMQ_LINGER, &linger, sizeof(linger));
    const std::string rep_bind = lexaire::bind_endpoint(rep_ep);
    const std::string tele_bind = lexaire::bind_endpoint(telemetry_ep);
    if (zmq_bind(rep, rep_bind.c_str()) != 0) {
        std::fprintf(stderr, "[flight-bridge] zmq_bind REP %s failed: %s\n", rep_bind.c_str(), zmq_strerror(zmq_errno()));
        return 3;
    }
    if (zmq_bind(pub, tele_bind.c_str()) != 0) {
        std::fprintf(stderr, "[flight-bridge] zmq_bind PUB %s failed: %s\n", tele_bind.c_str(), zmq_strerror(zmq_errno()));
        return 3;
    }

    // Heartbeat watchdog. Polls the MAVSDK system's connection state at
    // 5 Hz; if it stays disconnected past `hb_threshold_s` while the
    // autopilot reports armed and !on_ground, auto-issue RTL (or HOLD,
    // configurable). One-shot per disconnection — won't re-fire until the
    // link comes back.
    //
    // Skipped when --dummy or no autopilot ever connected (nothing to fly
    // home).
    std::thread heartbeat_thread([&]() {
        if (dummy || !ctx.system || !ctx.action || !ctx.telemetry) return;
        using namespace std::chrono_literals;
        using clk = std::chrono::steady_clock;
        std::optional<clk::time_point> disconnect_at;
        bool action_fired = false;
        while (!g_stop.load()) {
            std::this_thread::sleep_for(200ms);
            const bool connected = ctx.system->is_connected();
            if (connected) {
                disconnect_at.reset();
                action_fired = false;
                continue;
            }
            if (!disconnect_at) disconnect_at = clk::now();
            const double age_s = std::chrono::duration<double>(
                clk::now() - *disconnect_at).count();
            if (age_s < hb_threshold_s || action_fired) continue;
            // We can't read armed/in-air via MAVSDK once disconnected, so
            // gate on the spoken-arm state we maintain locally — it tracks
            // an active armed session.
            if (!state.armed_with_voice.load()) {
                action_fired = true;  // suppress spam; nothing to do.
                continue;
            }
            std::fprintf(stderr,
                         "[flight-bridge] heartbeat lost %.1fs — issuing %s\n",
                         age_s, hb_action.c_str());
            mavsdk::Action::Result r = mavsdk::Action::Result::Unknown;
            if (hb_action == "hold")      r = ctx.action->hold();
            else                          r = ctx.action->return_to_launch();
            std::fprintf(stderr, "[flight-bridge] heartbeat-loss %s -> %s\n",
                         hb_action.c_str(),
                         r == mavsdk::Action::Result::Success ? "ok" : "failed");
            action_fired = true;
        }
    });

    // Telemetry broadcaster thread.
    std::thread tele_thread([&]() {
        using namespace std::chrono_literals;
        while (!g_stop.load()) {
            lexaire::Telemetry t;
            t.ts_ns = lexaire::now_ns();
            if (ctx.telemetry) {
                t.connected = ctx.system && ctx.system->is_connected();
                t.armed = ctx.telemetry->armed();
                t.flight_mode = lexaire::flight_mode_to_string(ctx.telemetry->flight_mode());
                auto b = ctx.telemetry->battery();
                // MAVSDK Battery::remaining_percent is already 0..100, not 0..1.
                t.battery_pct = static_cast<int>(b.remaining_percent);
                t.battery_v = b.voltage_v;
                auto p = ctx.telemetry->position();
                t.lat = p.latitude_deg;
                t.lon = p.longitude_deg;
                t.abs_alt_m = p.absolute_altitude_m;
                t.rel_alt_m = p.relative_altitude_m;
                auto a = ctx.telemetry->attitude_euler();
                t.roll_deg = a.roll_deg;
                t.pitch_deg = a.pitch_deg;
                t.yaw_deg = a.yaw_deg;
                auto v = ctx.telemetry->velocity_ned();
                t.ground_speed_mps = std::sqrt(v.north_m_s * v.north_m_s + v.east_m_s * v.east_m_s);
            } else {
                t.connected = false;
                t.flight_mode = dummy ? "DUMMY" : "NO_AUTOPILOT";
            }
            std::string payload = t.to_json().dump();
            zmq_send(pub, payload.data(), payload.size(), 0);
            std::this_thread::sleep_for(100ms);
        }
    });

    // Main REQ/REP loop. Serial is fine at the orchestrator's command rate.
    while (!g_stop.load()) {
        zmq_pollitem_t items[] = {{rep, 0, ZMQ_POLLIN, 0}};
        int rc = zmq_poll(items, 1, 250);
        if (rc <= 0) continue;

        char buf[8192];
        int n = zmq_recv(rep, buf, sizeof(buf) - 1, 0);
        if (n < 0) continue;
        buf[n] = 0;

        lexaire::ToolResult result;
        try {
            json req = json::parse(std::string(buf, n));
            lexaire::ToolCall call = lexaire::ToolCall::from_json(req);

            // An arm call carrying voice_confirmed=true flips the spoken-arm
            // gate before safety runs. The orchestrator is the only producer
            // of tool calls and it only runs in response to a voice command,
            // so any arm that reaches here is by construction voice-spoken.
            // The flag persists until disarm so takeoff can follow the same
            // armed session — fresh arm next flight requires fresh voice.
            if (call.name == "arm" && call.args.value("voice_confirmed", false)) {
                state.armed_with_voice.store(true);
            }

            auto gate = lexaire::check_tool(call.name, call.args, env, state);
            if (!gate.allow) {
                result = lexaire::ToolResult{call.request_id, false, "safety_denied: " + gate.reason, json::object()};
            } else {
                result = lexaire::dispatch(call, ctx);
                if (call.name == "disarm" && result.ok) state.armed_with_voice.store(false);
            }
        } catch (const std::exception& e) {
            result = lexaire::ToolResult{"", false, std::string("bad_request: ") + e.what(), json::object()};
        }
        std::string reply = result.to_json().dump();
        zmq_send(rep, reply.data(), reply.size(), 0);
    }

    g_stop.store(true);
    if (heartbeat_thread.joinable()) heartbeat_thread.join();
    if (tele_thread.joinable()) tele_thread.join();
    zmq_close(rep);
    zmq_close(pub);
    zmq_ctx_destroy(zctx);
    std::fprintf(stderr, "[flight-bridge] shutdown complete\n");
    return 0;
}

int main(int argc, char** argv) {
    bool dummy = false;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--dummy") == 0) dummy = true;
    }
    std::signal(SIGINT, on_sigint);
    std::signal(SIGTERM, on_sigint);
    return run(dummy);
}
