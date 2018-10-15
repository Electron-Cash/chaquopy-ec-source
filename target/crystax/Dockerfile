FROM debian:stretch
SHELL ["/bin/bash", "-c"]

WORKDIR /root

# Install original Crystax NDK.
RUN apt-get update && \
    apt-get install -y wget xz-utils && \
    wget --progress=dot:giga \
        https://www.crystax.net/download/crystax-ndk-10.3.2-linux-x86_64.tar.xz && \
    tar -xf crystax-ndk-10.3.2-linux-x86_64.tar.xz && \
    rm crystax-ndk-10.3.2-linux-x86_64.tar.xz && \
    mv crystax-ndk-10.3.2 crystax

COPY platform src/crystax/platform
COPY toolchain src/crystax/toolchain
COPY vendor src/crystax/vendor

RUN apt-get update && \
    apt-get install -y file gcc make ruby

ENV ABIS="armeabi-v7a,arm64-v8a,x86"

# Rebuild libcrystax to fix several bugs (see commit history of `crystax/platform/ndk`).
RUN NDK=$(pwd)/crystax ABIS=$ABIS make -j $(nproc) -C src/crystax/platform/ndk/sources/crystax && \
    rm -r crystax/sources/crystax && \
    mv src/crystax/platform/ndk/sources/crystax crystax/sources

RUN rm -r src