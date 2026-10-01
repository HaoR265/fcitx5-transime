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

## Additional VM input-path checks — 2026-09-30

In the Kali VM, the current source was rebuilt against the private Fcitx5
5.1.21 SDK. The plugin CTest passed 2/2. The new Rime 5.1.14 bridge was built
from the hash-checked upstream archive, and the amended Pinyin 5.1.12 patch
passed `git apply --recount --check` against a fresh upstream extraction.
These were private VM builds, not installed into the host input method.

| Input method | Surface | Verified basic actions |
| --- | --- | --- |
| Rime | Qt/X11, GTK/X11 | Chinese candidate, translation, cancel |
| Rime | Qt/Wayland, GTK/Wayland | Chinese candidate, translation, cancel |
| Double-pinyin (`nihk`) | Qt/X11, GTK/X11 | Chinese candidate, translation |
| Double-pinyin (`nihk`) | Qt/Wayland, GTK/Wayland | Chinese candidate, translation, cancel |
| Full pinyin | Qt/Wayland, GTK/Wayland | Chinese candidate, translation, cancel regression |

The Wayland runs used a private Weston compositor nested under Xvfb and real
fixture windows receiving injected keys. For Rime and the TransIME plugin,
the harness also checked the running Fcitx process mappings against the newly
built private addon paths, to exclude an old system library being tested by
mistake. Initial double-pinyin translation failed because the Pinyin snapshot
explicitly rejected `shuangpin`; allowing its complete highlighted candidate
in the same read-only snapshot path fixed that failure in the tested key sequence.

These checks establish only the listed VM, toolkit and fixture combinations.
They do not establish native Wayland-session behavior across compositors,
browser/office compatibility, all double-pinyin schemes, Rime schemas,
complete mixed-paragraph behavior, installer reliability or translation quality.
The raw VM logs and model files are not part of the public source tree.

## Empty-shortcut regression — 2026-10-01

A new VM-only Qt/Wayland case checks whether Ctrl+Enter reaches the actual
editor when no composition has been typed. The fixture records the editor's
key-press handler, rather than treating an input-method return value as proof
of application delivery. The cold case is opt-in as `--cases passthrough`; an
`after_cancel` variant first types and cancels a composition, and
`--disable-transime` supplies a negative control.

With the current source rebuilt in the Kali VM, the selected Rime profile and
`shuangpin` delivered the cold empty shortcut to the editor. The cold Rime
case checks the selected profile but does not force the lazily loaded Rime
addon to map; the separate Rime translation and Chinese-candidate cases did
verify the private addon mapping and passed. Full pinyin did not deliver the
cold empty shortcut in two repeat runs; disabling TransIME while keeping the
same pinyin engine made the negative control pass. After a canceled
composition, pinyin and `shuangpin` did deliver it. The Rime after-cancel
case timed out during addon verification and is inconclusive. A later
cold-boot pinyin run also timed out while waiting for addon activation and
adds no key-delivery conclusion. The cold full-pinyin behavior remains an
open compatibility bug, not a passed regression. No unverified plugin
workaround from this investigation is included in the source.
