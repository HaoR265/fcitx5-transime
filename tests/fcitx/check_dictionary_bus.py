#!/usr/bin/env python3
"""Verify dictionary-manager D-Bus reload on a NEW bus, inside a test VM only.

Launch with dbus-run-session. This starts its own headless Fcitx and never joins
the desktop bus or reads personal Fcitx paths. A successful method invocation is
not proof that a particular dictionary appears in candidates.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--addon-dir", action="append", required=True)
    parser.add_argument("--data-dir", action="append", required=True)
    parser.add_argument("--library-dir", action="append", default=[])
    args = parser.parse_args()
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        parser.error("run on a fresh test bus using dbus-run-session")
    # Refuse an occupied bus instead of sending any request to its owner.
    status = subprocess.run([
        "gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
        "--object-path", "/org/freedesktop/DBus",
        "--method", "org.freedesktop.DBus.NameHasOwner", "org.fcitx.Fcitx5"],
        text=True, capture_output=True, check=True)
    if "false" not in status.stdout:
        raise RuntimeError("refusing a bus with an existing Fcitx owner")
    args.work_root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="bus-", dir=args.work_root))
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("FCITX_") or name in {
                "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SYSTEM_BUS_ADDRESS",
                "LD_PRELOAD", "LD_LIBRARY_PATH", "SKIP_FCITX_USER_PATH"}:
            env.pop(name, None)
    for name, child in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")]:
        location = run / child
        location.mkdir(mode=0o700)
        env[name] = str(location)
    env.update({
        "FCITX_DATA_HOME": str(run / "data/fcitx5"),
        "FCITX_CONFIG_HOME": str(run / "config/fcitx5"),
        "FCITX_CONFIG_DIRS": str(run / "config/fcitx5"),
        "FCITX_DATA_DIRS": ":".join(args.data_dir),
        "FCITX_ADDON_DIRS": ":".join(args.addon_dir),
        "SKIP_FCITX_PATH": "1", "LD_LIBRARY_PATH": ":".join(args.library_dir),
    })
    with (run / "fcitx.log").open("w") as log:
        process = subprocess.Popen(["fcitx5", "--disable=all",
            "--enable=dbus,pinyin,pinyinhelper,punctuation,keyboard"],
            env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            command = ["gdbus", "call", "--session", "--dest", "org.fcitx.Fcitx5",
                "--object-path", "/controller", "--method",
                "org.fcitx.Fcitx.Controller1.SetConfig",
                "fcitx://config/addon/pinyin/dictmanager", "<@a{sv} {}>"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                # Query the bus itself first: calling the destination too early
                # would auto-activate an unconfigured second Fcitx process.
                owner = subprocess.run([
                    "gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                    "--object-path", "/org/freedesktop/DBus", "--method",
                    "org.freedesktop.DBus.NameHasOwner", "org.fcitx.Fcitx5"],
                    text=True, capture_output=True, timeout=5)
                if "true" not in owner.stdout:
                    if process.poll() is not None:
                        raise RuntimeError((run / "fcitx.log").read_text())
                    time.sleep(0.2)
                    continue
                result = subprocess.run(command, text=True, capture_output=True, timeout=5)
                if result.returncode == 0:
                    print("PASS: isolated D-Bus dictionary-manager reload", result.stdout.strip())
                    print("Log:", run / "fcitx.log")
                    return 0
                if process.poll() is not None:
                    raise RuntimeError((run / "fcitx.log").read_text())
                time.sleep(0.2)
            raise RuntimeError("isolated Fcitx did not accept dictionary reload before deadline")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
