FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

ARG MLR_VERSION=v4

RUN apt-get update && apt-get install -y \
    git meson ninja-build pkg-config build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'Name: systemd\nDescription: systemd\nVersion: 9\nLibs: -lsystemd\nCflags: -I/usr/include/systemd\nsystemdsystemunitdir=/lib/systemd/system\nsystemduserunitdir=/lib/systemd/user\n' \
       > /usr/lib/pkgconfig/systemd.pc

RUN git clone --recurse-submodules --shallow-submodules --branch ${MLR_VERSION} \
        https://github.com/mavlink-router/mavlink-router.git /tmp/mlr \
    && cd /tmp/mlr && meson setup build && ninja -C build \
    && cp build/src/mavlink-routerd /usr/local/bin/ \
    && rm -rf /tmp/mlr

COPY start-relay.sh /start-relay.sh
RUN chmod +x /start-relay.sh

ENTRYPOINT ["/start-relay.sh"]
