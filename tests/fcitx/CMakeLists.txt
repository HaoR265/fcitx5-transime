cmake_minimum_required(VERSION 3.18)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(transime_fcitx_headless_tests LANGUAGES CXX)
endif()

set(TRANSIME_FCITX_VERSION "5.1.19" CACHE STRING "Fcitx SDK version for isolated tests")
find_package(Fcitx5Core ${TRANSIME_FCITX_VERSION} EXACT CONFIG REQUIRED)
set(TRANSIME_TEST_KEYBOARD_LIBRARY "" CACHE FILEPATH
    "Matching isolated SDK build's libkeyboard.a (not installed system addons)")
if(NOT EXISTS "${TRANSIME_TEST_KEYBOARD_LIBRARY}")
    message(FATAL_ERROR "Set TRANSIME_TEST_KEYBOARD_LIBRARY to the isolated Fcitx ${TRANSIME_FCITX_VERSION} libkeyboard.a")
endif()
add_executable(transime_fcitx_headless_test headless_test.cpp)
target_compile_features(transime_fcitx_headless_test PRIVATE cxx_std_20)
target_link_libraries(transime_fcitx_headless_test PRIVATE
    "${TRANSIME_TEST_KEYBOARD_LIBRARY}" Fcitx5::Core expat xkbcommon)
target_include_directories(transime_fcitx_headless_test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../bridge"
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugin"
    "${CMAKE_CURRENT_SOURCE_DIR}/../../core")
target_compile_options(transime_fcitx_headless_test PRIVATE -Wall -Wextra -Wpedantic)

# This executable requires explicit isolated runtime paths. Do not register an
# unqualified ctest invocation which could accidentally inherit desktop paths.
