# Introduction

Copyright (c) 2018 Chaquo Ltd

This repository contains Chaquopy source code for use with the Electron Cash Android app. It is
licensed only to that project, and is not open-source.


# Build

The build has two parts, which should be run in the following order:

* `target/README.md` contains instructions for Python and its dependencies.
* `product/README.md` contains instructions for the Chaquopy runtime and Gradle plugin.


# Deployment

To use the built packages, we need to deploy them to a Maven repository similar to the
[official Chaquopy one](https://chaquo.com/maven/). The repository is simply a directory
structure, which can be placed either on the local machine or on a webserver.

Arrange the packages built in the previous section as follows:

    maven
    └── com
        └── chaquo
            └── python
                ├── gradle
                │   └── 3.3.1
                │       ├── gradle-3.3.1.jar
                │       └── gradle-3.3.1.pom
                └── target
                    └── 3.6.5-4
                        ├── target-3.6.5-4-armeabi-v7a.zip
                        ├── target-3.6.5-4-stdlib.zip
                        ├── target-3.6.5-4-stdlib-pyc.zip
                        └── target-3.6.5-4-x86.zip

Now, to use this repository to build an app, follow the standard [Chaquopy setup
instructions](https://chaquo.com/chaquopy/doc/current/android.html#basic-setup), but replace
the URL https://chaquo.com/maven/ with the URL or local path of your own repository.
