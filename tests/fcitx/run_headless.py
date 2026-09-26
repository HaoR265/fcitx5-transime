#!/usr/bin/env python3
"""Run synthetic Fcitx tests with explicit isolated paths, never the desktop bus."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


# A bounded transport peer for cancellation, delay, and recovery tests. It is
# generated only inside the isolated run; real-model cases use the real worker.
SYNTHETIC_PUNCTUATION_WORKER = '''import json
from pathlib import Path
import struct
import sys
import time

def read_exact(size):
    data = bytearray()
    while len(data) < size:
        chunk = sys.stdin.buffer.read(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)

while True:
    header = read_exact(4)
    if header is None:
        break
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= 65536:
        raise SystemExit(3)
    body = read_exact(size)
    if body is None:
        raise SystemExit(4)
    request = json.loads(body)
    source = request["source"]
    time.sleep(2.0 if "timeout" in Path(__file__).name and source == "你好" else 0.4)
    translation = {"你好": "Hello.", "世界": "World."}.get(source, "")
    failed = ("fail" in Path(__file__).name and source == "你好") or not translation
    response = {"id": request["id"], "translation": "" if failed else translation,
                "error": "synthetic_backend_failure" if failed else None}
    payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack(">I", len(payload)) + payload)
    sys.stdout.buffer.flush()
'''

# No model and no persistent state: deliberately slow responses expose stale
# paragraph delivery. The mapping is only a transport fixture, never quality data.
SYNTHETIC_PARAGRAPH_WORKER = SYNTHETIC_PUNCTUATION_WORKER.replace(
    'translation = {"你好": "Hello.", "世界": "World."}.get(source, "")',
    'translation = {"你好": "Hello.", "世界": "World.", "你好,": "Old paragraph,", '
    '"你好,世界.": "New paragraph.", "你好,世界": "Hello, world."}.get(source, "Synthetic paragraph.")')
SYNTHETIC_PARAGRAPH_WORKER = SYNTHETIC_PARAGRAPH_WORKER.replace(
    '    source = request["source"]',
    '    source = request["source"]\n'
    '    with Path(__file__).with_name("paragraph-requests.tsv").open("a", encoding="utf-8") as log:\n'
    '        log.write(source + "\\t" + json.dumps(request.get("history", []), ensure_ascii=False) + "\\n")')
SYNTHETIC_PARAGRAPH_WORKER = SYNTHETIC_PARAGRAPH_WORKER.replace(
    '    failed =',
    '    if source == "中国":\n'
    '        history = request.get("history", [])\n'
    '        paired = bool(history) and history[-1].get("source") == "你好,世界." and history[-1].get("committed") == "New paragraph."\n'
    '        translation = "History source retained." if paired else "History source missing."\n'
    '    failed =')

# The deliberately artificial mapping cannot be mistaken for a bundled term.
# This helper exercises the production importer/remover inside the private run.
SYNTHETIC_DICTIONARY_HELPER = '''import json
from pathlib import Path
import sys
sys.path.insert(0, @PROJECT@)
from settings.dictionaries import DictionaryManager
root = Path(__file__).resolve().parent
if not (root / ".transime-headless-test-root").is_file():
    raise RuntimeError("not an isolated test root")
manager = DictionaryManager(root / "data/fcitx5")
if sys.argv[1] == "import":
    source = root / "synthetic-pack.tsv"
    source.write_text("word\\tpinyin\\tscore\\tenglish\\n麒麟密钥探针\\tka'ka'ka'ka'ka'ka\\t100000000\\tsynthetic fixture only\\n", encoding="utf-8")
    result = manager.import_file(source, "synthetic candidate-loading fixture")
    (root / "synthetic-pack.json").write_text(json.dumps(result), encoding="utf-8")
    print("SYNTHETIC_PACK_IMPORT", result["id"], result["count"], flush=True)
elif sys.argv[1] == "remove":
    record = json.loads((root / "synthetic-pack.json").read_text())
    result = manager.remove_pack(record["id"])
    print("SYNTHETIC_PACK_REMOVE", result["id"], result["removed"], flush=True)
else:
    raise RuntimeError("unsupported fixture action")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--addon-dir", type=Path, action="append", required=True)
    parser.add_argument("--data-dir", type=Path, action="append", required=True)
    parser.add_argument("--library-dir", type=Path, action="append", default=[])
    parser.add_argument("--with-transime", action="store_true")
    parser.add_argument("--candidate-rows", action="store_true",
                        help="test native per-candidate comments (60 second deadline)")
    parser.add_argument("--mixed", action="store_true",
                        help="test real modifier events and continuous Chinese-English mixed composition (60 seconds)")
    parser.add_argument("--paragraph", action="store_true",
                        help="test continuous native composition and explicit whole-draft confirmation (90 seconds)")
    parser.add_argument("--dictionary-pack", action="store_true",
                        help="test production importer and real Pinyin dictionary reload/removal with a synthetic pack")
    parser.add_argument("--punctuation-diagnostic", action="store_true",
                        help="compare synthetic punctuation behavior with the plugin off/on")
    parser.add_argument("--punctuation-commit", action="store_true",
                        help="test native punctuation commits plus synthetic delayed/failing transport (60 seconds)")
    parser.add_argument("--punctuation-history-expiry", action="store_true",
                        help="test short history TTL during punctuation wait; requires fixture-enabled addon")
    parser.add_argument("--without-bridge", action="store_true",
                        help="load enabled TransIME with an unpatched real pinyin addon")
    parser.add_argument("--native-python", type=Path)
    parser.add_argument("--native-script", type=Path)
    parser.add_argument("--native-model", type=Path)
    args = parser.parse_args()
    if sum((args.candidate_rows, args.punctuation_diagnostic, args.punctuation_commit,
            args.punctuation_history_expiry, args.paragraph, args.mixed, args.dictionary_pack)) > 1:
        parser.error("candidate rows, paragraph and punctuation modes must run separately")
    if (args.candidate_rows or args.punctuation_diagnostic or args.punctuation_commit or
            args.punctuation_history_expiry or args.paragraph or args.mixed) and (args.without_bridge or not all(
            (args.native_python, args.native_script, args.native_model))):
        parser.error("row/paragraph/diagnostic modes require the patched bridge and all three native worker paths")
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    (run / ".transime-headless-test-root").touch()
    if args.dictionary_pack:
        (run / "dictionary-fixture.py").write_text(SYNTHETIC_DICTIONARY_HELPER.replace(
            "@PROJECT@", repr(str(Path(__file__).resolve().parents[2]))), encoding="utf-8")
    if args.punctuation_commit or args.punctuation_history_expiry:
        for kind in ("delay", "fail", "timeout"):
            (run / f"synthetic-{kind}-worker.py").write_text(SYNTHETIC_PUNCTUATION_WORKER, encoding="utf-8")
    if args.paragraph:
        (run / "synthetic-paragraph-worker.py").write_text(SYNTHETIC_PARAGRAPH_WORKER, encoding="utf-8")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("FCITX_") or key in {
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
            "DBUS_SYSTEM_BUS_ADDRESS", "XMODIFIERS", "SKIP_FCITX_PATH",
            "SKIP_FCITX_USER_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
        }:
            env.pop(key, None)
    for key, subdir in {
        "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
        "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "runtime",
    }.items():
        path = run / subdir
        path.mkdir(mode=0o700)
        env[key] = str(path)
    # Only system/package data explicitly passed below is visible to Fcitx.
    env["XDG_CONFIG_DIRS"] = str(run / "config")
    env["XDG_DATA_DIRS"] = str(run / "data")
    if args.library_dir:
        env["LD_LIBRARY_PATH"] = ":".join(str(p.resolve()) for p in args.library_dir)
    command = [str(args.binary.resolve()), "--root", str(run)]
    for path in args.addon_dir:
        command += ["--addon-dir", str(path.resolve(strict=True))]
    for path in args.data_dir:
        command += ["--data-dir", str(path.resolve(strict=True))]
    if args.with_transime:
        command += ["--with-transime"]
    if args.candidate_rows:
        command += ["--candidate-rows"]
    if args.paragraph:
        command += ["--paragraph"]
    if args.mixed:
        command += ["--mixed"]
    if args.dictionary_pack:
        command += ["--dictionary-pack"]
    if args.punctuation_diagnostic:
        command += ["--punctuation-diagnostic"]
    if args.punctuation_commit:
        command += ["--punctuation-commit"]
    if args.punctuation_history_expiry:
        command += ["--punctuation-history-expiry"]
    if args.without_bridge:
        command += ["--without-bridge"]
    for option in ("native_python", "native_script", "native_model"):
        value = getattr(args, option)
        if value is not None:
            # Keep a venv Python's symlink path: resolving it to the base
            # interpreter would silently discard its isolated dependencies.
            if not value.exists():
                parser.error(f"missing {option} path")
            command += ["--" + option.replace("_", "-"), str(value.absolute())]
    timeout = 90 if args.paragraph else (60 if args.candidate_rows or args.punctuation_commit or args.mixed else 30)
    try:
        result = subprocess.run(command, env=env, cwd=run, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
        output, returncode = result.stdout, result.returncode
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output += f"\nFAIL: isolated headless test exceeded the {timeout} second deadline\n"
        returncode = 124
    (run / "result.log").write_text(output, encoding="utf-8")
    print(output, end="")
    print(f"Synthetic integration log: {run / 'result.log'}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
