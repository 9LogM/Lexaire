#include <iostream>
#include <fstream>
#include <sstream>
#include <memory>
#include <atomic>
#include <chrono>
#include <functional>
#include <map>
#include <string>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/telemetry/telemetry.h>
#include <mavsdk/log_callback.h>
#include <boost/asio.hpp>
#include <boost/process.hpp>

#include <ncurses.h>

#include "monitor.hpp"
#include "services_panel.hpp"
#include "lexaire/config.hpp"

using namespace mavsdk;

// ── App state ─────────────────────────────────────────────────────────────────

enum class State { MainMenu, SubMenu, Monitoring, Services };

struct AppContext {
    boost::asio::io_context      io;
    boost::asio::signal_set      signals;
    boost::asio::steady_timer    input_timer;
    boost::asio::steady_timer    render_timer;
    bool                         render_dirty = false;

    Mavsdk&                      sdk;
    std::shared_ptr<System>      system;
    std::unique_ptr<PluginBase>  telemetry_plugin;

    State        state         = State::MainMenu;
    bool         qgc_connected = false;
    bool         relay_active  = false;
    std::string  input_line;
    std::string  sub_content;
    std::string  drone_host;
    std::string  serial_device;
    int          serial_baud   = 0;
    std::string  scene_pub_ep;
    std::string  telem_pub_ep;
    std::string  orch_pub_ep;
    std::shared_ptr<TelemetrySnapshot> telemetry_snap = std::make_shared<TelemetrySnapshot>();

    std::unique_ptr<ServicesWatcher>  services_watcher;
    boost::asio::steady_timer         services_tick;
    std::shared_ptr<std::function<void()>> services_tick_fn;

    AppContext(Mavsdk& sdk, std::shared_ptr<System> system)
        : signals(io, SIGINT)
        , input_timer(io)
        , render_timer(io)
        , sdk(sdk)
        , system(system)
        , services_tick(io) {}
};

// ── Drawing ───────────────────────────────────────────────────────────────────

static void draw_header(const AppContext& ctx) {
    int cols = getmaxx(stdscr);

    attron(A_BOLD);
    mvhline(0, 0, '=', cols);
    attroff(A_BOLD);

    mvprintw(1, 2, "LEXAIRE");

    std::string qgc_label  = "QGC: ";
    std::string qgc_status = ctx.qgc_connected ? "Connected" : "Disconnected";

    int right = cols - (int)(qgc_label.size() + qgc_status.size()) - 2;
    mvprintw(1, right, "%s", qgc_label.c_str());
    if (ctx.qgc_connected) attron(COLOR_PAIR(1) | A_BOLD);
    else                   attron(COLOR_PAIR(2));
    printw("%s", qgc_status.c_str());
    attroff(COLOR_PAIR(1) | COLOR_PAIR(2) | A_BOLD);

    bool heartbeat = ctx.system && ctx.system->is_connected();
    std::string relay_label  = "Relay: ";
    std::string relay_status = !ctx.relay_active    ? "Inactive"
                             : heartbeat            ? "Active"
                                                    : "No Heartbeat";
    int relay_col = right - (int)(relay_label.size() + relay_status.size()) - 3;
    mvprintw(1, relay_col, "%s", relay_label.c_str());
    if (!ctx.relay_active)       attron(COLOR_PAIR(2));
    else if (heartbeat)          attron(COLOR_PAIR(1) | A_BOLD);
    else                         attron(COLOR_PAIR(3) | A_BOLD);
    printw("%s", relay_status.c_str());
    attroff(COLOR_PAIR(1) | COLOR_PAIR(2) | COLOR_PAIR(3) | A_BOLD);

    attron(A_BOLD);
    mvhline(2, 0, '=', cols);
    attroff(A_BOLD);
}

static void start_render_loop(AppContext& ctx);

static void request_render(AppContext& ctx) {
    if (!ctx.render_dirty) {
        ctx.render_dirty = true;
        start_render_loop(ctx);
    }
}

static void render(const AppContext& ctx) {
    int rows = getmaxy(stdscr);
    int cols = getmaxx(stdscr);
    erase();
    draw_header(ctx);

    if (ctx.state == State::MainMenu) {
        mvprintw(4, 2, ctx.relay_active ? "1. Stop relay" : "1. Start relay");
        mvprintw(5, 2, "2. QGroundControl setup");
        mvprintw(6, 2, "3. Live telemetry monitor");
        mvprintw(7, 2, "4. Service status monitor");
        attron(A_DIM);
        mvhline(rows - 3, 0, '-', cols);
        mvprintw(rows - 2, 2, "Ctrl+C to exit");
        attroff(A_DIM);

    } else if (ctx.state == State::SubMenu) {
        int row = 4;
        std::istringstream ss(ctx.sub_content);
        std::string line;
        while (std::getline(ss, line))
            mvprintw(row++, 0, "%s", line.c_str());

    } else if (ctx.state == State::Monitoring) {
        const auto& s = *ctx.telemetry_snap;
        int r = 4;

        // Status
        attron(A_BOLD); mvprintw(r++, 2, "Status"); attroff(A_BOLD);
        mvprintw(r++, 4, "Armed       : %s", s.armed ? "Yes" : "No");
        mvprintw(r++, 4, "Flight mode : %s", s.flight_mode.c_str());
        r++;

        // Power
        attron(A_BOLD); mvprintw(r++, 2, "Power"); attroff(A_BOLD);
        if (s.has_battery)
            mvprintw(r++, 4, "Battery     : %d%%  (%.1f V)", s.battery_pct, s.battery_v);
        else
            mvprintw(r++, 4, "Battery     : N/A");
        r++;

        // Position
        attron(A_BOLD); mvprintw(r++, 2, "Position"); attroff(A_BOLD);
        if (s.has_fix) {
            mvprintw(r++, 4, "Latitude    : %.6f", s.latitude);
            mvprintw(r++, 4, "Longitude   : %.6f", s.longitude);
            mvprintw(r++, 4, "Altitude    : %.1f m (rel: %.1f m)", s.abs_alt_m, s.rel_alt_m);
        } else {
            mvprintw(r++, 4, "No GPS fix");
        }
        r++;

        // Attitude
        attron(A_BOLD); mvprintw(r++, 2, "Attitude"); attroff(A_BOLD);
        mvprintw(r++, 4, "Roll        : %.1f°", s.roll_deg);
        mvprintw(r++, 4, "Pitch       : %.1f°", s.pitch_deg);
        mvprintw(r++, 4, "Yaw         : %.1f°", s.yaw_deg);
        r++;

        // Speed
        attron(A_BOLD); mvprintw(r++, 2, "Speed"); attroff(A_BOLD);
        mvprintw(r++, 4, "Ground      : %.1f m/s", s.ground_spd);

        attron(A_DIM);
        mvprintw(rows - 2, 2, "Press Enter to return.");
        attroff(A_DIM);

    } else if (ctx.state == State::Services) {
        const ServicesSnapshot snap = ctx.services_watcher
            ? ctx.services_watcher->snapshot()
            : ServicesSnapshot{};

        auto now_ns_mono = []() {
            using namespace std::chrono;
            return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
        };
        auto age_ms = [&](std::int64_t ts_ns) -> long long {
            if (ts_ns == 0) return -1;
            return (now_ns_mono() - ts_ns) / 1'000'000;
        };
        auto fmt_age = [](long long ms) -> std::string {
            if (ms < 0)       return "never";
            if (ms > 10'000)  return "stale (>10s)";
            return std::to_string(ms) + " ms ago";
        };
        auto status_pair = [](long long ms) -> int {
            // 1 = green, 2 = yellow, 3 = red — matches init_pair below.
            if (ms < 0)      return 3;
            if (ms > 2'000)  return 3;
            if (ms > 500)    return 2;
            return 1;
        };

        int r = 4;
        attron(A_BOLD); mvprintw(r++, 2, "Service status"); attroff(A_BOLD);
        r++;

        auto row = [&](const char* name, long long ms, const std::string& detail) {
            mvprintw(r, 4, "%-14s", name);
            attron(COLOR_PAIR(status_pair(ms)) | A_BOLD);
            mvprintw(r, 20, "%-20s", fmt_age(ms).c_str());
            attroff(COLOR_PAIR(status_pair(ms)) | A_BOLD);
            mvprintw(r, 42, "%s", detail.c_str());
            r++;
        };

        row("perception",    age_ms(snap.perception_last_ns), snap.perception_summary);
        row("telemetry",     age_ms(snap.telemetry_last_ns),  snap.telemetry_summary);
        std::string orch_line = snap.orch_state;
        if (!snap.orch_thought.empty()) {
            orch_line += " — ";
            orch_line += snap.orch_thought;
        }
        row("orchestrator",  age_ms(snap.orch_last_ns),       orch_line);

        r++;
        attron(A_DIM);
        mvprintw(r++, 4, "Watcher last tick: %s", fmt_age(age_ms(snap.watcher_last_ns)).c_str());
        attroff(A_DIM);

        attron(A_DIM);
        mvprintw(rows - 2, 2, "Press Enter to return.");
        attroff(A_DIM);
    }

    // Input prompt
    mvprintw(rows - 1, 0, "> %s", ctx.input_line.c_str());
    move(rows - 1, 2 + (int)ctx.input_line.size());
    refresh();
}

// ── Render loop (rate-limited to ~30fps) ──────────────────────────────────────

static void start_render_loop(AppContext& ctx) {
    ctx.render_timer.expires_after(std::chrono::milliseconds(33));
    ctx.render_timer.async_wait([&ctx](const boost::system::error_code& ec) {
        if (ec || ctx.io.stopped()) return;
        if (ctx.render_dirty) {
            ctx.render_dirty = false;
            render(ctx);
        }
    });
}

// ── Commands ──────────────────────────────────────────────────────────────────

static void process_command(AppContext& ctx, const std::string& cmd) {
    if (ctx.state == State::MainMenu) {
        if (cmd.empty()) return;

        int choice = 0;
        try { choice = std::stoi(cmd); } catch (...) {}

        switch (choice) {
            case 1: {
                bool stopping = ctx.relay_active;
                std::string shell_cmd = "DOCKER_HOST=ssh://" + ctx.drone_host +
                    " SERIAL_DEVICE=" + ctx.serial_device +
                    " SERIAL_BAUD=" + std::to_string(ctx.serial_baud) +
                    (stopping
                        ? " docker compose -f relay/docker-compose.yaml down"
                        : " docker compose -f relay/docker-compose.yaml up -d --build") +
                    " >/dev/null 2>&1";
                ctx.sub_content = stopping
                    ? "  STOP RELAY\n\n  Stopping...\n\n"
                    : "  START RELAY\n\n  Deploying - this may take a few minutes on first run...\n\n";
                ctx.state = State::SubMenu;
                render(ctx);
                boost::process::async_system(
                    ctx.io,
                    [&ctx, stopping](boost::system::error_code, int rc) {
                        if (rc == 0) ctx.relay_active = !stopping;
                        ctx.sub_content += rc == 0
                            ? (stopping ? "  Relay stopped." : "  Relay running.")
                            : "  Failed (exit " + std::to_string(rc) + ").\n"
                              "  Ensure companion computer has Docker running and SSH key is configured.";
                        ctx.sub_content += "\n\n  Press Enter to return.";
                        render(ctx);
                    },
                    boost::process::shell, shell_cmd
                );
                break;
            }
            case 2: {
                std::string host = ctx.drone_host.substr(ctx.drone_host.find('@') + 1);
                ctx.sub_content =
                    "  QGROUNDCONTROL SETUP\n\n"
                    "  Connect QGC:\n"
                    "    Comm Links -> Add -> UDP\n"
                    "    Server address : " + host + "\n"
                    "    Port           : 14550\n\n"
                    "  Press Enter to return.";
                ctx.state = State::SubMenu;
                render(ctx);
                break;
            }
            case 4: {
                ctx.state = State::Services;
                if (!ctx.services_watcher) {
                    ctx.services_watcher = std::make_unique<ServicesWatcher>(
                        ctx.scene_pub_ep, ctx.telem_pub_ep, ctx.orch_pub_ep);
                    ctx.services_watcher->start();
                }
                // Periodic re-render so "N ms ago" ages update while the user
                // watches. Store the recursive tick lambda on the context so
                // it outlives this scope; capture weak to avoid a cycle.
                ctx.services_tick_fn = std::make_shared<std::function<void()>>();
                std::weak_ptr<std::function<void()>> weak = ctx.services_tick_fn;
                *ctx.services_tick_fn = [&ctx, weak]() {
                    if (ctx.state != State::Services) return;
                    request_render(ctx);
                    ctx.services_tick.expires_after(std::chrono::milliseconds(500));
                    ctx.services_tick.async_wait(
                        [weak](const boost::system::error_code& ec) {
                            if (ec) return;
                            auto self = weak.lock();
                            if (self) (*self)();
                        });
                };
                (*ctx.services_tick_fn)();
                render(ctx);
                break;
            }
            case 3: {
                if (!ctx.system || !ctx.system->is_connected()) {
                    ctx.sub_content =
                        "  LIVE TELEMETRY\n\n"
                        "  No drone connected.\n\n"
                        "  Press Enter to return.";
                    ctx.state = State::SubMenu;
                    render(ctx);
                    break;
                }
                ctx.state = State::Monitoring;
                ctx.telemetry_snap = std::make_shared<TelemetrySnapshot>();
                ctx.telemetry_plugin = std::make_unique<Telemetry>(ctx.system);
                setup_monitoring(
                    static_cast<Telemetry&>(*ctx.telemetry_plugin),
                    ctx.telemetry_snap,
                    [&ctx]() {
                        boost::asio::post(ctx.io, [&ctx]() { request_render(ctx); });
                    }
                );
                ctx.system->subscribe_is_connected([&ctx](bool connected) {
                    boost::asio::post(ctx.io, [&ctx, connected]() {
                        if (!connected && ctx.state == State::Monitoring) {
                            ctx.telemetry_plugin = nullptr;
                            ctx.sub_content =
                                "  LIVE TELEMETRY\n\n"
                                "  Connection lost.\n\n"
                                "  Press Enter to return.";
                            ctx.state = State::SubMenu;
                            request_render(ctx);
                        }
                    });
                });
                render(ctx);
                break;
            }
            default:
                render(ctx);
        }
    } else {
        ctx.telemetry_plugin = nullptr;
        ctx.services_tick.cancel();
        ctx.state = State::MainMenu;
        render(ctx);
    }
}

// ── Input polling ─────────────────────────────────────────────────────────────

static void start_input_poll(AppContext& ctx) {
    ctx.input_timer.expires_after(std::chrono::milliseconds(33));
    ctx.input_timer.async_wait([&ctx](const boost::system::error_code& ec) {
        if (ec || ctx.io.stopped()) return;

        int ch;
        bool dirty = false;
        while ((ch = getch()) != ERR) {
            if (ch == KEY_RESIZE) {
                render(ctx);
                dirty = false;
            } else if (ch == '\n' || ch == KEY_ENTER) {
                std::string cmd = ctx.input_line;
                ctx.input_line.clear();
                process_command(ctx, cmd);
                dirty = false;
            } else if (ch == KEY_BACKSPACE || ch == 127 || ch == 8) {
                if (!ctx.input_line.empty()) {
                    ctx.input_line.pop_back();
                    dirty = true;
                }
            } else if (ch >= 32 && ch < 127) {
                ctx.input_line += (char)ch;
                dirty = true;
            }
        }

        if (dirty) {
            int rows = getmaxy(stdscr);
            mvprintw(rows - 1, 0, "> %-*s", getmaxx(stdscr) - 2, ctx.input_line.c_str());
            move(rows - 1, 2 + (int)ctx.input_line.size());
            refresh();
        }

        start_input_poll(ctx);
    });
}

// ── Relay status (one-shot on startup) ───────────────────────────────────────

static void check_relay_once(AppContext& ctx) {
    std::string cmd = "DOCKER_HOST=ssh://" + ctx.drone_host +
        " docker ps -q --filter name=lexaire-relay | grep -q .";
    boost::process::async_system(
        ctx.io,
        [&ctx](boost::system::error_code, int exit_code) {
            ctx.relay_active = (exit_code == 0);
            request_render(ctx);
        },
        boost::process::shell, cmd
    );
}

// ── Main ──────────────────────────────────────────────────────────────────────

int main() {
    mavsdk::log::subscribe([](mavsdk::log::Level, const std::string&, const std::string&, int) {
        return true;
    });

    auto config = lexaire::Config::load();
    const std::string serial_device = config.require<std::string>("drone.serial_device");
    const int         serial_baud   = config.require<int>("drone.serial_baud");
    const std::string drone_host    = config.require<std::string>("drone.host");
    const std::string drone_hostname = drone_host.substr(drone_host.find('@') + 1);

    const std::string connection = config.get_or<std::string>(
        "drone.mavsdk_udp", "udpout://" + drone_hostname + ":14551");

    const std::string scene_pub_ep = config.get_or<std::string>(
        "services.perception_scene_pub", "tcp://127.0.0.1:6100");
    const std::string telem_pub_ep = config.get_or<std::string>(
        "services.telemetry_pub", "tcp://127.0.0.1:6101");
    const std::string orch_pub_ep = config.get_or<std::string>(
        "services.orchestrator_status_pub", "tcp://127.0.0.1:6102");

    Mavsdk sdk{Mavsdk::Configuration{ComponentType::CompanionComputer}};
    if (sdk.add_any_connection(connection) != ConnectionResult::Success) {
        std::cerr << "Connection failed.\n";
        return -1;
    }

    std::shared_ptr<System> system = nullptr;

    initscr();
    cbreak();
    noecho();
    keypad(stdscr, TRUE);
    nodelay(stdscr, TRUE);
    curs_set(1);
    start_color();
    use_default_colors();
    init_pair(1, COLOR_GREEN,  -1);
    init_pair(2, COLOR_YELLOW, -1);
    init_pair(3, COLOR_RED,    -1);

    AppContext ctx(sdk, system);
    ctx.drone_host    = drone_host;
    ctx.serial_device = serial_device;
    ctx.serial_baud   = serial_baud;
    ctx.scene_pub_ep  = scene_pub_ep;
    ctx.telem_pub_ep  = telem_pub_ep;
    ctx.orch_pub_ep   = orch_pub_ep;

    ctx.signals.async_wait([&ctx](const boost::system::error_code&, int) {
        endwin();
        ctx.io.stop();
    });

    auto autopilot_found = std::make_shared<std::atomic<bool>>(false);
    auto qgc_found = std::make_shared<std::atomic<bool>>(false);
    sdk.subscribe_on_new_system([&sdk, &ctx, autopilot_found, qgc_found]() {
        for (auto& sys : sdk.systems()) {
            if (sys->has_autopilot() && !autopilot_found->exchange(true)) {
                boost::asio::post(ctx.io, [&ctx, sys]() {
                    ctx.system = sys;
                    request_render(ctx);
                });
            }
            if (!sys->has_autopilot() && !qgc_found->exchange(true)) {
                boost::asio::post(ctx.io, [&ctx]() {
                    ctx.qgc_connected = true;
                    request_render(ctx);
                });
                sys->subscribe_is_connected([&ctx, qgc_found](bool connected) {
                    boost::asio::post(ctx.io, [&ctx, connected, qgc_found]() {
                        ctx.qgc_connected = connected;
                        if (!connected) qgc_found->store(false);
                        request_render(ctx);
                    });
                });
            }
        }
    });

    check_relay_once(ctx);
    render(ctx);
    start_input_poll(ctx);
    ctx.io.run();

    endwin();
}
