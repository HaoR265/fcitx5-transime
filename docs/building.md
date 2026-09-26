# Build scope for the source Alpha

## Reproducible entry point

The README core build requires CMake 3.16+, a C++17 compiler, and Python 3.11+ for
the public non-UI checks. It does not need a translator model or Fcitx SDK.
The public checker runs explicitly selected unittest modules; GUI tests and
real-app integration are separate and are not silently counted as passed.

For settings UI tests, prepare PySide6, D-Bus and Xvfb in your test VM, then run
inside an isolated session/display (desktop theme plugins may need these even
when the Qt surface is offscreen):

```sh
dbus-run-session -- xvfb-run -a env QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests -p test_settings_ui.py -v
```

No dependencies are automatically installed by the README check commands.

## Full input engine: advanced, not a turnkey installer

The engine needs C++20, json-c >= 0.16 and the exact Fcitx SDK selected with
`TRANSIME_FCITX_VERSION`. The tested recent engine used Fcitx 5.1.21, LibIME
1.1.15 and patched Pinyin 5.1.12. CMake's historical default is Fcitx 5.1.19;
set the version explicitly when using the newer SDK. The system's unpatched
Pinyin library is not a substitute for the TransIME bridge.

With a separately prepared matching SDK, the module build is:

```sh
cmake -S . -B build/plugin -DTRANSIME_BUILD_FCITX=ON \
  -DTRANSIME_FCITX_VERSION=5.1.21 -DTRANSIME_ENABLE_TEST_FIXTURE=OFF \
  -DCMAKE_PREFIX_PATH=/absolute/path/to/matching/sdk/prefix
cmake --build build/plugin -j2
```

This is only a build recipe; it neither creates the SDK nor installs a working
input method. Never deploy a build with `TRANSIME_ENABLE_TEST_FIXTURE=ON`.

Historical `scripts/prepare_sdk.py`, `scripts/prepare_host_sdk.py` and
`scripts/package_plugin.py` assume a layout with the source at
`WORKSPACE/outputs/fcitx5-transime` and generated artifacts at `WORKSPACE/work`.
They also check exact runtime snapshots and expect particular model/source
locations. They are retained for source completeness, **not a portable bootstrap
or endorsed one-command installation**. An unknown/mismatched runtime should
stop, not cause system packages or dictionaries to be overwritten.

`tools/install_settings.py` installs only the settings application. It does not
install the engine, bridge, runtime or model. Use explicit temporary data paths
for installation lifecycle tests; read its `--help` first.

Model preparation uses `scripts/prepare_model.py`, explicit `--work-dir`, and
`--allow-download` for network retrieval. Model licenses and attribution remain
separate. No model is required for synthetic protocol tests.

Fresh-machine full engine setup, portable SDK preparation, activation/rollback
and supported distribution packages remain release work. This Alpha supplies
source and component checks, not a stable binary distribution.
