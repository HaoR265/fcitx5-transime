# Contributing

This is a Linux/Fcitx5 source Alpha. Please open an issue to discuss large changes.
Include your distribution, Fcitx/Pinyin/Qt versions, X11 or Wayland, application,
expected result, and a minimal synthetic reproduction. Do not attach personal
word lists, raw input histories, credentials or system configuration dumps.

Run builds and input tests in a disposable VM. Start with the README core checks;
UI tests need PySide6 and integration tests additionally need a matching SDK and
isolated display/session. No test should type into the user's active desktop.

Separate core correctness, real-app typing, installation compatibility and
translation quality in reports. A synthetic translator passing tests is not a
translation quality result. Tests named heldout are now public development data.

Preserve file-specific licenses and add tests for changes. Contributions are
offered under the applicable file's license unless explicitly agreed otherwise.
Do not submit third-party code or training data without documented provenance.
