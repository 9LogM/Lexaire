#include <clocale>
#include <fstream>
#include <sstream>
#include <memory>
#include <chrono>
#include <functional>
#include <string>

#include <boost/asio.hpp>
#include <boost/process.hpp>

#include <ncurses.h>

#include "monitor.hpp"
#include "services_panel.hpp"
#include "lexaire/config.hpp"

// ── App state ─────────────────────────────────────────────────────────────────

enum class State { MainMenu, SubMenu, Monitoring, Services };

// Unknown = docker daemon unreachable (distinct from Down).
// Deploying is set during in-flight startup commands.
enum class ServiceState { Unknown, Down, Deploying, Up };

// Spawned-process stderr goes here; inheriting it would paint over ncurses,
// /dev/null would hide failures.
constexpr const char* TUI_LOG_PATH = "/tmp/lexaire-tui.log";

struct AppContext {
    boost::asio::io_context      io;
    boost::asio::signal_set      signals;
    boost::asio::steady_timer    input_timer;
    boost::asio::steady_timer    render_timer;
    boost::asio::steady_timer    refresh_timer;  // periodic re-render off the snapshot
    bool                         render_dirty = false;

    State        state         = State::MainMenu;
    ServiceState relay_state   = ServiceState::Unknown;
    ServiceState stack_state   = ServiceState::Unknown;
    std::string  input_line;
    std::string  sub_content;
    std::string  drone_host;
    std::string  serial_device;
    int          serial_baud   = 0;

    std::unique_ptr<ServicesWatcher>  services_watcher;

    boost::asio::steady_timer    stack_timer;    // periodic re-check of GCS stack state

    AppContext()
        : signals(io, SIGINT, SIGTERM)
        , input_timer(io)
        , render_timer(io)
        , refresh_timer(io)
        , stack_timer(io) {}
};

static ServicesSnapshot current_snapshot(const AppContext& ctx) {
    return ctx.services_watcher ? ctx.services_watcher->snapshot() : ServicesSnapshot{};
}

// ── Drawing ───────────────────────────────────────────────────────────────────

static void draw_header(const AppContext& ctx) {
    int cols = getmaxx(stdscr);

    attron(A_BOLD);
    mvhline(0, 0, '=', cols);
    attroff(A_BOLD);

    mvprintw(1, 2, "LEXAIRE");

    const ServicesSnapshot snap = current_snapshot(ctx);
    const bool qgc_connected = snap.telemetry.qgc_connected;
    const bool autopilot_link = snap.telemetry.connected;

    std::string qgc_label  = "QGC: ";
    std::string qgc_status = qgc_connected ? "Connected" : "Disconnected";

    int right = cols - (int)(qgc_label.size() + qgc_status.size()) - 2;
    mvprintw(1, right, "%s", qgc_label.c_str());
    if (qgc_connected) attron(COLOR_PAIR(1) | A_BOLD);
    else               attron(COLOR_PAIR(2));
    printw("%s", qgc_status.c_str());
    attroff(COLOR_PAIR(1) | COLOR_PAIR(2) | A_BOLD);

    const bool heartbeat = autopilot_link;
    std::string relay_label = "Relay: ";
    std::string relay_status;
    int relay_color = 3;
    switch (ctx.relay_state) {
        case ServiceState::Up:
            relay_status = heartbeat ? "Active" : "No Heartbeat";
            relay_color = heartbeat ? 1 : 3;
            break;
        case ServiceState::Deploying:
            relay_status = "Deploying...";
            relay_color = 2;
            break;
        case ServiceState::Down:
            relay_status = "Inactive";
            relay_color = 2;
            break;
        case ServiceState::Unknown:
            // Daemon unreachable — diagnostics are in TUI_LOG_PATH.
            relay_status = "Unknown";
            relay_color = 3;
            break;
    }
    int relay_col = right - (int)(relay_label.size() + relay_status.size()) - 3;
    mvprintw(1, relay_col, "%s", relay_label.c_str());
    attron(COLOR_PAIR(relay_color) | A_BOLD);
    printw("%s", relay_status.c_str());
    attroff(COLOR_PAIR(relay_color) | A_BOLD);

    std::string stack_label = "Stack: ";
    std::string stack_status;
    int stack_color = 3;
    switch (ctx.stack_state) {
        case ServiceState::Up:        stack_status = "Up";        stack_color = 1; break;
        case ServiceState::Deploying: stack_status = "Starting..."; stack_color = 2; break;
        case ServiceState::Down:      stack_status = "Down";      stack_color = 2; break;
        case ServiceState::Unknown:   stack_status = "Unknown";   stack_color = 3; break;
    }
    int stack_col = relay_col - (int)(stack_label.size() + stack_status.size()) - 3;
    mvprintw(1, stack_col, "%s", stack_label.c_str());
    attron(COLOR_PAIR(stack_color) | A_BOLD);
    printw("%s", stack_status.c_str());
    attroff(COLOR_PAIR(stack_color) | A_BOLD);

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
        mvprintw(4, 2, "1. Pre-flight check");
        mvprintw(5, 2, "2. QGroundControl setup");
        mvprintw(6, 2, "3. Live telemetry monitor");
        mvprintw(7, 2, "4. Service status monitor");
        mvprintw(8, 2, "5. Restart relay");
        mvprintw(9, 2, "6. Restart GCS stack");
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
        const TelemetrySnapshot s = current_snapshot(ctx).telemetry;
        int r = 4;
        if (!s.connected) {
            attron(COLOR_PAIR(3) | A_BOLD);
            mvprintw(r++, 4, "Autopilot link not present.");
            attroff(COLOR_PAIR(3) | A_BOLD);
            attron(A_DIM);
            mvprintw(r++, 4, "Live telemetry will populate when the bridge connects to PX4.");
            attroff(A_DIM);
            r++;
        }

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
        mvprintw(r++, 4, "Roll        : %.1f deg", s.roll_deg);
        mvprintw(r++, 4, "Pitch       : %.1f deg", s.pitch_deg);
        mvprintw(r++, 4, "Yaw         : %.1f deg", s.yaw_deg);
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
            if (ms < 0)       return "no data yet";
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

        // Column header — same column positions used by the row lambda below.
        attron(A_BOLD | A_DIM);
        mvprintw(r, 4,  "%-14s", "Service");
        mvprintw(r, 20, "%-20s", "Last update");
        mvprintw(r, 42, "%s",    "Current state");
        attroff(A_BOLD | A_DIM);
        r++;
        attron(A_DIM);
        mvprintw(r++, 4, "%s", std::string(70, '-').c_str());
        attroff(A_DIM);

        // Force the orchestrator row red on alarm states even if the
        // orchestrator itself is publishing healthily.
        const bool bridge_offline = (snap.orch_state == "bridge_offline");
        const bool vlm_error     = (snap.orch_state == "vlm_error");
        const bool orch_alarm    = bridge_offline || vlm_error;

        auto row = [&](const char* name, long long ms, const std::string& detail,
                        int forced_pair = 0) {
            const int pair = forced_pair ? forced_pair : status_pair(ms);
            mvprintw(r, 4, "%-14s", name);
            attron(COLOR_PAIR(pair) | A_BOLD);
            mvprintw(r, 20, "%-20s", fmt_age(ms).c_str());
            attroff(COLOR_PAIR(pair) | A_BOLD);
            // An empty detail looks like a missing column; print "-" so the
            // row reads cleanly when the service hasn't reported anything.
            mvprintw(r, 42, "%s", detail.empty() ? "-" : detail.c_str());
            r++;
        };

        row("perception",    age_ms(snap.perception_last_ns), snap.perception_summary);
        row("telemetry",     age_ms(snap.telemetry_last_ns),  snap.telemetry_summary);
        std::string orch_line = snap.orch_state;
        if (!snap.orch_thought.empty()) {
            orch_line += " : ";
            orch_line += snap.orch_thought;
        }
        row("orchestrator",  age_ms(snap.orch_last_ns),       orch_line,
            orch_alarm ? 3 : 0);

        if (bridge_offline) {
            attron(COLOR_PAIR(3) | A_BOLD);
            mvprintw(r++, 4, ">> FLIGHT BRIDGE OFFLINE - tool calls suspended");
            attroff(COLOR_PAIR(3) | A_BOLD);
        } else if (vlm_error) {
            attron(COLOR_PAIR(3) | A_BOLD);
            mvprintw(r++, 4, ">> VLM ERROR - check API key, quota, network");
            attroff(COLOR_PAIR(3) | A_BOLD);
        }

        r++;
        attron(A_DIM);
        mvprintw(r++, 4, "Subscriber heartbeat: %s",
                 fmt_age(age_ms(snap.watcher_last_ns)).c_str());
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
                const std::string out_file = "/tmp/lexaire-preflight.out";
                std::string shell_cmd =
                    "bash /workspace/scripts/preflight.sh > " + out_file +
                    " 2>&1";
                ctx.sub_content =
                    "  PRE-FLIGHT CHECK\n\n"
                    "  Running checks...\n\n";
                ctx.state = State::SubMenu;
                render(ctx);
                boost::process::async_system(
                    ctx.io,
                    [&ctx, out_file](boost::system::error_code, int rc) {
                        std::ifstream f(out_file);
                        std::stringstream ss;
                        ss << f.rdbuf();
                        std::string body = ss.str();
                        if (body.empty()) {
                            body =
                                "  PRE-FLIGHT CHECK\n\n"
                                "  Script produced no output. "
                                "Verify scripts/preflight.sh exists and is executable.";
                        }
                        ctx.sub_content = body;
                        ctx.sub_content += "\n\n  Exit code ";
                        ctx.sub_content += std::to_string(rc);
                        ctx.sub_content += rc == 0
                            ? "  (passed)\n"
                            : "  (blocked - fix [FAIL] entries above)\n";
                        ctx.sub_content += "\n  Press Enter to return.";
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
            case 3: {
                ctx.state = State::Monitoring;
                render(ctx);
                break;
            }
            case 4: {
                ctx.state = State::Services;
                render(ctx);
                break;
            }
            case 5: {
                // Force-redeploy of the relay on the Pi. Useful when relay/
                // scripts changed locally and you want them picked up, or
                // when the running relay container has gone wedged. The
                // up-to-date check inside ensure_relay_running treats a
                // running relay as final, so this path bypasses it.
                ctx.relay_state = ServiceState::Deploying;
                ctx.sub_content =
                    "  RESTART RELAY\n\n"
                    "  Redeploying - may take a few minutes if the image rebuilds...\n\n";
                ctx.state = State::SubMenu;
                render(ctx);
                std::string shell_cmd = "DOCKER_HOST=ssh://" + ctx.drone_host +
                    " SERIAL_DEVICE=" + ctx.serial_device +
                    " SERIAL_BAUD=" + std::to_string(ctx.serial_baud) +
                    " docker compose -f relay/docker-compose.yaml up -d --build" +
                    " >>" + TUI_LOG_PATH + " 2>&1";
                boost::process::async_system(
                    ctx.io,
                    [&ctx](boost::system::error_code, int rc) {
                        ctx.relay_state = (rc == 0) ? ServiceState::Up : ServiceState::Unknown;
                        ctx.sub_content += rc == 0
                            ? "  Relay redeployed."
                            : "  Failed (exit " + std::to_string(rc) + ").\n"
                              "  See " + TUI_LOG_PATH + " for stderr.";
                        ctx.sub_content += "\n\n  Press Enter to return.";
                        render(ctx);
                    },
                    boost::process::shell, shell_cmd
                );
                break;
            }
            case 6: {
                // Rebuild and restart the local GCS stack containers
                // (perception, orchestrator, flight-bridge). Picks up code
                // changes without dropping out of the TUI to the host shell.
                ctx.stack_state = ServiceState::Deploying;
                ctx.sub_content =
                    "  RESTART GCS STACK\n\n"
                    "  Rebuilding and restarting local services...\n\n";
                ctx.state = State::SubMenu;
                render(ctx);
                std::string shell_cmd = std::string(
                    "docker compose up -d --build >>") + TUI_LOG_PATH + " 2>&1";
                boost::process::async_system(
                    ctx.io,
                    [&ctx](boost::system::error_code, int rc) {
                        ctx.stack_state = (rc == 0) ? ServiceState::Up : ServiceState::Unknown;
                        ctx.sub_content += rc == 0
                            ? "  GCS stack restarted."
                            : "  Failed (exit " + std::to_string(rc) + ").\n"
                              "  See " + TUI_LOG_PATH + " for stderr.";
                        ctx.sub_content += "\n\n  Press Enter to return.";
                        render(ctx);
                    },
                    boost::process::shell, shell_cmd
                );
                break;
            }
            default:
                render(ctx);
        }
    } else {
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

// `docker ps -q` exits 0 with empty stdout when no match, so we have to
// inspect output. Maps to: 0 = Up, 1 = Down, 2 = daemon unreachable.
static std::string ps_query_shell(const std::string& env_prefix,
                                   const std::string& filter_args) {
    return env_prefix +
        " out=$(docker ps -q " + filter_args + " 2>>" + TUI_LOG_PATH + ")"
        " ; rc=$?"
        " ; if [ \"$rc\" -ne 0 ]; then exit 2"
        " ; elif [ -z \"$out\" ]; then exit 1"
        " ; else exit 0"
        " ; fi";
}

static ServiceState exit_to_state(int exit_code) {
    if (exit_code == 0) return ServiceState::Up;
    if (exit_code == 1) return ServiceState::Down;
    return ServiceState::Unknown;
}

static void ensure_relay_running(AppContext& ctx) {
    std::string check_cmd = ps_query_shell(
        "DOCKER_HOST=ssh://" + ctx.drone_host,
        "--filter name=lexaire-relay");
    boost::process::async_system(
        ctx.io,
        [&ctx](boost::system::error_code, int check_rc) {
            ctx.relay_state = exit_to_state(check_rc);
            if (ctx.relay_state == ServiceState::Up) {
                request_render(ctx);
                return;
            }
            ctx.relay_state = ServiceState::Deploying;
            request_render(ctx);

            std::string deploy_cmd = "DOCKER_HOST=ssh://" + ctx.drone_host +
                " SERIAL_DEVICE=" + ctx.serial_device +
                " SERIAL_BAUD=" + std::to_string(ctx.serial_baud) +
                " docker compose -f relay/docker-compose.yaml up -d --build" +
                " >>" + TUI_LOG_PATH + " 2>&1";
            boost::process::async_system(
                ctx.io,
                [&ctx](boost::system::error_code, int deploy_rc) {
                    ctx.relay_state = (deploy_rc == 0)
                        ? ServiceState::Up
                        : ServiceState::Unknown;
                    request_render(ctx);
                },
                boost::process::shell, deploy_cmd
            );
        },
        boost::process::shell, check_cmd
    );
}

static void refresh_stack_state(AppContext& ctx) {
    // `lexaire` is build-only and `tools` profile services aren't part of the
    // running stack — match only the long-running core trio.
    std::string cmd = ps_query_shell(
        "",
        "--filter name=lexaire-perception "
        "--filter name=lexaire-orchestrator "
        "--filter name=lexaire-flight-bridge");
    boost::process::async_system(
        ctx.io,
        [&ctx](boost::system::error_code, int exit_code) {
            const ServiceState next = exit_to_state(exit_code);
            // Don't overwrite a Deploying state mid-restart.
            if (ctx.stack_state != ServiceState::Deploying) {
                ctx.stack_state = next;
                request_render(ctx);
            }
        },
        boost::process::shell, cmd
    );
}

// ── Main ──────────────────────────────────────────────────────────────────────

int main() {
    // Activate the wide-char pipeline before initscr() so ncurses (linked
    // against libncursesw) decodes UTF-8 byte sequences into wide chars
    // instead of stamping each byte as its own cell. C.UTF-8 is bundled
    // with glibc and present in our Debian-slim base image, so no locale
    // pre-generation is required at build or run time.
    std::setlocale(LC_ALL, "C.UTF-8");

    auto config = lexaire::Config::load();
    const std::string serial_device = config.require<std::string>("drone.serial_device");
    const int         serial_baud   = config.require<int>("drone.serial_baud");
    const std::string drone_host    = config.require<std::string>("drone.host");

    const std::string scene_pub_ep = config.require<std::string>("services.perception_scene_pub");
    const std::string telem_pub_ep = config.require<std::string>("services.telemetry_pub");
    const std::string orch_pub_ep  = config.require<std::string>("services.orchestrator_status_pub");

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

    AppContext ctx;
    ctx.drone_host    = drone_host;
    ctx.serial_device = serial_device;
    ctx.serial_baud   = serial_baud;

    ctx.signals.async_wait([&ctx](const boost::system::error_code&, int) {
        endwin();
        ctx.io.stop();
    });

    ctx.services_watcher = std::make_unique<ServicesWatcher>(
        scene_pub_ep, telem_pub_ep, orch_pub_ep);
    ctx.services_watcher->start();

    std::function<void()> refresh_tick;
    refresh_tick = [&ctx, &refresh_tick]() {
        request_render(ctx);
        ctx.refresh_timer.expires_after(std::chrono::milliseconds(500));
        ctx.refresh_timer.async_wait(
            [&refresh_tick](const boost::system::error_code& ec) {
                if (!ec) refresh_tick();
            });
    };
    refresh_tick();

    // docker ps every 500ms would be wasteful; 3s is fast enough that the
    // header tracks stack state when it's started/stopped outside the TUI.
    std::function<void()> stack_tick;
    stack_tick = [&ctx, &stack_tick]() {
        refresh_stack_state(ctx);
        ctx.stack_timer.expires_after(std::chrono::seconds(3));
        ctx.stack_timer.async_wait(
            [&stack_tick](const boost::system::error_code& ec) {
                if (!ec) stack_tick();
            });
    };
    stack_tick();

    ensure_relay_running(ctx);
    render(ctx);
    start_input_poll(ctx);
    ctx.io.run();

    endwin();
}
