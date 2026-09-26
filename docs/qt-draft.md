# Optional Qt6 draft preservation patch

The patch targets fcitx5-qt 5.1.14 and was tested with Qt 6.10.2 on X11. Upstream
Qt6 source files are symlinks to the shared Qt5 implementation. Apply to a clean
matching source tree using `patch --follow-symlinks -p1`, then build only the Qt6
plugin against a matching Qt private-header SDK. Build only in a test VM.

Load the compiled library from a private
`platforminputcontexts/libfcitx5platforminputcontextplugin.so` directory through
an application-local `QT_PLUGIN_PATH`. Enable policy for that process with
`TRANSIME_QT_PRESERVE_PANEL_DRAFT=1`. Do not replace the system Qt frontend or set
this globally. No private binary or Qt SDK is included in this source package.

When client preedit is empty, Qt `commit()` no longer cancels the server-side
panel draft. Explicit `reset()` still clears it. Field ownership is tracked with
a guarded Qt object pointer; changing fields clears the previous field's draft.
Reset must run while the input context is focused. Returning through temporary
null focus to a different field uses FocusIn then Reset before subsequent keys.

The policy also changes explicit application `commit()` with empty preedit; it
is not a universal compatibility fix. A fallback may briefly redraw candidates;
tests verified no wrong-field commit, not zero flicker. Qt5, Wayland, browser and
all office-app combinations remain unverified.

To disable, unset the policy and private plugin-path override before restarting
the test application. Already-running processes retain their loaded plugin.
Baseline behavior is unchanged when the opt-in policy is disabled.
