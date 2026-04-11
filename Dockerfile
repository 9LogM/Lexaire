FROM arm64v8/debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    wget cmake build-essential libboost-all-dev \
    git meson ninja-build pkg-config libsystemd-dev \
    libncurses-dev \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'Name: systemd\nDescription: systemd\nVersion: 252\nLibs: -lsystemd\nCflags: -I/usr/include/systemd\nsystemdsystemunitdir=/lib/systemd/system\nsystemduserunitdir=/lib/systemd/user\n' \
       > /usr/lib/pkgconfig/systemd.pc

RUN wget https://github.com/mavlink/MAVSDK/releases/download/v3.9.0/libmavsdk-dev_3.9.0_debian12_arm64.deb && \
    apt-get install -y ./libmavsdk-dev_3.9.0_debian12_arm64.deb && \
    rm libmavsdk-dev_3.9.0_debian12_arm64.deb

RUN git clone --recurse-submodules --shallow-submodules \
        https://github.com/mavlink-router/mavlink-router.git /tmp/mlr \
    && cd /tmp/mlr && meson setup build && ninja -C build \
    && cp build/src/mavlink-routerd /usr/local/bin/ \
    && rm -rf /tmp/mlr

WORKDIR /workspace

COPY . .

RUN mkdir build && cd build && \
    cmake .. && \
    make

ENTRYPOINT ["/workspace/build/orbis"]
