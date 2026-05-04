FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

ARG MAVSDK_VERSION=3.17.0

RUN apt-get update && apt-get install -y \
    wget cmake build-essential pkg-config \
    libboost-system-dev libboost-filesystem-dev libncurses-dev \
    libyaml-cpp-dev libzmq3-dev nlohmann-json3-dev \
    ca-certificates curl gnupg openssh-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian trixie stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/mavlink/MAVSDK/releases/download/v${MAVSDK_VERSION}/libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb && \
    apt-get install -y ./libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb && \
    rm libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb

RUN echo "StrictHostKeyChecking accept-new" >> /etc/ssh/ssh_config

WORKDIR /workspace

# Copy build inputs first so changes to README/python/scripts don't invalidate
# the C++ build cache.
COPY CMakeLists.txt ./
COPY include ./include
COPY src ./src
RUN mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make -j"$(nproc)"

# Everything else (compose files, scripts, docs, python). Cheap to recopy.
COPY . .
RUN chmod +x /workspace/docker-entrypoint.sh

ENTRYPOINT ["/workspace/docker-entrypoint.sh"]
