#pragma once
#include <string>
#include <sys/types.h>

pid_t start_mavlink_router(const std::string& serial_device, int baud);
