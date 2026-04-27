cmake_minimum_required(VERSION 3.16)
project(lexaire CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MAVSDK REQUIRED)
find_package(Boost REQUIRED COMPONENTS system)
find_package(Threads REQUIRED)
# Wide-char ncurses (libncursesw) is required so the TUI renders any UTF-8
# byte sequence — including Gemini-produced text in the orchestrator status
# row — as proper glyphs instead of raw multi-byte garbage. setlocale() in
# main() activates the byte-to-wchar pipeline.
set(CURSES_NEED_WIDE TRUE)
find_package(Curses REQUIRED)
find_package(yaml-cpp REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED libzmq)

# TUI

add_executable(lexaire
    src/main.cpp
    src/services_panel.cpp
)

target_include_directories(lexaire PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CURSES_INCLUDE_DIR}
    ${ZMQ_INCLUDE_DIRS}
)

target_link_libraries(lexaire PRIVATE
    Boost::system
    Threads::Threads
    ${CURSES_LIBRARIES}
    ${ZMQ_LIBRARIES}
    yaml-cpp
)

# Flight bridge

add_executable(lexaire-flight-bridge
    src/flight_bridge/main.cpp
    src/flight_bridge/handlers.cpp
)

target_include_directories(lexaire-flight-bridge PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${ZMQ_INCLUDE_DIRS}
)

target_link_libraries(lexaire-flight-bridge PRIVATE
    MAVSDK::mavsdk
    Threads::Threads
    yaml-cpp
    ${ZMQ_LIBRARIES}
)