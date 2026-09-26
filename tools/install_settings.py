#!/usr/bin/env python3
"""Install, upgrade, or uninstall the user-level settings window only.

Never changes the input engine, configuration, dictionaries, or learning data.
Development executions belong in a VM, not the daily-use host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

MANIFEST = ".transime-settings-manifest.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symlink target: {part}")
    return path


def inventory(root):
    files, directories = {}, []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            if relative != MANIFEST:
                files[relative] = digest(path)
        else:
            raise ValueError(f"Refusing non-regular file: {path}")
    return files, directories


def desktop_text(destination):
    script = str(destination / "settings/app.py")
    if any(c in script for c in "\n\r\x00"):
        raise ValueError("Unsupported path characters.")
    escaped = script.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return ('[Desktop Entry]\nType=Application\nName=TransIME 设置\n'
            'Comment=输入、快捷键、独立词库与本地翻译设置\nExec=python3 -B "' + escaped +
            '"\nIcon=preferences-desktop-keyboard\nTerminal=false\nCategories=Settings;Utility;\n')


def verify(destination, desktop):
    safe_path(destination)
    safe_path(desktop)
    manifest = destination / MANIFEST
    safe_path(manifest)
    if not destination.is_dir() or not manifest.is_file():
        raise ValueError("Existing installation is unowned (no manifest); nothing replaced.")
    if manifest.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid ownership manifest.")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("owner") != "transime-settings":
        raise ValueError("Invalid ownership manifest.")
    files, directories = inventory(destination)
    if files != data.get("files") or directories != data.get("directories"):
        raise ValueError("Installation changed or contains unowned files; nothing removed.")
    # No path from the manifest is ever passed to a filesystem operation.
    if not desktop.is_file() or digest(desktop) != data.get("desktop_sha256"):
        raise ValueError("Desktop entry changed or missing; nothing removed.")
    return data


def remove_tree(root):
    """Remove only a previously verified/private tree, without recursive deletion."""
    paths = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            raise ValueError("Tree changed during operation; retained for inspection.")
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()


def install(source, data_home, upgrade=False):
    data_home = safe_path(data_home)
    destination = safe_path(data_home / "transime-settings")
    desktop = safe_path(data_home / "applications/transime-settings.desktop")
    existing = destination.exists() or desktop.exists()
    if existing:
        if not upgrade:
            raise ValueError("Installation exists. Use --upgrade for a verified owned installation.")
        verify(destination, desktop)
    elif upgrade:
        raise ValueError("No installation to upgrade. Run without --upgrade first.")
    source = Path(source)
    # Validate the source tree before copytree can follow a link.
    for name in ("settings", "examples/dictionaries"):
        safe_path(source / name)
        if not (source / name).is_dir():
            raise ValueError(f"Missing source directory: {name}")
        inventory(source / name)
    data_home.mkdir(parents=True, exist_ok=True)
    desktop.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".transime-settings-", dir=data_home))
    staged, old = temporary / "payload", temporary / "previous"
    old_desktop = temporary / "previous.desktop"
    new_desktop = temporary / "new.desktop"
    committed = False
    try:
        shutil.copytree(source / "settings", staged / "settings", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(source / "examples/dictionaries", staged / "examples/dictionaries")
        new_desktop.write_text(desktop_text(destination), encoding="utf-8")
        files, directories = inventory(staged)
        (staged / MANIFEST).write_text(json.dumps({"format": 1, "owner": "transime-settings", "files": files,
            "directories": directories, "desktop_sha256": digest(new_desktop)}, indent=2) + "\n", encoding="utf-8")
        if existing:
            verify(destination, desktop)
            destination.rename(old)
            try:
                desktop.rename(old_desktop)
            except BaseException:
                old.rename(destination)
                raise
        desktop_created = False
        try:
            staged.rename(destination)
            # Exclusive creation avoids overwriting another application's entry.
            with desktop.open("xb") as stream:
                desktop_created = True
                stream.write(new_desktop.read_bytes())
            committed = True
        except BaseException:
            if desktop_created:
                desktop.unlink()
            if destination.exists():
                destination.rename(staged)
            if old.exists():
                old.rename(destination)
            if old_desktop.exists() and not desktop.exists():
                old_desktop.rename(desktop)
            raise
    finally:
        # Preserve rollback data on any incomplete rollback, never discard it.
        if committed or not old.exists() and not old_desktop.exists():
            remove_tree(temporary)
    return destination


def uninstall(data_home):
    data_home = safe_path(data_home)
    destination = data_home / "transime-settings"
    desktop = data_home / "applications/transime-settings.desktop"
    verify(destination, desktop)
    # Only the private code tree and exact verified launcher are owned.
    desktop.unlink()
    remove_tree(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-home", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--upgrade", action="store_true")
    actions.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        if args.uninstall:
            uninstall(args.data_home)
            print("Settings removed. Input engine, configuration and user dictionaries preserved.")
        else:
            from PySide6 import QtCore, QtWidgets  # Check before writes; uninstall needs no Qt.
            destination = install(Path(__file__).resolve().parents[1], args.data_home, args.upgrade)
            print(f"Settings {'upgraded' if args.upgrade else 'installed'}: {destination}\nPySide6 {QtCore.__version__}")
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"Settings operation stopped: {exc}\n")


if __name__ == "__main__":
    main()
