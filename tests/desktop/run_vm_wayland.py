#!/usr/bin/env python3
"""Exercise Wayland Qt/GTK input in a private Weston/Xvfb session inside a VM."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


class Seat:
    KEYS = {0x20: "space", 0xFF0D: "Return", 0xFF1B: "Escape"}

    def __init__(self):
        def find_window():
            tree = subprocess.run(["xwininfo", "-root", "-tree"],
                                  capture_output=True, text=True, check=True).stdout
            match = re.search(r'(0x[0-9a-f]+) "Weston Compositor - screen0"', tree)
            return match.group(1) if match else None
        self.window = until(find_window, 10)
        subprocess.run(["xdotool", "windowfocus", "--sync", self.window], check=True, timeout=5)

    def key(self, symbol):
        key = symbol if isinstance(symbol, str) else self.KEYS.get(symbol, chr(symbol))
        subprocess.run(["xdotool", "key", "--clearmodifiers", key], check=True, timeout=5)

    def type(self, text):
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "80", text], check=True, timeout=10)

    def click(self, x, y):
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, timeout=5)
        subprocess.run(["xdotool", "click", "1"], check=True, timeout=5)


def until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.08)
    raise AssertionError("condition timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolkit", choices=("qt", "gtk"), default="qt")
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--transime-addon-dir", type=Path, required=True)
    parser.add_argument("--rime-addon-dir", type=Path)
    parser.add_argument("--qt-plugin-path", type=Path)
    parser.add_argument("--input-method", choices=("pinyin", "shuangpin", "rime"), default="pinyin")
    parser.add_argument("--cases", default="chinese,raw,cancel")
    parser.add_argument("--disable-transime", action="store_true",
                        help="Negative control: keep the same engine but disable TransIME")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.input_method == "rime" and not args.rime_addon_dir:
        parser.error("Rime checks require --rime-addon-dir with the patched addon")
    if subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True).returncode:
        raise SystemExit("VM-only test")
    if not args.inside:
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                                "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH", "LD_PRELOAD")
                 and not key.startswith(("FCITX_", "QT_"))}
        clean["TRANSIME_PRIVATE_WESTON"] = "1"
        return subprocess.call(["xvfb-run", "-a", "-s", "-screen 0 1280x800x24",
                                "dbus-run-session", sys.executable, __file__,
                                *sys.argv[1:], "--inside"], env=clean)
    if os.environ.get("TRANSIME_PRIVATE_WESTON") != "1":
        raise SystemExit("private Weston wrapper missing")
    project = Path(__file__).resolve().parents[2]
    base = args.build_root.resolve()
    models = args.model_root.resolve()
    root = Path(tempfile.mkdtemp(prefix="transime-wayland-", dir="/var/tmp"))
    env = dict(os.environ)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["WAYLAND_DISPLAY"] = "wayland-transime"
    user_home = root / "home"
    for key, place in (("HOME", user_home),
                       ("XDG_CONFIG_HOME", root / "config"),
                       ("XDG_DATA_HOME", root / "data"),
                       ("XDG_CACHE_HOME", root / "cache")):
        place.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[key] = str(place)
    config = root / "config/fcitx5"
    (config / "conf").mkdir(parents=True, exist_ok=True)
    (config / "profile").write_text("[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=" + args.input_method +
        "\n\n[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n[Groups/0/Items/1]\nName=" + args.input_method +
        "\nLayout=\n\n[GroupOrder]\n0=Default\n")
    (config / "conf/transime.conf").write_text(
        f"Enabled={'False' if args.disable_transime else 'True'}\n"
        "ContinuousComposition=True\nShowModeHints=False\nEnableHistory=False\nContextAware=False\n"
        f"PythonExecutable={models}/baseline-python/bin/python\nWorkerScript={project}/worker/transime_worker.py\n"
        f"ModelDirectory={models}/baseline-model\nRequestTimeoutMilliseconds=5000\nColdStartTimeoutMilliseconds=15000\n"
        "\n[TranslateKey]\n0=Control+Return\n")
    data_home = root / "data/fcitx5"
    addon_dirs = ":".join(str(path.resolve()) for path in
                          (args.transime_addon_dir, args.rime_addon_dir) if path)
    addon_dirs += f":{base}/bridge/bin:{base}/plugin/plugin"
    if args.input_method == "rime":
        rime_data = data_home / "rime"
        rime_data.mkdir(parents=True, exist_ok=True)
        (rime_data / "default.custom.yaml").write_text(
            "patch:\n  schema_list:\n    - schema: pinyin_simp\n")
    env.update({"FCITX_CONFIG_HOME": str(config), "FCITX_DATA_HOME": str(data_home),
        "FCITX_CONFIG_DIRS": str(config), "FCITX_DATA_DIRS": str(base / "staging") + ":/usr/share/fcitx5",
        "FCITX_ADDON_DIRS": addon_dirs + ":/usr/lib/x86_64-linux-gnu/fcitx5",
        "LD_LIBRARY_PATH": f"{base}/sdk/prefix/usr/lib/x86_64-linux-gnu:{base}/sdk/deps/usr/lib/x86_64-linux-gnu",
        "XDG_CONFIG_DIRS": str(root / "config"), "QT_IM_MODULE": "fcitx",
        "QT_QPA_PLATFORM": "wayland", "GTK_IM_MODULE": "fcitx", "XMODIFIERS": "@im=fcitx",
        "GDK_BACKEND": "wayland",
        "SKIP_FCITX_PATH": "1", "QT_DEBUG_PLUGINS": "1"})
    if args.qt_plugin_path:
        env["QT_PLUGIN_PATH"] = str(args.qt_plugin_path.resolve())
        env["TRANSIME_QT_PRESERVE_PANEL_DRAFT"] = "1"
    children = []
    handles = []
    def launch(argv, name):
        handle = (root / name).open("w")
        handles.append(handle)
        process = subprocess.Popen(argv, env=env, stdout=handle, stderr=subprocess.STDOUT)
        children.append(process)
        return process
    outcomes = []
    try:
        weston = launch(["weston", "--backend=x11-backend.so", "--socket=wayland-transime",
                         "--width=1024", "--height=768", "--idle-time=0"], "weston.log")
        until(lambda: (runtime / "wayland-transime").exists() or weston.poll() is not None, 12)
        if weston.poll() is not None:
            raise RuntimeError("private Weston failed to start; see " + str(root / "weston.log"))
        fcitx = launch(["fcitx5", "--disable=all",
                "--enable=dbus,dbusfrontend,xcb,xim,classicui,keyboard,pinyin,rime,pinyinhelper,punctuation,transime"], "fcitx.log")
        time.sleep(1)
        print(f"fcitx process after startup: {fcitx.poll()}", flush=True)
        required = [args.transime_addon_dir.resolve() / "libtransime.so"]
        if args.input_method == "rime":
            required.append(args.rime_addon_dir.resolve() / "librime.so")
        def verify_private_addons(require_rime=True):
            active_required = required if require_rime else required[:1]
            mapped = Path(f"/proc/{fcitx.pid}/maps").read_text()
            if not all(str(library) in mapped for library in active_required):
                return False
            (root / "addon-maps.txt").write_text("\n".join(
                line for line in mapped.splitlines()
                if any(str(library) in line for library in active_required)) + "\n")
            return True
        seat = Seat()
        sources = {"chinese": ("nihk" if args.input_method == "shuangpin" else "nihao", 0x20, "你好"),
                   "raw": ("nihk" if args.input_method == "shuangpin" else "nihao", 0xFF0D,
                           "nihk" if args.input_method == "shuangpin" else "nihao"),
                   "translation": ("nihk" if args.input_method == "shuangpin" else "nihao", "ctrl+Return", "Hello."),
                   "cancel": ("nihk" if args.input_method == "shuangpin" else "nihao", 0xFF1B, ""),
                   "passthrough": ("", "ctrl+Return", ""),
                   "after_cancel": ("nihk" if args.input_method == "shuangpin" else "nihao", "ctrl+Return", "")}
        selected = args.cases.split(",")
        if not selected or any(name not in sources for name in selected):
            raise ValueError("invalid --cases selection")
        if any(name in selected for name in ("passthrough", "after_cancel")) and args.toolkit != "qt":
            raise ValueError("key pass-through cases require the Qt key-event fixture")
        for name in selected:
            source, submit, expected = sources[name]
            marker = "TransIME Wayland " + name
            state = root / (name + ".json")
            app = launch([sys.executable, str(project / f"tests/desktop/{args.toolkit}_fixture.py"),
                          "--state", str(state), "--marker", marker, "--execute"], name + ".log")
            def read():
                try:
                    return json.loads(state.read_text())
                except (OSError, json.JSONDecodeError):
                    return {}
            try:
                if args.toolkit == "qt":
                    until(lambda: read().get("qt_platform") == "wayland")
                else:
                    until(lambda: str(read().get("display", "")).startswith("wayland"))
                seat.click(400, 300)
                until(lambda: read().get("active"))
                subprocess.run(["fcitx5-remote", "-s", args.input_method], env=env,
                               check=True, capture_output=True, text=True, timeout=5)
                subprocess.run(["fcitx5-remote", "-o"], env=env,
                               check=True, capture_output=True, text=True, timeout=5)
                until(lambda: subprocess.run(["fcitx5-remote"], env=env,
                    capture_output=True, text=True).stdout.strip() == "2")
                until(lambda: subprocess.run(["fcitx5-remote", "-n"], env=env,
                    capture_output=True, text=True).stdout.strip() == args.input_method)
                # Rime is loaded on first selection, not necessarily at fcitx startup.
                until(lambda: verify_private_addons(name not in ("passthrough", "after_cancel")))
                if args.input_method == "rime":
                    time.sleep(1)
                if source:
                    seat.type(source)
                if name == "after_cancel":
                    seat.key(0xFF1B)
                    until(verify_private_addons)
                time.sleep(0.6)
                before = read()
                if before.get("text"):
                    raise AssertionError("early commit: " + repr(before))
                attempts = 1
                if name == "translation":
                    # Translation is asynchronous; an early shortcut must not
                    # discard the Rime/pinyin composition. Retry the explicit
                    # user action while the model warms, bounded to 10 seconds.
                    for attempts in range(1, 21):
                        seat.key(submit)
                        time.sleep(0.5)
                        if read().get("text") == expected:
                            break
                else:
                    seat.key(submit)
                if expected:
                    until(lambda: read().get("text") == expected)
                else:
                    time.sleep(0.3)
                after = read()
                if after.get("text") != expected:
                    raise AssertionError("wrong text: " + repr(after))
                if (name in ("passthrough", "after_cancel") and
                        after.get("ctrl_enter_key_presses", 0) !=
                        before.get("ctrl_enter_key_presses", 0) + 1):
                    raise AssertionError("Ctrl+Enter did not reach the application: " + repr(after))
                outcomes.append({"case": name, "passed": True, "before": before, "after": after,
                                 "submit_attempts": attempts})
            except Exception as error:
                outcomes.append({"case": name, "passed": False, "error": str(error), "state": read()})
            finally:
                app.terminate()
                app.wait(timeout=5)
                time.sleep(0.2)
        (root / "results.json").write_text(json.dumps(outcomes, ensure_ascii=False, indent=2))
        print(json.dumps({"root": str(root), "summary": [{"case": row["case"], "passed": row["passed"],
              "error": row.get("error")} for row in outcomes]}, ensure_ascii=False))
        return 0 if all(row["passed"] for row in outcomes) else 1
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
