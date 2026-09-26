#!/usr/bin/env python3
"""Real Qt/GTK input in a fresh VM-only X server and D-Bus session."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--toolkit", choices=("qt", "gtk"), default="qt")
    parser.add_argument("--cases", help="Comma-separated cases for targeted debugging (unknown names rejected)")
    parser.add_argument("--qt-plugin-path", type=Path, help="Private Qt platform input plugin build (VM-only)")
    parser.add_argument("--qt-panel-draft", action="store_true", help="Enable private Qt panel-draft compatibility fix")
    args = parser.parse_args()
    if args.qt_panel_draft and not args.qt_plugin_path:
        parser.error("--qt-panel-draft requires --qt-plugin-path")
    if subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True).returncode:
        raise SystemExit("Refusing desktop tests outside a VM")
    if not args.inside:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FCITX_", "QT_")) and k not in (
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS", "LD_LIBRARY_PATH", "LD_PRELOAD")}
        env["TRANSIME_PRIVATE_X11"] = "1"
        return subprocess.call(["xvfb-run", "-a", "-s", "-screen 0 1280x800x24", "dbus-run-session",
            sys.executable, __file__, *sys.argv[1:], "--inside"], env=env)
    if os.environ.get("TRANSIME_PRIVATE_X11") != "1":
        raise SystemExit("Use the wrapper to create a private display")
    args.work_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=args.toolkit + "-", dir=args.work_root))
    env = os.environ.copy()
    for key, directory in {"HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
                           "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "runtime"}.items():
        path = root / directory
        path.mkdir(mode=0o700)
        env[key] = str(path)
    build = args.build_root
    config = root / "config/fcitx5"
    (config / "conf").mkdir(parents=True)
    (config / "profile").write_text("[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=pinyin\n\n[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n[Groups/0/Items/1]\nName=pinyin\nLayout=\n\n[GroupOrder]\n0=Default\n")
    project = Path(__file__).resolve().parents[2]
    (config / "conf/transime.conf").write_text(
        "Enabled=True\nContinuousComposition=True\nShowModeHints=False\nEnableHistory=False\nContextAware=False\n"
        f"PythonExecutable={args.model_root}/baseline-python/bin/python\nWorkerScript={project}/worker/transime_worker.py\n"
        f"ModelDirectory={args.model_root}/baseline-model\nRequestTimeoutMilliseconds=5000\nColdStartTimeoutMilliseconds=15000\n"
        "\n[TranslateKey]\n0=Control+Return\n\n[ToggleKey]\n0=Control+Alt+u\n")
    env.update({"FCITX_CONFIG_HOME": str(config), "FCITX_DATA_HOME": str(root / "data/fcitx5"),
        "FCITX_CONFIG_DIRS": str(config), "FCITX_DATA_DIRS": str(build / "staging") + ":/usr/share/fcitx5",
        "FCITX_ADDON_DIRS": f"{build}/bridge/bin:{build}/plugin/plugin:/usr/lib/x86_64-linux-gnu/fcitx5",
        "LD_LIBRARY_PATH": f"{build}/sdk/prefix/usr/lib/x86_64-linux-gnu:{build}/sdk/deps/usr/lib/x86_64-linux-gnu",
        "XDG_CONFIG_DIRS": str(root / "config"), "XDG_DATA_DIRS": "/usr/share", "QT_IM_MODULE": "fcitx",
        "QT_QPA_PLATFORM": "xcb", "GTK_IM_MODULE": "fcitx", "XMODIFIERS": "@im=fcitx", "SKIP_FCITX_PATH": "1"})
    if args.qt_plugin_path:
        env["QT_PLUGIN_PATH"] = str(args.qt_plugin_path.resolve())
    env.pop("TRANSIME_QT_PRESERVE_PANEL_DRAFT", None)
    if args.qt_panel_draft:
        env["TRANSIME_QT_PRESERVE_PANEL_DRAFT"] = "1"
    processes = []
    handles = []
    outcomes = []

    def launch(argv, log):
        handle = (root / log).open("w")
        handles.append(handle)
        child = subprocess.Popen(argv, env=env, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(child)
        return child

    def command(*argv):
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10, check=True).stdout.strip()

    def wait_for(fn, seconds=6):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("condition timed out")

    def visible_window(marker):
        found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "^" + marker + "$"],
                               env=env, capture_output=True, text=True, timeout=5)
        return found.stdout.splitlines()[0] if found.returncode == 0 and found.stdout.strip() else None

    try:
        command("setxkbmap", "us")
        (root / "keymap.txt").write_text(command("xmodmap", "-pke"))
        command("dbus-update-activation-environment", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DISPLAY")
        launch(["dbus-monitor", "--session", "destination='org.fcitx.Fcitx5'"], "bus.log")
        launch(["xfwm4", "--compositor=off"], "wm.log")
        launch(["fcitx5", "--disable=all", "--enable=dbus,dbusfrontend,xcb,xim,classicui,keyboard,pinyin,pinyinhelper,punctuation,transime"], "fcitx.log")
        time.sleep(1)
        cases = [("chinese", "nihao", "space", "你好"), ("raw", "nihao", "Return", "nihao"),
                 ("translation", "nihao", "ctrl+Return", "Hello."), ("cancel", "nihao", "Escape", ""),
                 ("punctuation", "nihao,", "space", "你好,"), ("english", "API", "space", "API"),
                 ("mixed", "nihao1API.", "space", "你好API."), ("edit", "nihaox", "space", "你好"),
                 ("rapid_chinese", "nihao", "space", "你好"),
                 ("cancel_retype", "nihao", "space", "API"),
                 ("focus_return", "nihao", "space", "你好"),
                 ("cancel_focus", "nihao", "space", " "),
                 ("disable_draft", "nihao", "space", "你好"),
                 ("custom", "nihao", "ctrl+alt+j", "你好"),
                 ("custom_raw", "nihao", "ctrl+alt+k", "nihao"),
                 ("custom_f6", "nihao", "F6", "你好")]
        if args.toolkit == "qt":
            index = next(i for i, case in enumerate(cases) if case[0] == "custom")
            cases[index:index] = [("explicit_reset", "nihao", "space", "API"),
                                  ("field_switch", "nihao", "space", " "),
                                  ("sensitive_switch", "nihao", "space", " "),
                                  ("focus_new_field", "nihao", "space", "")]
            cases.append(("sensitive", "synthetic-secret-01", "", "synthetic-secret-01"))
        if args.cases:
            selected = set(args.cases.split(","))
            unknown = selected - {case[0] for case in cases}
            if unknown:
                raise ValueError("Unknown cases: " + ", ".join(sorted(unknown)))
            cases = [case for case in cases if case[0] in selected]
        custom_loaded = False
        for name, source, submit, expected in cases:
            started = time.monotonic()
            other = None
            state = root / (name + ".json")
            marker = "TransIME-Test-Round2-" + name
            if name in ("custom", "custom_raw") and not custom_loaded:
                with (config / "conf/transime.conf").open("a") as stream:
                    stream.write("\n[ChineseCommitKey]\n0=Control+Alt+j\n\n[RawCommitKey]\n0=Control+Alt+k\n")
                command("gdbus", "call", "--session", "--dest", "org.fcitx.Fcitx5", "--object-path", "/controller",
                        "--method", "org.fcitx.Fcitx.Controller1.ReloadAddonConfig", "transime")
                time.sleep(.3)
                custom_loaded = True
            if name == "custom_f6":
                config_path = config / "conf/transime.conf"
                contents = config_path.read_text()
                if custom_loaded:
                    contents = contents.replace("0=Control+Alt+j", "0=F6")
                else:
                    contents += "\n[ChineseCommitKey]\n0=F6\n"
                config_path.write_text(contents)
                command("gdbus", "call", "--session", "--dest", "org.fcitx.Fcitx5", "--object-path", "/controller",
                        "--method", "org.fcitx.Fcitx.Controller1.ReloadAddonConfig", "transime")
            extra = ["--sensitive"] if name == "sensitive" else []
            if name in ("field_switch", "sensitive_switch", "focus_new_field"):
                extra += ["--two-fields"]
                if name == "sensitive_switch":
                    extra += ["--second-sensitive"]
                if name == "focus_new_field":
                    extra += ["--switch-field-on-deactivate"]
            app = launch([sys.executable, str(project / ("tests/desktop/" + args.toolkit + "_fixture.py")), "--execute", "--state", str(state), "--marker", marker] + extra, name + ".log")
            def read_state():
                try:
                    return json.loads(state.read_text())
                except (OSError, ValueError):
                    return {}
            try:
                wait_for(lambda: read_state().get("pid") == app.pid)
                if args.toolkit == "qt" and args.qt_plugin_path:
                    mapped = [line for line in Path(f"/proc/{app.pid}/maps").read_text().splitlines()
                              if "platforminputcontexts/" in line]
                    (root / (name + "-plugin-maps.txt")).write_text("\n".join(mapped) + "\n")
                    assert any(str(args.qt_plugin_path.resolve()) in line for line in mapped), "private Qt plugin not mapped"
                wid = wait_for(lambda: visible_window(marker))
                command("xdotool", "windowactivate", "--sync", wid)
                wait_for(lambda: read_state().get("active"))
                if name == "sensitive":
                    command("xdotool", "type", "--clearmodifiers", "--delay", "90", source)
                    wait_for(lambda: read_state().get("text") == expected)
                    result = read_state()
                    assert result.get("synthetic_sensitive") and result["ime_commits"] == [], result
                    outcomes.append({"case": name, "passed": True, "after": result})
                    continue
                wait_for(lambda: command("fcitx5-remote") in ("1", "2"))
                command("fcitx5-remote", "-s", "pinyin")
                command("fcitx5-remote", "-o")
                wait_for(lambda: command("fcitx5-remote") == "2" and command("fcitx5-remote", "-n") == "pinyin")
                (root / (name + "-engine.txt")).write_text(command("fcitx5-remote", "-n") + "\n" + command("fcitx5-remote"))
                time.sleep(.2)
                if command("xdotool", "getactivewindow") != wid:
                    raise AssertionError("test window lost focus")
                command("xdotool", "type", "--clearmodifiers", "--delay", "90", source)
                if name == "edit":
                    command("xdotool", "key", "BackSpace")
                if name == "custom":
                    command("xdotool", "key", "Return")
                if name == "cancel_retype":
                    command("xdotool", "key", "Escape")
                    command("xdotool", "type", "--clearmodifiers", "--delay", "30", "API")
                if name == "disable_draft":
                    command("xdotool", "key", "--clearmodifiers", "ctrl+alt+u")
                if name == "explicit_reset":
                    command("xdotool", "key", "--clearmodifiers", "ctrl+alt+r")
                    command("xdotool", "type", "--clearmodifiers", "API")
                if name in ("field_switch", "sensitive_switch"):
                    command("xdotool", "mousemove", *map(str, read_state()["second_center"]), "click", "1")
                    wait_for(lambda: read_state().get("focused_field") == "second")
                    assert read_state()["secondary_text"] == "", "draft leaked to second field"
                    command("xdotool", "type", "--clearmodifiers", "API")
                    if name == "field_switch":
                        command("xdotool", "key", "space")
                    wait_for(lambda: read_state()["secondary_text"] == "API")
                    if name == "sensitive_switch":
                        assert read_state()["secondary_commits"] == [], "password used IME commits"
                    command("xdotool", "mousemove", *map(str, read_state()["first_center"]), "click", "1")
                    wait_for(lambda: read_state().get("focused_field") == "first")
                if name in ("focus_return", "cancel_focus", "focus_new_field"):
                    if name == "cancel_focus":
                        command("xdotool", "key", "Escape")
                    other_state = root / (name + "-other.json")
                    other_marker = marker + "-Other"
                    other = launch([sys.executable, str(project / ("tests/desktop/" + args.toolkit + "_fixture.py")),
                                    "--execute", "--state", str(other_state), "--marker", other_marker], name + "-other.log")
                    wait_for(lambda: other_state.exists())
                    other_wid = wait_for(lambda: visible_window(other_marker))
                    command("xdotool", "windowactivate", "--sync", other_wid)
                    wait_for(lambda: json.loads(other_state.read_text()).get("active"))
                    time.sleep(1)
                    assert json.loads(other_state.read_text())["text"] == "", "draft leaked to another window"
                    command("xdotool", "windowactivate", "--sync", wid)
                    if name == "focus_new_field":
                        wait_for(lambda: read_state().get("window_active") and read_state().get("focused_field") == "second")
                        assert read_state()["secondary_text"] == "", "draft leaked on window return"
                        command("xdotool", "type", "--clearmodifiers", "API")
                    else:
                        wait_for(lambda: read_state().get("active"))
                    other.terminate()
                    other.wait(timeout=5)
                if name != "rapid_chinese":
                    time.sleep(1.8)
                before = read_state()
                if before.get("text") != "":
                    raise AssertionError("draft committed early: " + repr(before))
                if name == "custom_f6":
                    from x11_keys import function_key
                    function_key(submit, int(wid))
                else:
                    command("xdotool", "key", "--clearmodifiers", submit)
                if name == "focus_new_field":
                    wait_for(lambda: read_state().get("secondary_text") == "API", 15)
                if expected:
                    wait_for(lambda: read_state().get("text") == expected, 15)
                else:
                    time.sleep(.3)
                result = read_state()
                assert result["text"] == expected, result
                if args.toolkit == "qt":
                    expected_commits = [] if name in ("field_switch", "sensitive_switch", "cancel_focus") else ([expected] if expected else [])
                    assert result["ime_commits"] == expected_commits, result
                else:
                    assert "".join(event["text"] for event in result["buffer_insert_events"]) == expected, result
                outcomes.append({"case": name, "passed": True, "before": before, "after": result,
                                 "elapsed_seconds": round(time.monotonic() - started, 3)})
            except Exception as exc:
                outcomes.append({"case": name, "passed": False, "error": str(exc), "state": read_state()})
            finally:
                if other is not None and other.poll() is None:
                    other.terminate()
                    other.wait(timeout=5)
                if name == "disable_draft":
                    command("xdotool", "key", "--clearmodifiers", "ctrl+alt+u")
                app.terminate()
                app.wait(timeout=5)
                time.sleep(.2)
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for handle in handles:
            handle.close()
        (root / "results.json").write_text(json.dumps(outcomes, ensure_ascii=False, indent=2))
        print(json.dumps({"root": str(root), "results": outcomes}, ensure_ascii=False))
    return 0 if len(outcomes) == len(cases) and all(o["passed"] for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
