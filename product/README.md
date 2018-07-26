# Introduction

This file contains instructions for building and packaging Chaquopy for use with Electron
Cash. This process has only been tested on Linux x86-64. However, the resulting packages can be
used on any supported Android build platform (Windows, Linux or Mac).


# Build prerequisites

* JDK 8 or higher.


# Android SDK

Create an empty directory for the Android SDK, and let its location be `$ANDROID_SDK`.

Download the Android [command line tools](https://developer.android.com/studio/) package
(there's no need to install Android Studio itself), and unzip it into `$ANDROID_SDK`.

Let `$COMPILE_SDK_VERSION` be the value given in
`product/buildSrc/src/main/java/com/chaquo/python/Common.java`. Then run the following:

	$ANDROID_SDK/tools/bin/sdkmanager --install ndk-bundle "cmake;3.6.4111459" "platforms;android-$COMPILE_SDK_VERSION"


# Crystax Python

Install [Crystax NDK](https://www.crystax.net/en/download) version 10.3.2. Let its location be
`$CRYSTAX_DIR`.

`$CRYSTAX_DIR/sources/python/3.6` must contain the Python libraries and includes.  Either
generate them using the instructions in `target/README.md`, or copy them from another machine.


# Build

Create a `local.properties` file in `product` (i.e. the same directory as this README), with
the following content (expand the variables yourself):

	sdk.dir=$ANDROID_SDK
	ndk.dir=$ANDROID_SDK/ndk-bundle
	crystax.dir=$CRYSTAX_DIR

Then run the following:

	cd product
	./gradlew gradle-plugin:build

The Gradle plugin JAR and POM files will now be in `product/gradle-plugin/build/libs`.
