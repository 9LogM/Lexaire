cmake_minimum_required(VERSION 3.16)
project(orbis CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MAVSDK REQUIRED)
find_package(Boost REQUIRED COMPONENTS system)
find_package(Threads REQUIRED)
find_package(Curses REQUIRED)

add_executable(orbis
    src/main.cpp
    src/monitor.cpp
    src/qgc_bridge.cpp
)

target_include_directories(orbis PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CURSES_INCLUDE_DIR}
)

target_link_libraries(orbis PRIVATE
    MAVSDK::mavsdk
    Boost::system
    Threads::Threads
    ${CURSES_LIBRARIES}
)