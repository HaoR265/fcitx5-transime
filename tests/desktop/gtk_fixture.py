#!/usr/bin/python3
"""Empty real GTK text editor which observes input, never inserts test text.

Buffer insert events include ordinary typing and are not IME commit events.
"""
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
    parser.add_argument("--maximize", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Prepared only; --execute opens the dedicated empty GTK test window.")
        return

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    window = Gtk.Window(title=args.marker)
    window.set_default_size(680, 300)
    window.connect("destroy", Gtk.main_quit)
    editor = Gtk.TextView()
    editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    editor.set_left_margin(12)
    editor.set_right_margin(12)
    scroll = Gtk.ScrolledWindow()
    scroll.add(editor)
    window.add(scroll)
    buffer = editor.get_buffer()
    insert_events = []

    def observe_insert(_buffer, location, text, length):
        insert_events.append({"offset": location.get_offset(), "text": text,
                              "utf8_bytes": length})
        del insert_events[:-10]

    buffer.connect("insert-text", observe_insert)
    window.show_all()
    if args.maximize:
        window.maximize()
    editor.grab_focus()
    args.state.parent.mkdir(parents=True, exist_ok=True)

    def publish():
        display = Gdk.Display.get_default()
        value = {"pid": os.getpid(), "marker": args.marker, "kind": "gtk",
                 "active": window.is_active() and editor.has_focus(),
                 "monotonic": time.monotonic(),
                 "text": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                 "buffer_insert_events": insert_events,
                 "toolkit": f"GTK {Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}.{Gtk.MICRO_VERSION}",
                 "display": display.get_name() if display else None}
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.state)
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(100, publish)
    publish()
    Gtk.main()


if __name__ == "__main__":
    main()
