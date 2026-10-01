#!/usr/bin/python3
"""An empty real Qt text editor which observes input; it never inserts text."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--maximize", action="store_true",
                        help="Cover other applications for a dedicated candidate-popup screenshot")
    parser.add_argument("--sensitive", action="store_true",
                        help="Synthetic password-field fixture; never enter real credentials")
    parser.add_argument("--two-fields", action="store_true")
    parser.add_argument("--second-sensitive", action="store_true")
    parser.add_argument("--switch-field-on-deactivate", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Prepared only; --execute opens the dedicated empty Qt test window.")
        return
    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtGui import QKeySequence, QShortcut
    from PyQt6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QWidget, QVBoxLayout
    app = QApplication([])
    commits = []
    preedit_events = 0
    ctrl_enter_key_presses = 0
    focus_events = []
    second_commits = []

    # Keep the normal text fixture unchanged. The alternate widget exercises
    # Qt's real password echo mode plus its standard sensitive-input hints.
    base_editor = QLineEdit if args.sensitive else QPlainTextEdit

    class Editor(base_editor):
        def keyPressEvent(self, event):
            nonlocal ctrl_enter_key_presses
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                ctrl_enter_key_presses += 1
                event.accept()
                return
            super().keyPressEvent(event)

        def focusOutEvent(self, event):
            focus_events.append({"event": "out", "reason": event.reason().name,
                                 "monotonic": time.monotonic()})
            super().focusOutEvent(event)
            if (args.switch_field_on_deactivate and secondary is not None
                    and event.reason() == Qt.FocusReason.ActiveWindowFocusReason):
                QTimer.singleShot(0, lambda: secondary.setFocus())

        def focusInEvent(self, event):
            focus_events.append({"event": "in", "reason": event.reason().name,
                                 "monotonic": time.monotonic()})
            super().focusInEvent(event)

        def inputMethodEvent(self, event):
            nonlocal preedit_events
            if event.preeditString():
                preedit_events += 1
            if event.commitString():
                commits.append(event.commitString())
            super().inputMethodEvent(event)

    editor = Editor()
    if args.sensitive:
        editor.setEchoMode(QLineEdit.EchoMode.Password)
        editor.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoPredictiveText)
    window = editor
    secondary = None
    if args.two_fields:
        window = QWidget()
        layout = QVBoxLayout(window)
        layout.addWidget(editor)
        if isinstance(editor, QPlainTextEdit):
            editor.setTabChangesFocus(True)
        class Secondary(QLineEdit):
            def inputMethodEvent(self, event):
                if event.commitString():
                    second_commits.append(event.commitString())
                super().inputMethodEvent(event)
        secondary = Secondary()
        if args.second_sensitive:
            secondary.setEchoMode(QLineEdit.EchoMode.Password)
            secondary.setInputMethodHints(Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText)
        layout.addWidget(secondary)
        QWidget.setTabOrder(editor, secondary)
    # Explicit reset models an application's request, not an engine shortcut.
    reset_shortcut = QShortcut(QKeySequence("Ctrl+Alt+R"), window)
    reset_shortcut.activated.connect(lambda: app.inputMethod().reset())
    window.setWindowTitle(args.marker)
    window.resize(680, 300)
    if args.maximize:
        window.showMaximized()
    else:
        window.show()
    # This focuses only the widget in the newly created test window. The runner
    # must independently verify compositor focus before sending hardware keys.
    editor.setFocus()
    args.state.parent.mkdir(parents=True, exist_ok=True)

    def publish():
        value = {"pid": os.getpid(), "marker": args.marker, "kind": "qt",
                 "active": editor.isActiveWindow() and editor.hasFocus(),
                 "window_active": window.isActiveWindow(),
                 "monotonic": time.monotonic(),
                 "text": editor.text() if args.sensitive else editor.toPlainText(),
                 "ime_commits": commits[-10:], "preedit_events": preedit_events,
                 "ctrl_enter_key_presses": ctrl_enter_key_presses,
                 "qt_platform": app.platformName(), "focus_events": focus_events[-20:]}
        if secondary is not None:
            first_point = editor.mapToGlobal(editor.rect().center())
            second_point = secondary.mapToGlobal(secondary.rect().center())
            value.update({"focused_field": "first" if editor.hasFocus() else "second" if secondary.hasFocus() else None,
                          "secondary_text": secondary.text(), "secondary_commits": second_commits[-10:],
                          "secondary_sensitive": args.second_sensitive,
                          "first_center": [first_point.x(), first_point.y()],
                          "second_center": [second_point.x(), second_point.y()]})
        if args.sensitive:
            value["synthetic_sensitive"] = True
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.state)

    timer = QTimer()
    timer.timeout.connect(publish)
    timer.start(100)
    publish()
    app.exec()


if __name__ == "__main__":
    main()
