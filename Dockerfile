FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

ARG MAVSDK_VERSION=3.17.0

RUN apt-get update && apt-get install -y \
    wget cmake build-essential \
    libboost-all-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/mavlink/MAVSDK/releases/download/v${MAVSDK_VERSION}/libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb && \
    apt-get install -y ./libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb && \
    rm libmavsdk-dev_${MAVSDK_VERSION}_debian13_amd64.deb

WORKDIR /workspace

COPY . .

RUN mkdir build && cd build && \
    cmake .. && \
    make

ENTRYPOINT ["/workspace/build/orbis"]
