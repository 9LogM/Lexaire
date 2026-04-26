#pragma once

// ServicesWatcher: background ZMQ SUB reader for the three status channels
// the Python services expose (perception scene, flight-bridge telemetry,
// orchestrator status). Exposes a thread-safe snapshot for the TUI to draw.
//
// It owns its own thread; the TUI does not pump ZMQ messages on the main
// event loop. Last-seen monotonic timestamps let the UI show staleness
// without any cross-thread notification plumbing.

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "monitor.hpp"

struct ServicesSnapshot {
    // Monotonic ns of last successful recv, or 0 if never.
    std::int64_t perception_last_ns = 0;
    std::int64_t telemetry_last_ns  = 0;
    std::int64_t orch_last_ns       = 0;

    // One-line summaries from the most recent message on each channel.
    std::string perception_summary;
    std::string telemetry_summary;
    std::string orch_state;     // "idle" | "thinking" | "executing" | "aborted"
    std::string orch_thought;

    TelemetrySnapshot telemetry;

    // Last time the watcher itself checked in (helps diagnose a stuck thread).
    std::int64_t watcher_last_ns = 0;
};

class ServicesWatcher {
public:
    ServicesWatcher(std::string scene_ep,
                    std::string telem_ep,
                    std::string orch_ep);
    ~ServicesWatcher();

    ServicesWatcher(const ServicesWatcher&) = delete;
    ServicesWatcher& operator=(const ServicesWatcher&) = delete;

    void start();
    void stop();

    ServicesSnapshot snapshot() const;

private:
    void run();

    std::string scene_ep_;
    std::string telem_ep_;
    std::string orch_ep_;

    std::atomic<bool> stop_{false};
    std::thread thr_;

    mutable std::mutex mu_;
    ServicesSnapshot snap_;
};
