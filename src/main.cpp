#include <iostream>
#include <memory>
#include <future>
#include <functional>
#include <csignal>

#include <mavsdk/mavsdk.h>
#include <mavsdk/system.h>
#include <mavsdk/plugins/telemetry/telemetry.h>
#include <boost/asio.hpp>
#include <boost/asio/posix/stream_descriptor.hpp>
#include <boost/bind/bind.hpp>

#include "monitor.hpp"

using namespace mavsdk;

struct AppContext {
    boost::asio::io_context io_context;
    boost::asio::signal_set signals;
    boost::asio::posix::stream_descriptor input;
    boost::asio::streambuf buffer;

    std::shared_ptr<System> system;
    std::unique_ptr<PluginBase> pointer;
    
    enum State { MainMenu, NotMainMenu } current_state;

    AppContext(std::shared_ptr<System> system) :
        signals(io_context, SIGINT),
        input(io_context, ::dup(STDIN_FILENO)),
        system(system),
        current_state(State::MainMenu) {}
};

void display_main_menu();
void process_command(AppContext &ctx, const std::string &command);
void on_input_received(AppContext &ctx, const boost::system::error_code &error);
void start_reading_input(AppContext &ctx);
void start_monitoring(AppContext &ctx);

int main() {
    Mavsdk sdk{Mavsdk::Configuration{ComponentType::CompanionComputer}};
    const std::string connection_url = "serial:///dev/ttyACM0:57600";
    ConnectionResult connection_result = sdk.add_any_connection(connection_url);
    if (connection_result != ConnectionResult::Success) {
        std::cerr << "Connection failed: " << connection_result << std::endl;
        return -1;
    }

    std::cout << "Waiting for a drone to connect..." << std::endl;
    auto prom{std::make_shared<std::promise<std::shared_ptr<System>>>()};
    auto fut{prom->get_future()};
    sdk.subscribe_on_new_system([&sdk, prom]() {
        std::shared_ptr<System> system = sdk.systems().at(0);
        std::cout << "Drone discovered!" << std::endl;
        prom->set_value(system);
    });
    std::shared_ptr<System> system{fut.get()};

    AppContext ctx(system);

    ctx.signals.async_wait([&ctx](const boost::system::error_code&, int) {
        std::cout << "\nCtrl+C detected. Shutting down." << std::endl;
        ctx.io_context.stop();
    });

    display_main_menu();
    start_reading_input(ctx);
    ctx.io_context.run();
}

void start_reading_input(AppContext &ctx) {
    boost::asio::async_read_until(
        ctx.input,
        ctx.buffer,
        '\n',
        boost::bind(on_input_received, std::ref(ctx), boost::asio::placeholders::error)
    );
}

void on_input_received(AppContext &ctx, const boost::system::error_code &error) {
    if (error) {
        if (error != boost::asio::error::eof) {
            std::cerr << "Input error: " << error.message() << std::endl;
        }
        ctx.io_context.stop();
        return;
    }

    std::istream is(&ctx.buffer);
    std::string command;
    std::getline(is, command);

    process_command(ctx, command);

    if (!ctx.io_context.stopped()) {
        start_reading_input(ctx);
    }
}

void process_command(AppContext &ctx, const std::string &command) {
    if (ctx.current_state == AppContext::MainMenu) {
        short choice = 0;
        try {
            choice = std::stoi(command);
        } catch (...) {
            std::cout << "Invalid input. Please enter a number.\n\n";
            display_main_menu();
            return;
        }

        switch (choice) {
            case 1:
                ctx.current_state = AppContext::NotMainMenu;
                std::cout 
                    << "\n" << std::string(50, '-')
                    << "\nINSTRUCTIONS:"
                    << "\n 1. Configure the drone and its joystick controls via QGroundControl."
                    << "\n 2. Start SLAM pipeline."
                    << "\n\nPress Enter to return to the main menu."
                    << "\n" << std::string(50, '-') << "\n> " << std::flush;
                break;
            case 2:
                start_monitoring(ctx);
                break;
            default:
                std::cout << "Invalid choice.\n";
                display_main_menu();
                break;
        }
    } else if (ctx.current_state == AppContext::NotMainMenu) {
        std::cout << "\nReturning to main menu.\n";
        ctx.pointer = nullptr;
        ctx.current_state = AppContext::MainMenu;
        display_main_menu();
    }
}

void display_main_menu() {
    std::cout
        << "\n" << std::string(60, '_') << "\n"
        << std::string(25, ' ') << "Main Menu\n"
        << std::string(60, '_')
        << "\nWelcome to the main menu. At any time, press Ctrl+C to exit."
        << "\nEnter your selection:\n\n"
        << " 1. Read instructions\n"
        << " 2. Start telemetry monitoring\n"
        << std::string(60, '_') << "\n> " << std::flush;
}

void start_monitoring(AppContext &ctx) {
    ctx.current_state = AppContext::NotMainMenu;
    ctx.pointer = std::make_unique<Telemetry>(ctx.system);
    setup_monitoring(static_cast<mavsdk::Telemetry&>(*ctx.pointer)); 

    std::cout << "\n" << std::string(50, '-')
              << "\nTelemetry monitoring started. MAVSDK is printing updates."
              << "\nPress Enter to return to the main menu."
              << "\n" << std::string(50, '-') << "\n> " << std::flush;
}