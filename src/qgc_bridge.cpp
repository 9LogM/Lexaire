#include "qgc_bridge.hpp"

#include <unistd.h>
#include <fcntl.h>
#include <fstream>
#include <iostream>

pid_t start_mavlink_router(const std::string& serial_device, int baud) {
    const std::string config_path = "/tmp/orbis-mlr.conf";
    {
        std::ofstream f(config_path);
        if (!f.is_open()) {
            std::cerr << "Failed to write mavlink-router config.\n";
            return -1;
        }
        f << "[General]\nTcpServerPort=0\n\n"
          << "[UartEndpoint Pixhawk]\nDevice=" << serial_device << "\nBaud=" << baud << "\n\n"
          << "[UdpEndpoint QGC]\nMode=Server\nAddress=0.0.0.0\nPort=14550\n\n"
          << "[UdpEndpoint MAVSDK]\nMode=Normal\nAddress=127.0.0.1\nPort=14551\n";
    }

    pid_t pid = fork();
    if (pid < 0) {
        std::cerr << "fork() failed — mavlink-router not started.\n";
        return -1;
    }
    if (pid == 0) {
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        execl("/usr/local/bin/mavlink-routerd", "mavlink-routerd",
              "-c", config_path.c_str(), nullptr);
        _exit(1);
    }

    std::cout << "MAVLink router started.\n";
    return pid;
}
