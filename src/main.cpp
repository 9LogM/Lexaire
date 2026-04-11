#include <iostream>
#include <fstream>
#include <sstream>
#include <memory>
#include <future>
#include <atomic>
#include <chrono>
#include <map>
#include <string>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/telemetry/telemetry.h>
#include <mavsdk/log_callback.h>
#include <boost/asio.hpp>

#include <ncurses.h>

#include "monitor.hpp"
#include "qgc_bridge.hpp"

using namespace mavsdk;

// ── Config ────────────────────────────────────────────────────────────────────

static std::map<std::string, std::string> load_config(const std::string& path) {
    std::map<std::string, std::string> result;
    std::ifstream file(path);
    if (!file.is_open()) return result;

    auto trim = [](std::string& s) {
        s.erase(0, s.find_first_not_of(" \t\r\n"));
        auto pos = s.find_last_not_of(" \t\r\n");
        if (pos != std::string::npos) s.erase(pos + 1);
        else s.clear();
    };

    std::string line;
    while (std::getline(file, line)) {
        if (line.empty() || line[0] == '#') continue;
        auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string key = line.substr(0, colon);
        std::string value = line.substr(colon + 1);
        trim(key); trim(value);
        if (!key.empty()) result[key] = value;
    }
    return result;
}

static std::map<std::string, std::string> try_load_config() {
    for (const std::string& path : {"common/config.yaml", "../common/config.yaml"}) {
        auto c = load_config(path);
        if (!c.empty()) return c;
    }
    return {};
}

static std::string get_local_ip() {
    struct ifaddrs* ifaddr;
    if (getifaddrs(&ifaddr) == -1) return "unknown";
    std::string result = "unknown";
    for (auto* ifa = ifaddr; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
        if (std::string(ifa->ifa_name) == "lo") continue;
        char buf[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &reinterpret_cast<sockaddr_in*>(ifa->ifa_addr)->sin_addr, buf, sizeof(buf));
        result = buf;
        break;
    }
    freeifaddrs(ifaddr);
    return result;
}

// ── App state ─────────────────────────────────────────────────────────────────

enum class State { MainMenu, SubMenu, Monitoring };

struct AppContext {
    boost::asio::io_context      io;
    boost::asio::signal_set      signals;
    boost::asio::steady_timer    input_timer;
    boost::asio::steady_timer    qgc_watchdog;
    boost::asio::steady_timer    render_timer;
    bool                         render_dirty = false;

    Mavsdk&                      sdk;
    std::shared_ptr<System>      system;
    std::unique_ptr<PluginBase>  telemetry_plugin;

    State        state         = State::MainMenu;
    bool         qgc_connected = false;
    std::string  input_line;
    std::string  sub_content;
    std::shared_ptr<TelemetrySnapshot> telemetry_snap = std::make_shared<TelemetrySnapshot>();

    AppContext(Mavsdk& sdk, std::shared_ptr<System> system)
        : signals(io, SIGINT)
        , input_timer(io)
        , qgc_watchdog(io)
        , render_timer(io)
        , sdk(sdk)
        , system(system) {}
};

// ── Drawing ───────────────────────────────────────────────────────────────────

static void draw_header(const AppContext& ctx) {
    int cols = getmaxx(stdscr);

    attron(A_BOLD);
    mvhline(0, 0, '=', cols);
    attroff(A_BOLD);

    mvprintw(1, 2, "ORBIS");

    std::string label = "QGC: ";
    std::string status = ctx.qgc_connected ? "Connected" : "Waiting...";
    int right_col = cols - (int)(label.size() + status.size()) - 2;
    mvprintw(1, right_col, "%s", label.c_str());
    if (ctx.qgc_connected) attron(COLOR_PAIR(1) | A_BOLD);
    else                   attron(COLOR_PAIR(2));
    printw("%s", status.c_str());
    attroff(COLOR_PAIR(1) | COLOR_PAIR(2) | A_BOLD);

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
        mvprintw(4, 2, "1. QGroundControl");
        mvprintw(5, 2, "2. Telemetry monitor");
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
                std::string ip = get_local_ip();
                ctx.sub_content =
                    "  QGROUNDCONTROL\n\n"
                    "  Connect QGC on your laptop:\n"
                    "    Comm Links -> Add -> UDP\n"
                    "    Server address : " + ip + "\n"
                    "    Port           : 14550\n\n"
                    "  Press Enter to return.";
                ctx.state = State::SubMenu;
                render(ctx);
                break;
            }
            case 2: {
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
                render(ctx);
                break;
            }
            default:
                render(ctx);
        }
    } else {
        ctx.telemetry_plugin = nullptr;
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

// ── QGC watchdog ──────────────────────────────────────────────────────────────

static void start_qgc_watchdog(AppContext& ctx) {
    ctx.qgc_watchdog.expires_after(std::chrono::seconds(1));
    ctx.qgc_watchdog.async_wait([&ctx](const boost::system::error_code& ec) {
        if (ec || ctx.io.stopped()) return;

        bool found = false;
        for (auto& sys : ctx.sdk.systems()) {
            if (!sys->has_autopilot() && sys->is_connected()) { found = true; break; }
        }

        if (found != ctx.qgc_connected) {
            ctx.qgc_connected = found;
            request_render(ctx);
        }

        start_qgc_watchdog(ctx);
    });
}

// ── Main ──────────────────────────────────────────────────────────────────────

int main() {
    mavsdk::log::subscribe([](mavsdk::log::Level, const std::string&, const std::string&, int) {
        return true;
    });

    auto config = try_load_config();
    const std::string serial_device = config.count("serial_device") ? config.at("serial_device") : "/dev/ttyACM0";
    const int serial_baud = config.count("serial_baud") ? std::stoi(config.at("serial_baud")) : 57600;

    if (start_mavlink_router(serial_device, serial_baud) < 0)
        std::cerr << "Warning: MAVLink router failed to start.\n";

    Mavsdk sdk{Mavsdk::Configuration{ComponentType::CompanionComputer}};
    if (sdk.add_any_connection("udpin://0.0.0.0:14551") != ConnectionResult::Success) {
        std::cerr << "Connection failed.\n";
        return -1;
    }

    std::cout << "Waiting for drone..." << std::flush;
    auto prom = std::make_shared<std::promise<std::shared_ptr<System>>>();
    auto fut = prom->get_future();
    auto promise_set = std::make_shared<std::atomic<bool>>(false);
    sdk.subscribe_on_new_system([&sdk, prom, promise_set]() {
        for (auto& sys : sdk.systems())
            if (sys->has_autopilot() && !promise_set->exchange(true))
                prom->set_value(sys);
    });
    std::shared_ptr<System> system{fut.get()};
    std::cout << " connected.\n" << std::flush;

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

    AppContext ctx(sdk, system);

    ctx.signals.async_wait([&ctx](const boost::system::error_code&, int) {
        endwin();
        ctx.io.stop();
    });

    render(ctx);
    start_input_poll(ctx);
    start_qgc_watchdog(ctx);
    ctx.io.run();

    endwin();
}
