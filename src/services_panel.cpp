#include "services_panel.hpp"

#include <array>
#include <chrono>
#include <sstream>
#include <utility>

#include <nlohmann/json.hpp>
#include <zmq.h>

namespace {

std::int64_t mono_ns() {
    using namespace std::chrono;
    return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
}

void close_if(void*& s) {
    if (s) { zmq_close(s); s = nullptr; }
}

// Read a full multipart message into a single string (header frame only —
// payload frames are drained and discarded). Returns false if nothing's
// available or the recv failed.
bool recv_header_only(void* sock, std::string& out) {
    zmq_msg_t msg;
    if (zmq_msg_init(&msg) != 0) return false;

    int rc = zmq_msg_recv(&msg, sock, ZMQ_DONTWAIT);
    if (rc < 0) {
        zmq_msg_close(&msg);
        return false;
    }

    out.assign(static_cast<char*>(zmq_msg_data(&msg)), zmq_msg_size(&msg));

    while (zmq_msg_more(&msg)) {
        zmq_msg_close(&msg);
        zmq_msg_init(&msg);
        rc = zmq_msg_recv(&msg, sock, 0);
        if (rc < 0) break;
    }
    zmq_msg_close(&msg);
    return true;
}

std::string summarize_scene(const std::string& header) {
    try {
        auto j = nlohmann::json::parse(header);
        auto dets = j.value("detections", nlohmann::json::array());
        if (!dets.is_array() || dets.empty()) return "no detections";

        std::ostringstream ss;
        ss << dets.size() << " detection" << (dets.size() == 1 ? "" : "s") << " (";
        for (std::size_t i = 0; i < dets.size() && i < 3; ++i) {
            if (i) ss << ", ";
            ss << dets[i].value("label", "?");
        }
        if (dets.size() > 3) ss << ", ...";
        ss << ")";
        return ss.str();
    } catch (...) {
        return "parse error";
    }
}

std::string summarize_telemetry(const std::string& header) {
    try {
        auto j = nlohmann::json::parse(header);
        const bool connected = j.value("connected", false);
        const std::string mode = j.value("flight_mode", "");

        // "no autopilot" needs cable/relay; "timeout" needs FC power/RC.
        if (!connected) {
            if (mode == "NO_AUTOPILOT") return "no autopilot at startup";
            return "autopilot heartbeat timeout";
        }

        std::ostringstream ss;
        ss << mode;
        ss << " / " << (j.value("armed", false) ? "armed" : "disarmed");
        if (j.contains("rel_alt_m") && !j["rel_alt_m"].is_null()) {
            ss << " / " << j["rel_alt_m"].get<double>() << " m";
        }
        if (j.contains("battery_pct") && !j["battery_pct"].is_null()) {
            ss << " / " << j["battery_pct"].get<int>() << "%";
        }
        return ss.str();
    } catch (...) {
        return "parse error";
    }
}

// Optional fields gate the has_* booleans so missing data renders as N/A
// rather than zero.
void parse_telemetry(const std::string& header, TelemetrySnapshot& out) {
    out = TelemetrySnapshot{};
    auto j = nlohmann::json::parse(header, nullptr, /*allow_exceptions*/ false);
    if (!j.is_object()) return;

    out.connected     = j.value("connected",     false);
    out.qgc_connected = j.value("qgc_connected", false);
    out.armed         = j.value("armed",         false);
    out.flight_mode   = j.value("flight_mode",   std::string{"N/A"});

    const bool has_pct = j.contains("battery_pct") && !j["battery_pct"].is_null();
    const bool has_v   = j.contains("battery_v")   && !j["battery_v"].is_null();
    out.has_battery = has_pct || has_v;
    if (has_pct) out.battery_pct = j["battery_pct"].get<int>();
    if (has_v)   out.battery_v   = j["battery_v"].get<float>();

    if (j.contains("lat") && !j["lat"].is_null() &&
        j.contains("lon") && !j["lon"].is_null()) {
        out.has_fix   = true;
        out.latitude  = j["lat"].get<double>();
        out.longitude = j["lon"].get<double>();
    }
    if (j.contains("abs_alt_m") && !j["abs_alt_m"].is_null()) {
        out.abs_alt_m = j["abs_alt_m"].get<float>();
    }
    if (j.contains("rel_alt_m") && !j["rel_alt_m"].is_null()) {
        out.rel_alt_m = j["rel_alt_m"].get<float>();
    }

    out.roll_deg   = j.value("roll_deg",         0.0f);
    out.pitch_deg  = j.value("pitch_deg",        0.0f);
    out.yaw_deg    = j.value("yaw_deg",          0.0f);
    out.ground_spd = j.value("ground_speed_mps", 0.0f);
}

std::pair<std::string, std::string> parse_orch(const std::string& header) {
    try {
        auto j = nlohmann::json::parse(header);
        return {j.value("state", "?"), j.value("last_thought", "")};
    } catch (...) {
        return {"parse error", ""};
    }
}

}  // namespace

ServicesWatcher::ServicesWatcher(std::string scene_ep,
                                 std::string telem_ep,
                                 std::string orch_ep)
    : scene_ep_(std::move(scene_ep))
    , telem_ep_(std::move(telem_ep))
    , orch_ep_(std::move(orch_ep)) {}

ServicesWatcher::~ServicesWatcher() {
    stop();
}

void ServicesWatcher::start() {
    stop_.store(false);
    thr_ = std::thread(&ServicesWatcher::run, this);
}

void ServicesWatcher::stop() {
    stop_.store(true);
    if (thr_.joinable()) thr_.join();
}

ServicesSnapshot ServicesWatcher::snapshot() const {
    std::lock_guard<std::mutex> lk(mu_);
    return snap_;
}

void ServicesWatcher::run() {
    void* ctx = zmq_ctx_new();
    if (!ctx) return;

    auto mksub = [&](const std::string& ep) -> void* {
        void* s = zmq_socket(ctx, ZMQ_SUB);
        if (!s) return nullptr;
        int hwm = 8;
        zmq_setsockopt(s, ZMQ_RCVHWM, &hwm, sizeof(hwm));
        int linger = 0;
        zmq_setsockopt(s, ZMQ_LINGER, &linger, sizeof(linger));
        zmq_setsockopt(s, ZMQ_SUBSCRIBE, "", 0);
        if (zmq_connect(s, ep.c_str()) != 0) {
            zmq_close(s);
            return nullptr;
        }
        return s;
    };

    void* scene = mksub(scene_ep_);
    void* telem = mksub(telem_ep_);
    void* orch  = mksub(orch_ep_);

    std::array<zmq_pollitem_t, 3> items{{
        {scene, 0, ZMQ_POLLIN, 0},
        {telem, 0, ZMQ_POLLIN, 0},
        {orch,  0, ZMQ_POLLIN, 0},
    }};

    while (!stop_.load(std::memory_order_relaxed)) {
        int rc = zmq_poll(items.data(), (int)items.size(), 250);
        {
            std::lock_guard<std::mutex> lk(mu_);
            snap_.watcher_last_ns = mono_ns();
        }
        if (rc <= 0) continue;

        std::string hdr;

        if ((items[0].revents & ZMQ_POLLIN) && scene && recv_header_only(scene, hdr)) {
            auto summary = summarize_scene(hdr);
            std::lock_guard<std::mutex> lk(mu_);
            snap_.perception_last_ns = mono_ns();
            snap_.perception_summary = std::move(summary);
        }
        if ((items[1].revents & ZMQ_POLLIN) && telem && recv_header_only(telem, hdr)) {
            auto summary = summarize_telemetry(hdr);
            TelemetrySnapshot parsed;
            parse_telemetry(hdr, parsed);
            std::lock_guard<std::mutex> lk(mu_);
            snap_.telemetry_last_ns = mono_ns();
            snap_.telemetry_summary = std::move(summary);
            snap_.telemetry         = std::move(parsed);
        }
        if ((items[2].revents & ZMQ_POLLIN) && orch && recv_header_only(orch, hdr)) {
            auto [state, thought] = parse_orch(hdr);
            std::lock_guard<std::mutex> lk(mu_);
            snap_.orch_last_ns = mono_ns();
            snap_.orch_state   = std::move(state);
            snap_.orch_thought = std::move(thought);
        }
    }

    close_if(scene);
    close_if(telem);
    close_if(orch);
    zmq_ctx_term(ctx);
}
