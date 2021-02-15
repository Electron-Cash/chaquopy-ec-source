# The Android Gradle plugin still requires Java 8, so use a Debian version which includes that.
FROM debian:stretch-20180831
SHELL ["/bin/bash", "-c"]
WORKDIR /root

RUN apt-get update && \
    apt-get install -y openjdk-8-jdk-headless unzip wget
RUN echo "progress=dot:giga" > .wgetrc

# Install the same minor Python version as Chaquopy uses.
RUN apt-get update && \
    apt-get install -y gcc libbz2-dev libffi-dev liblzma-dev libsqlite3-dev libssl-dev \
                       zlib1g-dev make
RUN version=3.8.7 && \
    wget https://www.python.org/ftp/python/$version/Python-$version.tgz && \
    tar -xf Python-$version.tgz && \
    cd Python-$version && \
    ./configure && \
    make -j $(nproc) && \
    make install && \
    cd .. && \
    rm -r Python-$version*

RUN filename=commandlinetools-linux-6609375_latest.zip && \
    wget https://dl.google.com/android/repository/$filename && \
    mkdir -p android-sdk/cmdline-tools && \
    unzip -q -d android-sdk/cmdline-tools $filename && \
    rm $filename
