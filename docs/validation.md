# Source Alpha validation — 2026-09-26

## This exported source snapshot

The exported source was unpacked into a newly created directory in a Kali test
VM and checked as an unprivileged user, without accessing the developer source
tree, changing the daily input method or loading a translator model.

| Check | Result |
| --- | --- |
| Release core configure/build | Passed, CMake + GNU C++ 16.2 |
| Core/cache CTest | 2/2 passed |
| Public Python component suites | 111/111 passed, Python 3.14 |
| Settings UI suite | 9/9 passed, PySide6 6.10.3, private D-Bus/Xvfb session |

The eight public suites cover context (11), numbers (10), worker (32), worker
resilience (6), evaluation tooling (13), dictionaries (12), settings configuration
(13), and settings install/upgrade/uninstall with artificial data (14).

Fresh-directory testing found and fixed an old test path assumption that tried
to write outside a normal clone. Evaluation artifacts now default to temporary
directories; `TRANSIME_WORK_DIR` remains available as an explicit override.
The initial offscreen GUI command lacked a display for a desktop theme plugin;
the documented isolated D-Bus/Xvfb command passed all nine tests.

This is **not a fresh operating-system installation test**: the VM already had
compiler, Python and UI test dependencies. It does not establish that the full
input engine can be installed on an arbitrary new machine. Core/protocol tests
use synthetic translators and do not measure translation quality.

## Earlier full input-path regression

Before export, the matching isolated engine and optional Qt frontend had passed
Qt 21/21 and GTK 16/16 desktop cases. Six focus/cancel/reset/field-isolation cases
passed again on an independent repeat. Disabling the Qt policy with the same
private library reproduced the window-switch draft loss (negative control).

The tested Qt stack was Qt 6.10.2 / fcitx5-qt 5.1.14 / X11, Fcitx5 5.1.21 with
the isolated Pinyin 5.1.12 bridge. The Qt patch and desktop fixture source were
copied unchanged. This is prior evidence for that exact setup, not a claim that
all desktop tests were rerun from the exported package.

No private binaries, full environment inventories, personal text, raw desktop
logs or installation receipts are distributed in this repository. Synthetic
test source is included so results can be independently investigated.
