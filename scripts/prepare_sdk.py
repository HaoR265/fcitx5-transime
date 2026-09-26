#!/usr/bin/env python3
"""Prepare a workspace-only Fcitx 5.1.19 SDK with LibIME 1.1.14 headers.

Downloads require --download. This script never runs apt install/update, uses
sudo, changes a host configuration, starts fcitx5, or downloads an NMT model.
The LibIME runtime is a hash-locked snapshot of the original host libraries;
several base development libraries remain host-provided.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


SOURCES = {
    "fcitx5-5.1.19.tar.gz": (
        "https://codeload.github.com/fcitx/fcitx5/tar.gz/refs/tags/5.1.19",
        "a6e4d99a82298845df9f8dc5036c1aaed258c2d3f5bd215b8f91c75410167ff0"),
    "libime-1.1.14.tar.gz": (
        "https://codeload.github.com/fcitx/libime/tar.gz/refs/tags/1.1.14",
        "e1bc3a0868333745dc7dd6ec3123355b35008003500d3d54593c69c2a9580e79"),
    "chinese-addons-5.1.12.tar.gz": (
        "https://codeload.github.com/fcitx/fcitx5-chinese-addons/tar.gz/refs/tags/5.1.12",
        "b60de3b84dbb091f1301367ba9d2e8228735bf7a0ff125b738b8363c74b2ff32"),
    "kenlm-4cb443e60b7bf2c0ddf3c745378f76cb59e254e5.tar.gz": (
        "https://codeload.github.com/kpu/kenlm/tar.gz/4cb443e60b7bf2c0ddf3c745378f76cb59e254e5",
        "11df4f929b175f0b3bd26be7a5a83b3c17cdefa83baecab0b6e69f77430184ea"),
    "en_dict-20121020.tar.gz": (
        "https://download.fcitx-im.org/data/en_dict-20121020.tar.gz",
        "c44a5d7847925eea9e4d2d04748d442cd28dd9299a0b572ef7d91eac4f5a6ceb"),
}

# These are download-and-extract dependencies, not host package installations.
PACKAGES = {
    "extra-cmake-modules_6.28.0-1_amd64.deb": "872952d4e62fbaa5f242c70951e46b941c2209c371000ccd377ecb2b81f37806",
    "gettext_1.0-3_amd64.deb": "ff9e01960daa4f8e361492c8d5e19501f9a9bc192ee142a6cfcc8e30f619806c",
    "nlohmann-json3-dev_3.12.0.really.3.12.0.really.3.11.3-3_all.deb": "00e6d1978f21e7b9d3e516575168bc86397fd727aa41e3bd6e8b5cfe4affce79",
    "libboost1.90-dev_1.90.0-6_amd64.deb": "164ee0b1aff360bb4c4a45cd454084823984fd48a6993b45a24ae41892f6fa92",
    "libboost-iostreams1.90-dev_1.90.0-6_amd64.deb": "60e953cf01db2bf68d4b467e3983b7d12cc737e6eea5b02cf2ec039b78b0363a",
    "libboost-random1.90-dev_1.90.0-6_amd64.deb": "afb09634fe752159282e0cc3fa9f7be705a3bbd4ea274848de53df855f365c75",
    "libboost-random1.90.0_1.90.0-6_amd64.deb": "ba059f4a995cc1837a10877f3f36dfca159657a55410ee8d868878ebf3cba493",
    "libboost-regex1.90-dev_1.90.0-6_amd64.deb": "8db75f9856a6b5cbe8efdd6c1c055c4a5b2cb0fb3d9bdbee8a7bf8b567afb745",
    "libboost-regex1.90.0_1.90.0-6_amd64.deb": "bd9cc78548418f7bddc08b2b3f7f144336e3a07dfc5dfb2119f512318d5d406b",
}

RUNTIME_SNAPSHOTS = {
    "libIMECore.so.1.1.14": "cfb15a80976a5c64e8d11dffd930fcb2174b7c6b65577b271fe30d12bff69cac",
    "libIMEPinyin.so.1.1.14": "45876b2550874362803bc5c1a8156cfcdb9a1955411096a4d4bf357b2a503f30",
    "libIMETable.so.1.1.14": "b0757927f86037ad9ccf6c3139cdcf784443f39af42b05bf72c4f14a81852310",
}

SHARED_DICTIONARIES = {
    "sc.dict": "e82c66be594be96ab19b63dc007e49b625a28f4ddd59adb508cf931decf7a281",
    "extb.dict": "4bb853510d191791128aeda9d2bc8697ec538a7cd6ebf4f487f6f1022bb1ab81",
}


def run(command: list[str], *, cwd: Path, env: dict[str, str], log: Path) -> None:
    print("Running: " + " ".join(command), flush=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log}")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_archive(path: Path, expected: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing cached download: {path}; use --download explicitly")
    if sha256(path) != expected:
        raise RuntimeError(f"download hash mismatch: {path}; existing file was preserved")


def extract_if_absent(archive: Path, destination: Path, target: Path) -> None:
    if target.exists():
        print(f"Reusing source directory without overwriting edits: {target}", flush=True)
        return
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def link_existing(target: Path, link: Path) -> None:
    if not target.is_file():
        raise RuntimeError(f"required host runtime/tool unavailable: {target}")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise RuntimeError(f"refusing to replace existing SDK file: {link}")
    link.symlink_to(target)


def check_host() -> dict[str, str]:
    if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
        raise RuntimeError("this dependency lock was verified on Debian/Kali amd64 only")
    packages = ("libfcitx5core7", "libimecore0", "libimepinyin0", "libimetable0", "fcitx5-pinyin")
    versions = {package: subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", package], text=True).strip() for package in packages}
    # Host Fcitx may change independently. This build uses its own pinned Core;
    # only hash-checked LibIME snapshots enter its prefix. Recording the disk
    # version is not a claim about the already-running desktop process.
    for name in ("cmake", "ninja", "c++", "dpkg-deb", "pkg-config"):
        if not shutil.which(name):
            raise RuntimeError(f"required existing command unavailable: {name}")
    return versions


def main() -> int:
    project = Path(__file__).absolute().parents[1]
    workspace = project.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", type=Path, default=workspace / "work" / "sdk")
    parser.add_argument("--download", action="store_true", help="allow fixed source/package downloads")
    parser.add_argument("--verify-only", action="store_true", help="check cached hashes and SDK paths without building")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    sdk = args.sdk_dir.expanduser().absolute()
    if not sdk.resolve().is_relative_to((workspace / "work").resolve()):
        raise RuntimeError("SDK must remain under this workspace's work/ directory")
    if args.jobs < 1:
        raise RuntimeError("--jobs must be positive")
    host_versions = check_host()
    downloads, deps, prefix, logs = (sdk / part for part in ("downloads", "deps", "prefix", "logs"))
    for folder in (downloads, deps, prefix, logs):
        folder.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(deps / "usr/bin") + os.pathsep + env.get("PATH", "")
    runtime_paths = [str(deps / "usr/lib/x86_64-linux-gnu")]
    if env.get("LD_LIBRARY_PATH"):
        runtime_paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = os.pathsep.join(runtime_paths)
    for filename, (url, digest) in SOURCES.items():
        path = downloads / filename
        if not path.exists() and args.download and not args.verify_only:
            run(["curl", "--fail", "--location", "--retry", "2", "--connect-timeout", "20", "--output", str(path), url],
                cwd=downloads, env=env, log=logs / f"download-{filename}.log")
        check_archive(path, digest)
    for filename, digest in PACKAGES.items():
        path = downloads / filename
        if not path.exists() and args.download and not args.verify_only:
            name, version, _arch = filename.rsplit("_", 2)
            run(["apt", "download", f"{name}={version}"], cwd=downloads, env=env,
                log=logs / f"download-{name}.log")
        check_archive(path, digest)
    runtime_cache = downloads / "runtime"
    runtime_cache.mkdir(exist_ok=True)
    for filename, digest in RUNTIME_SNAPSHOTS.items():
        cached = runtime_cache / filename
        if not cached.exists() and not args.verify_only:
            source = Path("/usr/lib/x86_64-linux-gnu") / filename
            check_archive(source, digest)
            shutil.copy2(source, cached)
        check_archive(cached, digest)
    if not args.verify_only:
        for filename in PACKAGES:
            run(["dpkg-deb", "-x", str(downloads / filename), str(deps)],
                cwd=sdk, env=env, log=logs / f"extract-{filename}.log")
        link_existing(Path("/usr/lib/x86_64-linux-gnu/libboost_iostreams.so.1.90.0"),
                      deps / "usr/lib/x86_64-linux-gnu/libboost_iostreams.so.1.90.0")
        src = sdk / "src"
        fcitx_source = src / "fcitx5-5.1.19"
        libime_source = src / "libime-1.1.14"
        for filename, target in (("fcitx5-5.1.19.tar.gz", fcitx_source),
                                 ("libime-1.1.14.tar.gz", libime_source),
                                 ("chinese-addons-5.1.12.tar.gz", src / "fcitx5-chinese-addons-5.1.12")):
            extract_if_absent(downloads / filename, src, target)
        kenlm = libime_source / "src/libime/core/kenlm"
        if not (kenlm / "lm/model.hh").exists():
            staged = src / "kenlm-4cb443e60b7bf2c0ddf3c745378f76cb59e254e5"
            extract_if_absent(downloads / "kenlm-4cb443e60b7bf2c0ddf3c745378f76cb59e254e5.tar.gz", src, staged)
            if kenlm.exists() and any(kenlm.iterdir()):
                raise RuntimeError("existing kenlm directory is incomplete and nonempty; preserved")
            shutil.copytree(staged, kenlm, dirs_exist_ok=True)
        spell_archive = fcitx_source / "src/modules/spell/en_dict-20121020.tar.gz"
        if not spell_archive.exists():
            shutil.copyfile(downloads / spell_archive.name, spell_archive)
        check_archive(spell_archive, SOURCES[spell_archive.name][1])
        common = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                  f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_INSTALL_LIBDIR=lib",
                  f"-DCMAKE_PREFIX_PATH={prefix};{deps / 'usr'}"]
        fcitx_build = sdk / "build/fcitx5"
        run(["cmake", "-S", str(fcitx_source), "-B", str(fcitx_build), *common,
             "-DENABLE_X11=OFF", "-DENABLE_WAYLAND=OFF", "-DENABLE_DBUS=OFF",
             "-DENABLE_ENCHANT=OFF", "-DENABLE_SERVER=OFF", "-DENABLE_XDGAUTOSTART=OFF",
             "-DENABLE_EMOJI=OFF", "-DBUILD_SPELL_DICT=ON", "-DENABLE_TEST=ON",
             "-DENABLE_TESTING_ADDONS=ON", "-DENABLE_KEYBOARD=ON", "-DEVENT_LOOP_BACKEND=libuv"],
            cwd=sdk, env=env, log=logs / "fcitx5-configure.log")
        run(["cmake", "--build", str(fcitx_build), "-j", str(args.jobs)], cwd=sdk, env=env, log=logs / "fcitx5-build.log")
        run(["cmake", "--install", str(fcitx_build)], cwd=sdk, env=env, log=logs / "fcitx5-install.log")
        run(["ctest", "--test-dir", str(fcitx_build), "-R", "^(testinputcontext|testaddon|testinstance)$", "--output-on-failure"],
            cwd=sdk, env=env, log=logs / "fcitx5-headless-tests.log")
        libime_build = sdk / "build/libime"
        run(["cmake", "-S", str(libime_source), "-B", str(libime_build), *common,
             "-DENABLE_TEST=OFF", "-DENABLE_DATA=OFF", "-DENABLE_TOOLS=OFF"],
            cwd=sdk, env=env, log=logs / "libime-configure.log")
        for component in ("header", "Devel"):
            run(["cmake", "--install", str(libime_build), "--component", component],
                cwd=sdk, env=env, log=logs / f"libime-install-{component}.log")
        for name in ("Core", "Pinyin", "Table"):
            filename = f"libIME{name}.so.1.1.14"
            target = prefix / "lib" / filename
            if target.exists() or target.is_symlink():
                check_archive(target, RUNTIME_SNAPSHOTS[filename])
                if target.is_symlink():
                    raise RuntimeError(f"runtime must be a prefix-owned snapshot, not a host symlink: {target}")
            else:
                shutil.copy2(runtime_cache / filename, target)
            for suffix in (".so", ".so.0"):
                link_existing(target, prefix / "lib" / f"libIME{name}{suffix}")
        for name in ("libime_pinyindict", "libime_tabledict", "libime_slm_build_binary", "libime_prediction", "libime_history"):
            link_existing(Path("/usr/bin") / name, prefix / "bin" / name)
        for filename, digest in SHARED_DICTIONARIES.items():
            source = Path("/usr/share/libime") / filename
            check_archive(source, digest)
            link_existing(source, prefix / "share/libime" / filename)
    expected_files = ["lib/cmake/Fcitx5Core/Fcitx5CoreConfig.cmake", "lib/cmake/LibIMEPinyin/LibIMEPinyinConfig.cmake",
                      "lib/libFcitx5Core.so.5.1.19", "lib/libIMECore.so.1.1.14", "lib/libIMEPinyin.so.1.1.14",
                      "lib/fcitx5/libtestfrontend.so", "share/fcitx5/addon/keyboard.conf"]
    missing = [str(prefix / name) for name in expected_files if not (prefix / name).is_file()]
    if missing:
        raise RuntimeError("SDK is incomplete: " + ", ".join(missing))
    for filename, digest in RUNTIME_SNAPSHOTS.items():
        target = prefix / "lib" / filename
        check_archive(target, digest)
        if target.is_symlink():
            raise RuntimeError(f"SDK still depends on a mutable host runtime symlink: {target}")
    report = {"status": "sdk_paths_and_download_hashes_verified", "prefix": str(prefix),
              "cmake_prefix_path": f"{prefix};{deps / 'usr'}", "host_versions": host_versions,
              "mode": "verify_only" if args.verify_only else "build_and_headless_tests",
              "fcitx_upstream_version": "5.1.19", "libime_header_version": "1.1.14",
              "libime_runtime": "prefix-owned hash-locked copies of original host 1.1.14 shared libraries",
              "libime_runtime_hashes": RUNTIME_SNAPSHOTS,
              "shared_dictionary_hashes": SHARED_DICTIONARIES,
              "host_deployment_compatibility": "not_verified",
              "pinyin_build": "owned by bridge preparation; not performed by this script",
              "semantic_translation_quality": "not_evaluated",
              "sources": {name: {"url": url, "sha256": digest} for name, (url, digest) in SOURCES.items()},
              "dependency_packages": PACKAGES}
    (logs / "sdk-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"SDK preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
