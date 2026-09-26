#!/usr/bin/env python3
"""Stage a versioned native plugin; activation and rollback are explicit actions.

Never replaces system libraries, edits pinyin configuration/dictionaries, starts
Fcitx, or downloads dependencies. Existing user addon/config files are backed up
only by the explicitly invoked activate command. Rollback preserves the bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".transime-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_write(path: Path, value: object) -> None:
    write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def check_library(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"library missing: {path}")
    dynamic = subprocess.check_output(["readelf", "-d", str(path)], text=True)
    if "(RPATH)" in dynamic or "(RUNPATH)" in dynamic:
        raise RuntimeError(f"workspace RPATH forbidden for deployment library: {path}")
    dependencies = subprocess.check_output(["ldd", str(path)], text=True)
    if "not found" in dependencies:
        raise RuntimeError(f"unresolved deployment dependencies: {path}")


def addon_library(text: str, library: Path) -> str:
    if text.count("\nLibrary=") != 1:
        raise RuntimeError("expected exactly one Library entry in addon metadata")
    return re.sub(r"(?m)^Library=.*$", "Library=" + str(library.with_suffix("")), text)


def validate_home(home: Path, workspace: Path) -> None:
    if home.resolve() != Path.home().resolve() and not home.resolve().is_relative_to((workspace / "work").resolve()):
        raise RuntimeError("--home must be current user home or a rehearsal directory within work/")


def bundle_python(executable: Path, destination: Path) -> dict:
    probe = subprocess.check_output([str(executable), "-I", "-c",
        "import json,sys,sysconfig; print(json.dumps({'base':sys.base_prefix,'purelib':sysconfig.get_path('purelib'),'version':sys.version.split()[0]}))"], text=True)
    info = json.loads(probe)
    base, purelib = Path(info["base"]), Path(info["purelib"])
    if not info["version"].startswith("3.12.") or (purelib / "torch").exists():
        raise RuntimeError("expected Python 3.12 inference runtime without conversion-time torch")
    shutil.copytree(base, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target_site = destination / "lib/python3.12/site-packages"
    shutil.copytree(purelib, target_site, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in destination.rglob("*"):
        if not path.is_symlink():
            continue
        link = Path(os.readlink(path))
        if link.is_absolute():
            if not link.is_relative_to(base):
                raise RuntimeError(f"Python bundle has external absolute symlink: {path}")
            mapped = destination / link.relative_to(base)
            path.unlink()
            path.symlink_to(os.path.relpath(mapped, path.parent))
        if not path.resolve().is_relative_to(destination.resolve()) or not path.exists():
            raise RuntimeError(f"Python bundle has escaping or broken symlink: {path}")
    # Check actual interpreter relocation and imports, not only file presence.
    binary = destination / "bin/python3.12"
    result = json.loads(subprocess.check_output([str(binary), "-I", "-B", "-c",
        "import json,sys,ctranslate2,sentencepiece,numpy,sacremoses; print(json.dumps({'prefix':sys.prefix,'path':sys.path,'ctranslate2':ctranslate2.__version__}))"], text=True))
    if Path(result["prefix"]).resolve() != destination.resolve():
        raise RuntimeError("copied Python did not use its private prefix")
    if any(path and not Path(path).resolve().is_relative_to(destination.resolve()) for path in result["path"]):
        raise RuntimeError("copied Python still imports from an external Python directory")
    return {"python_version": info["version"], "ctranslate2_version": result["ctranslate2"],
            "relocated_imports_verified": True, "source_base_prefix": str(base)}


def worker_smoke(release: Path) -> dict:
    request = json.dumps({"id": 1, "source": "你好", "history": [], "context": False}, ensure_ascii=False).encode()
    result = subprocess.run([str(release / "python/bin/python3.12"), "-I", "-B", "-u",
                             str(release / "worker/transime_worker.py"), "--model-dir", str(release / "model"),
                             "--threads", "2", "--context-policy", "lexical"],
                            input=struct.pack(">I", len(request)) + request, capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) < 4:
        raise RuntimeError("copied worker failed its synthetic framed-protocol smoke")
    count = struct.unpack(">I", result.stdout[:4])[0]
    if len(result.stdout) != count + 4:
        raise RuntimeError("copied worker returned an invalid protocol frame")
    response = json.loads(result.stdout[4:])
    if response.get("id") != 1 or response.get("error") is not None or not response.get("translation"):
        raise RuntimeError("copied worker did not produce a translation")
    return {"synthetic_input": "你好", "translation": response["translation"], "returncode": result.returncode}


def stage(args: argparse.Namespace, project: Path, workspace: Path) -> None:
    destination = args.stage_dir.absolute()
    if not destination.resolve().is_relative_to((workspace / "work").resolve()):
        raise RuntimeError("staging must remain in workspace work/")
    if destination.exists():
        raise RuntimeError("staging directory already exists; use a fresh version/directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.version):
        raise RuntimeError("invalid version directory name")
    if args.context and not args.history:
        raise RuntimeError("--context requires explicit --history")
    home = args.home.absolute()
    validate_home(home, workspace)
    install_root = home / ".local/share/fcitx5/transime/releases" / args.version
    # These explicit paths are also recorded for review before activation.
    # Custom FCITX_DATA_HOME/CONFIG_HOME must be supplied by the caller.
    data_home = args.fcitx_data_home.absolute() if args.fcitx_data_home else home / ".local/share/fcitx5"
    config_home = args.fcitx_config_home.absolute() if args.fcitx_config_home else home / ".config/fcitx5"
    for path in (data_home, config_home):
        if not path.resolve().is_relative_to(home.resolve()):
            raise RuntimeError("activation data/config roots must be inside selected home")
    for library in (args.plugin, args.bridge):
        check_library(library)
    cache = args.build_cache.read_text()
    for setting in ("TRANSIME_ENABLE_TEST_FIXTURE:BOOL=OFF", "TRANSIME_FCITX_VERSION:STRING=5.1.21"):
        if setting not in cache.splitlines():
            raise RuntimeError("production build cache is missing " + setting)
    python = args.python_executable.absolute()
    model = args.model_dir.absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("worker Python executable missing")
    if not (model / "model.bin").is_file():
        raise RuntimeError("converted CTranslate2 model.bin missing")
    destination.mkdir(parents=True)
    release = destination / "release"
    (release / "lib").mkdir(parents=True)
    (release / "worker").mkdir()
    (release / "tools").mkdir()
    # Keep rollback/remove available after deleting the development workspace.
    # These actions use only the standard library and installed receipt.
    shutil.copy2(Path(__file__), release / "tools/package_plugin.py")
    shutil.copy2(project / "scripts/restart_kde.py", release / "tools/restart_kde.py")
    shutil.copy2(args.plugin, release / "lib/libtransime.so")
    shutil.copy2(args.bridge, release / "lib/libpinyin.so")
    for source in sorted((project / "worker").glob("*.py")):
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("worker sources must be regular Python files")
        shutil.copy2(source, release / "worker" / source.name)
    # Reconstruct a standalone Python tree from its complete base interpreter
    # plus inference-only site-packages. Do not copy the venv's pyvenv.cfg.
    python_info = bundle_python(python, release / "python")
    private_python = install_root / "python/bin/python3.12"
    # Models are copied from explicitly provided, already local assets.
    shutil.copytree(model, release / "model", symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
    private_model = install_root / "model"
    licenses = release / "licenses"
    licenses.mkdir()
    upstream = workspace / "work/host-sdk/src/fcitx5-chinese-addons-5.1.12"
    shutil.copy2(upstream / "COPYING", licenses / "chinese-addons-COPYING")
    shutil.copytree(upstream / "LICENSES", licenses / "chinese-addons-LICENSES")
    shutil.copy2(project / "bridge/pinyin-5.1.12-transime-snapshot-v1.patch", licenses / "pinyin-bridge.patch")
    model_card = workspace / "work/nmt/source-model/README.md"
    if model_card.is_file():
        shutil.copy2(model_card, licenses / "opus-mt-zh-en-MODEL_CARD.md")
    json_write(licenses / "sources.json", {
        "pinyin": "https://github.com/fcitx/fcitx5-chinese-addons/tree/5.1.12",
        "model": "https://huggingface.co/Helsinki-NLP/opus-mt-zh-en",
        "model_license": "CC-BY-4.0 (model card)",
        "model_license_url": "https://creativecommons.org/licenses/by/4.0/",
        "model_author": "Language Technology Research Group at the University of Helsinki",
        "model_changes": "Converted from Marian/Transformers to CTranslate2 and quantized to INT8 for local CPU inference",
        "python_and_wheel_licenses": "included with the copied Python tree and site-packages *.dist-info directories"})
    smoke = worker_smoke(release)
    pinyin_addon = addon_library(Path("/usr/share/fcitx5/addon/pinyin.conf").read_text(), install_root / "lib/libpinyin.so")
    pinyin_addon = re.sub(r"(?m)^(\d+=)core:[^\n]+$", r"\g<1>core:5.1.21", pinyin_addon)
    transime_addon = addon_library(args.addon_metadata.read_text(), install_root / "lib/libtransime.so")
    config = (f"Enabled={'True' if args.enable else 'False'}\n"
              f"EnableHistory={'True' if args.history else 'False'}\nContextAware={'True' if args.context else 'False'}\n"
              "CandidateTranslations=True\nContinuousComposition=True\nFastContext=True\nAutoCommitOnPunctuation=False\n"
              f"PythonExecutable={private_python}\nWorkerScript={install_root / 'worker/transime_worker.py'}\n"
              f"ModelDirectory={private_model}\nWorkerThreads=2\nDebounceMilliseconds=60\n"
              "RequestTimeoutMilliseconds=2000\nColdStartTimeoutMilliseconds=10000\nWorkerIdleSeconds=120\n"
              "\n[TranslateKey]\n0=Control+Return\n\n[ClearHistoryKey]\n0=Control+Alt+BackSpace\n")
    activation = [("activation/pinyin.conf", data_home / "addon/pinyin.conf", pinyin_addon),
                  ("activation/transime.conf", data_home / "addon/transime.conf", transime_addon),
                  ("activation/transime-user.conf", config_home / "conf/transime.conf", config)]
    activation_records = []
    for name, target, text in activation:
        path = destination / name
        write_atomic(path, text.encode())
        activation_records.append({"source": name, "destination": str(target), "sha256": digest(path)})
    files = [{"path": str(path.relative_to(release)), "sha256": digest(path), "bytes": path.stat().st_size,
              "symlink": os.readlink(path) if path.is_symlink() else None}
             for path in sorted(release.rglob("*")) if path.is_file()]
    manifest = {"schema": 1, "version": args.version, "home": str(home), "install_root": str(install_root),
                "fcitx_data_home": str(data_home), "fcitx_config_home": str(config_home),
                "files": files, "activation": activation_records, "python": python_info,
                "private_python": str(private_python), "worker_smoke": smoke,
                "fcitx_version": "5.1.21", "libime_version": "1.1.15", "pinyin_version": "5.1.12",
                "history_enabled": args.history, "context_reranking_enabled": args.context,
                "desktop_started": False, "status": "staged_not_activated"}
    json_write(destination / "manifest.json", manifest)
    print(json.dumps({"stage": str(destination), "manifest": str(destination / 'manifest.json'),
                      "install_root": str(install_root), "status": "staged_not_activated"}, indent=2))


def verify_stage(stage_dir: Path, workspace: Path) -> dict:
    if not stage_dir.resolve().is_relative_to((workspace / "work").resolve()):
        raise RuntimeError("package must be staged in workspace work/")
    manifest = json.loads((stage_dir / "manifest.json").read_text())
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", manifest["version"]):
        raise RuntimeError("invalid package version")
    validate_home(Path(manifest["home"]), workspace)
    home = Path(manifest["home"])
    expected = home / ".local/share/fcitx5/transime/releases" / manifest["version"]
    if Path(manifest["install_root"]) != expected:
        raise RuntimeError("invalid private installation path")
    data_home, config_home = Path(manifest["fcitx_data_home"]), Path(manifest["fcitx_config_home"])
    if any(not path.resolve().is_relative_to(home.resolve()) for path in (data_home, config_home)):
        raise RuntimeError("activation roots outside selected home")
    expected_targets = {str(data_home / "addon/pinyin.conf"), str(data_home / "addon/transime.conf"),
                        str(config_home / "conf/transime.conf")}
    if len(manifest["activation"]) != 3 or {item["destination"] for item in manifest["activation"]} != expected_targets:
        raise RuntimeError("activation must contain exactly the designed two addon files and one module config")
    for record in manifest["files"]:
        path = stage_dir / "release" / record["path"]
        if not path.resolve().is_relative_to((stage_dir / "release").resolve()) or digest(path) != record["sha256"]:
            raise RuntimeError("staged release file changed")
        if (os.readlink(path) if path.is_symlink() else None) != record["symlink"]:
            raise RuntimeError("staged release symlink changed")
    for record in manifest["activation"]:
        path = stage_dir / record["source"]
        target = Path(record["destination"])
        if not path.resolve().is_relative_to(stage_dir.resolve()) or digest(path) != record["sha256"]:
            raise RuntimeError("staged activation file changed")
        if not target.parent.resolve().is_relative_to(home.resolve()) or target.name not in {"pinyin.conf", "transime.conf"}:
            raise RuntimeError("invalid activation file destination")
    return manifest


def activate(args: argparse.Namespace, workspace: Path) -> None:
    stage_dir = args.stage_dir.absolute()
    manifest = verify_stage(stage_dir, workspace)
    import prepare_host_sdk
    versions = {name: subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", name], text=True).strip()
                for name in prepare_host_sdk.HOST_VERSIONS}
    if versions != prepare_host_sdk.HOST_VERSIONS:
        raise RuntimeError("host disk versions changed since this package was built")
    install_root = Path(manifest["install_root"])
    if install_root.exists():
        raise RuntimeError("private version already exists; preserve it and choose a new version")
    # Stage first, then back up the exact three entries. No pinyin user config,
    # inputmethod profile, history, custom phrase, or dictionary is accessed.
    shutil.copytree(stage_dir / "release", install_root, symlinks=True)
    json_write(install_root / "install-manifest.json", manifest)
    backup = install_root / ".activation-backup"
    backup.mkdir(mode=0o700)
    receipt = {"schema": 1, "home": manifest["home"], "install_root": str(install_root),
               "manifest_sha256": digest(install_root / "install-manifest.json"),
               "status": "activation_in_progress", "entries": []}
    receipt_path = install_root / "activation-receipt.json"
    for index, record in enumerate(manifest["activation"]):
        target = Path(record["destination"])
        entry = {"destination": str(target), "installed_sha256": record["sha256"], "previous": "absent"}
        if target.is_symlink():
            entry.update(previous="symlink", link=os.readlink(target))
        elif target.exists():
            if not target.is_file():
                raise RuntimeError(f"activation target is not a file: {target}")
            saved = backup / str(index)
            shutil.copy2(target, saved)
            os.chmod(saved, 0o600)
            entry.update(previous="file", backup=str(saved), backup_sha256=digest(saved), mode=target.stat().st_mode & 0o777)
        receipt["entries"].append(entry)
    json_write(receipt_path, receipt)
    written = []
    try:
        for record, entry in zip(manifest["activation"], receipt["entries"]):
            write_atomic(Path(record["destination"]), (stage_dir / record["source"]).read_bytes())
            written.append(entry)
    except OSError:
        for entry in reversed(written):
            restore_entry(entry)
        receipt["status"] = "activation_failed_originals_restored"
        json_write(receipt_path, receipt)
        raise
    receipt["status"] = "activated_restart_not_performed"
    json_write(receipt_path, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path)}, indent=2))


def restore_entry(entry: dict) -> None:
    target = Path(entry["destination"])
    if entry["previous"] == "file":
        write_atomic(target, Path(entry["backup"]).read_bytes(), entry["mode"])
    elif entry["previous"] == "symlink":
        target.unlink()
        target.symlink_to(entry["link"])
    else:
        target.unlink()


def rollback(args: argparse.Namespace, workspace: Path) -> None:
    receipt_path = args.receipt.absolute()
    receipt = json.loads(receipt_path.read_text())
    home = Path(receipt["home"])
    validate_home(home, workspace)
    install_root = Path(receipt["install_root"])
    if receipt_path.parent != install_root or not install_root.resolve().is_relative_to((home / ".local/share/fcitx5/transime/releases").resolve()):
        raise RuntimeError("receipt is outside a private version directory")
    if receipt["status"] == "rolled_back_restart_not_performed":
        print("Already rolled back; nothing changed.")
        return
    for entry in receipt["entries"]:
        target = Path(entry["destination"])
        if not target.parent.resolve().is_relative_to(home.resolve()):
            raise RuntimeError("rollback target outside selected home")
        if target.is_symlink() or not target.is_file() or digest(target) != entry["installed_sha256"]:
            raise RuntimeError(f"installed entry changed since activation; preserved for review: {target}")
        if entry["previous"] == "file" and digest(Path(entry["backup"])) != entry["backup_sha256"]:
            raise RuntimeError("backup hash mismatch; preserved")
    for entry in reversed(receipt["entries"]):
        restore_entry(entry)
    receipt["status"] = "rolled_back_restart_not_performed"
    json_write(receipt_path, receipt)
    print(json.dumps({"status": receipt["status"], "private_bundle_preserved": str(install_root)}, indent=2))


def remove(args: argparse.Namespace, workspace: Path) -> None:
    receipt_path = args.receipt.absolute()
    receipt = json.loads(receipt_path.read_text())
    home = Path(receipt["home"])
    validate_home(home, workspace)
    root = Path(receipt["install_root"])
    releases = home / ".local/share/fcitx5/transime/releases"
    if root.is_symlink() or receipt_path.parent != root or root.parent.resolve() != releases.resolve():
        raise RuntimeError("remove requires one exact private release directory")
    if receipt["status"] != "rolled_back_restart_not_performed":
        raise RuntimeError("run rollback before removing a release")
    # Check only process identity and references, never report command contents.
    references = {str(root).encode(), str(root.resolve()).encode()}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            name = (proc / "comm").read_text().strip()
            if name == "fcitx5":
                content = (proc / "maps").read_bytes()
            elif name.startswith("python"):
                content = (proc / "cmdline").read_bytes()
            else:
                continue
            if any(reference in content for reference in references):
                raise RuntimeError(f"release is still used by process {proc.name}; stop/restart that process first")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            raise RuntimeError(f"cannot inspect candidate process {proc.name}; release preserved") from error
    manifest_path = root / "install-manifest.json"
    if digest(manifest_path) != receipt["manifest_sha256"]:
        raise RuntimeError("installed manifest changed; release preserved")
    manifest = json.loads(manifest_path.read_text())
    if Path(manifest["install_root"]) != root:
        raise RuntimeError("installed manifest does not match this release")
    allowed = {"install-manifest.json", "activation-receipt.json"}
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or digest(path) != item["sha256"]:
            raise RuntimeError(f"release file changed or missing; preserved: {item['path']}")
        if (os.readlink(path) if path.is_symlink() else None) != item["symlink"]:
            raise RuntimeError("release symlink changed; preserved")
        allowed.add(item["path"])
    for entry in receipt["entries"]:
        if entry["previous"] == "file":
            backup = Path(entry["backup"])
            if not backup.resolve().is_relative_to((root / ".activation-backup").resolve()) or digest(backup) != entry["backup_sha256"]:
                raise RuntimeError("activation backup changed; release preserved")
            allowed.add(str(backup.relative_to(root)))
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != allowed:
        raise RuntimeError("release has untracked or missing files; preserved for review")
    shutil.rmtree(root)
    print(json.dumps({"status": "release_removed", "path": str(root), "system_files_changed": False}, indent=2))


def main() -> int:
    project = Path(__file__).absolute().parents[1]
    workspace = project.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    staging = sub.add_parser("stage")
    staging.add_argument("--stage-dir", type=Path, required=True)
    staging.add_argument("--version", required=True)
    staging.add_argument("--plugin", type=Path, required=True)
    staging.add_argument("--bridge", type=Path, required=True)
    staging.add_argument("--build-cache", type=Path, required=True)
    staging.add_argument("--addon-metadata", type=Path, required=True)
    staging.add_argument("--python-executable", type=Path, required=True)
    staging.add_argument("--model-dir", type=Path, required=True)
    staging.add_argument("--home", type=Path, default=Path.home())
    staging.add_argument("--fcitx-data-home", type=Path)
    staging.add_argument("--fcitx-config-home", type=Path)
    staging.add_argument("--enable", action="store_true")
    staging.add_argument("--history", action="store_true")
    staging.add_argument("--context", action="store_true")
    activating = sub.add_parser("activate")
    activating.add_argument("--stage-dir", type=Path, required=True)
    rolling = sub.add_parser("rollback")
    rolling.add_argument("--receipt", type=Path, required=True)
    removing = sub.add_parser("remove")
    removing.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "stage":
        stage(args, project, workspace)
    elif args.action == "activate":
        activate(args, workspace)
    elif args.action == "rollback":
        rollback(args, workspace)
    else:
        remove(args, workspace)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
