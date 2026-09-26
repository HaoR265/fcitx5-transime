find_package(PkgConfig REQUIRED)
pkg_check_modules(TRANSIME_JSONC REQUIRED IMPORTED_TARGET json-c>=0.16)
add_library(transime MODULE transime.cpp worker_client.cpp)
target_link_libraries(transime PRIVATE transime_core Fcitx5::Core Fcitx5::Config PkgConfig::TRANSIME_JSONC)
target_include_directories(transime PRIVATE "${PROJECT_SOURCE_DIR}/bridge")
target_compile_features(transime PRIVATE cxx_std_20)
target_compile_options(transime PRIVATE -Wall -Wextra -Wpedantic)
target_compile_definitions(transime PRIVATE TRANSIME_ENABLE_TEST_FIXTURE=$<BOOL:${TRANSIME_ENABLE_TEST_FIXTURE}>)
configure_file(transime.conf.in transime.conf @ONLY)
# No host installation target at this experimental stage. The headless test
# copies the module and addon metadata into its private loader directories.
