# Introduction

Copyright (c) 2018 Chaquo Ltd

This repository contains Chaquopy source code for use with the Electron Cash Android app. It is
licensed only to that project, and is not open-source.

# Build

A Linux x86-64 machine with Docker is required. If necessary, install Docker using the
[instructions on its website](https://docs.docker.com/install/#supported-platforms).

Make sure all submodules are up to date:

    git submodule init && git submodule update

Then run the script `build-maven.sh`. This will generate a `maven` directory containing the
Chaquopy repository.

To use this repository to build an app, follow the standard [Chaquopy setup
instructions](https://chaquo.com/chaquopy/doc/current/android.html#basic-setup), but replace
the URL https://chaquo.com/maven/ with the URL or local path of this directory.
