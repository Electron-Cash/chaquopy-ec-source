cmake_minimum_required(VERSION 3.4.1)
project(runtime LANGUAGES C)

set(CMAKE_C_FLAGS_RELEASE "${CMAKE_C_FLAGS_RELEASE} -s")

include_directories(
    src/main/c
    ${CHAQUOPY_INCLUDE_DIRS})

if(CHAQUOPY_LIB_DIRS)
    link_directories(${CHAQUOPY_LIB_DIRS})
endif()

add_library(chaquopy MODULE
    src/main/c/chaquopy.c)
target_link_libraries(chaquopy ${CHAQUOPY_LIBS} dl)  # On MSYS2, libdl is in the "dlfcn" package.
set_target_properties(chaquopy PROPERTIES PREFIX "${PY_LIB_PREFIX}")
set_target_properties(chaquopy PROPERTIES SUFFIX "${PY_LIB_SUFFIX}")

add_library(chaquopy_java MODULE
    src/main/c/chaquopy_java.c)
target_link_libraries(chaquopy_java ${CHAQUOPY_LIBS})
set_target_properties(chaquopy_java PROPERTIES PREFIX "${JAVA_LIB_PREFIX}")
set_target_properties(chaquopy_java PROPERTIES SUFFIX "${JAVA_LIB_SUFFIX}")
