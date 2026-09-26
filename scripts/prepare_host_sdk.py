#!/usr/bin/env python3
"""Prepare a host-version SDK and patched pinyin in work/; never install on host.

Uses exact development packages extracted with dpkg-deb, hash-locked copies of
the matching host runtime libraries, and the official 5.1.12 pinyin source.
No apt install/update, service control, user configuration, or model download.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import prepare_sdk as common

PACKAGES = {
    "libfcitx5core-dev_5.1.21-1_amd64.deb": "8a4e0d244a81c80aa24a71a107d2455f1e4dbcd0153ada35e678a8124534c674",
    "libfcitx5config-dev_5.1.21-1_amd64.deb": "7773f54b3e095e918751e61805502e36753146328ef63766d25070d74c07a7d5",
    "libfcitx5utils-dev_5.1.21-1_amd64.deb": "5d8fc0c66f06f0f03497f894448eff73c81649cd1572b7942c3247d4e8825488",
    "fcitx5-modules-dev_5.1.21-1_amd64.deb": "a4048641d87e26b3dbee706042c391186c4a9cdaadd7dc8b5c27525055d6932e",
    "fcitx5-module-lua-dev_5.0.17-1_amd64.deb": "bc978bdf69973da886454bd26a213ebc6759cb51108bb031baa163e1180cee16",
    "libimecore-dev_1.1.15-2_amd64.deb": "9c522925088f86acd4bbbd717a0c5c94f556964adc45b3246ba35c9c2ff1f726",
    "libimepinyin-dev_1.1.15-2_amd64.deb": "0e9f5aa178f77c066b0d4d4f8408aa82020b9f5c543588599c6955b3b481e16b",
    "libimetable-dev_1.1.15-2_amd64.deb": "10ca57ea5e95b52319bed1b61b607c4931498dcaea376b23b1fcaeffb887fd3f",
}
HOST_VERSIONS = {
    "libfcitx5core7": "5.1.21-1", "libfcitx5config6": "5.1.21-1",
    "libfcitx5utils2": "5.1.21-1", "libimecore0": "1.1.15-2",
    "libimepinyin0": "1.1.15-2", "libimetable0": "1.1.15-2",
    "fcitx5-pinyin": "5.1.12-1",
    "fcitx5-module-lua": "5.0.17-1",
}
RUNTIMES = {
    "libFcitx5Core.so.5.1.21": ("7", "c99dfc81f75fa81bd47be40824f69a426a262a3ef6e36d1d544cc708b1bdc87e"),
    "libFcitx5Config.so.5.1.21": ("6", "b2ef849b83072f086627ccd7475152e3f522a53796c48fdef7fb3a80bbf2dadf"),
    "libFcitx5Utils.so.5.1.21": ("2", "5f93ce48e610ff4a0c885b5bde89199ad41c9a696276fa08e5f466a460f47464"),
    "libIMECore.so.1.1.15": ("0", "00aafcdcf52d1e0cecd5fbd9f22f16c1f4c7950d171a87965691e244bb4e7fe2"),
    "libIMEPinyin.so.1.1.15": ("0", "e463c0f54c85fcdf934d10363de86466ec38641394fac064ede7e9602a233a42"),
    "libIMETable.so.1.1.15": ("0", "0e4e78a03258845b4845390bc448348deb4533553187d2875a74897cf8d63adc"),
}
FCITX_SOURCE = ("fcitx5-5.1.21.tar.gz",
    "https://codeload.github.com/fcitx/fcitx5/tar.gz/refs/tags/5.1.21",
    "8211fe5996db22254e5df9617cbd45873ae7fab82e7e0c42bde5a197299d1276")


def main() -> int:
    project = Path(__file__).absolute().parents[1]
    workspace = project.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", type=Path, default=workspace / "work/host-sdk")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--test-support", action="store_true", help="build matching headless keyboard/test modules from official source")
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    sdk = args.sdk_dir.absolute()
    if not sdk.resolve().is_relative_to((workspace / "work").resolve()) or args.jobs < 1:
        raise RuntimeError("SDK must be inside workspace work/ and --jobs positive")
    if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
        raise RuntimeError("this lock is for amd64 only")
    versions = {name: subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", name], text=True).strip()
                for name in HOST_VERSIONS}
    if versions != HOST_VERSIONS:
        raise RuntimeError(f"host disk versions changed; revise and verify the lock first: {versions}")
    downloads, prefix, deps, logs = (sdk / name for name in ("downloads", "prefix/usr", "deps", "logs"))
    for directory in (downloads, prefix, deps, logs):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(deps / "usr/bin") + os.pathsep + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = str(deps / "usr/lib/x86_64-linux-gnu")

    def run(command: list[str], name: str, cwd: Path = sdk) -> None:
        common.run(command, cwd=cwd, env=env, log=logs / name)

    old_cache = workspace / "work/sdk/downloads"
    for filename, digest in (PACKAGES | common.PACKAGES).items():
        package = downloads / filename
        if not package.exists() and not args.verify_only:
            if (old_cache / filename).is_file():
                common.check_archive(old_cache / filename, digest)
                shutil.copy2(old_cache / filename, package)
            elif args.download:
                name, version, _ = filename.rsplit("_", 2)
                run(["apt", "download", f"{name}={version}"], f"download-{name}.log", downloads)
        common.check_archive(package, digest)
        if not args.verify_only:
            root = prefix.parent if filename in PACKAGES else deps
            run(["dpkg-deb", "-x", str(package), str(root)], f"extract-{filename}.log")
    # Debian module exports and this compiler-settings variable are absolute.
    # Relocate only development lookup paths in the extracted copies. Runtime
    # data paths remain /usr/share so the deployment bridge uses system data.
    relocations = []
    for cmake_file in (prefix / "lib/x86_64-linux-gnu/cmake").glob("*/*.cmake"):
        original = cmake_file.read_text()
        changed = original.replace(
            'set(FCITX_INSTALL_CMAKECONFIG_DIR "/usr/lib/x86_64-linux-gnu/cmake")',
            f'set(FCITX_INSTALL_CMAKECONFIG_DIR "{prefix}/lib/x86_64-linux-gnu/cmake")').replace(
            'INTERFACE_INCLUDE_DIRECTORIES "/usr/include/',
            f'INTERFACE_INCLUDE_DIRECTORIES "{prefix}/include/')
        if changed != original:
            if args.verify_only:
                raise RuntimeError(f"unrelocated SDK CMake lookup: {cmake_file}")
            before = common.sha256(cmake_file)
            cmake_file.write_text(changed)
            relocations.append({"path": str(cmake_file), "package_sha256": before,
                                "relocated_sha256": common.sha256(cmake_file)})
    if relocations:
        (logs / "cmake-relocations.json").write_text(json.dumps(relocations, indent=2) + "\n")
    runtime_manifest = []
    libdir = prefix / "lib/x86_64-linux-gnu"
    libdir.mkdir(parents=True, exist_ok=True)
    for filename, (soname, digest) in RUNTIMES.items():
        source = Path("/usr/lib/x86_64-linux-gnu") / filename
        target = libdir / filename
        common.check_archive(source, digest)
        if not target.exists() and not args.verify_only:
            shutil.copy2(source, target)
        common.check_archive(target, digest)
        if target.is_symlink():
            raise RuntimeError(f"SDK runtime must be a fixed file copy: {target}")
        base = filename.split(".so.")[0]
        for suffix in (".so." + soname, ".so"):
            link = libdir / (base + suffix)
            if not link.exists() and not link.is_symlink() and not args.verify_only:
                link.symlink_to(filename)
            if not link.is_file() or link.resolve() != target.resolve():
                raise RuntimeError(f"unexpected SDK runtime link: {link}")
        runtime_manifest.append({"source": str(source), "copy": str(target), "sha256": digest})
    for name in ("libime_pinyindict", "libime_tabledict", "libime_slm_build_binary", "libime_prediction", "libime_history"):
        if not args.verify_only:
            common.link_existing(Path("/usr/bin") / name, prefix / "bin" / name)
    if not args.verify_only:
        common.link_existing(Path("/usr/lib/x86_64-linux-gnu/libboost_iostreams.so.1.90.0"),
                             deps / "usr/lib/x86_64-linux-gnu/libboost_iostreams.so.1.90.0")
    source_locks = {"chinese-addons-5.1.12.tar.gz": common.SOURCES["chinese-addons-5.1.12.tar.gz"]}
    if args.test_support:
        source_locks[FCITX_SOURCE[0]] = FCITX_SOURCE[1:]
        source_locks["en_dict-20121020.tar.gz"] = common.SOURCES["en_dict-20121020.tar.gz"]
    for filename, (url, digest) in source_locks.items():
        archive = downloads / filename
        if not archive.exists() and not args.verify_only:
            if (old_cache / filename).exists():
                common.check_archive(old_cache / filename, digest)
                shutil.copy2(old_cache / filename, archive)
            elif args.download:
                run(["curl", "--fail", "--location", "--retry", "2", "--output", str(archive), url],
                    f"download-{filename}.log")
        common.check_archive(archive, digest)
    pinyin_source = sdk / "src/fcitx5-chinese-addons-5.1.12"
    pinyin_build = sdk / "build/pinyin"
    patch = project / "bridge/pinyin-5.1.12-transime-snapshot-v1.patch"
    patch_digest = common.sha256(patch)
    stamp = pinyin_source / ".transime-patch-sha256"
    if not args.verify_only:
        common.extract_if_absent(downloads / "chinese-addons-5.1.12.tar.gz", sdk / "src", pinyin_source)
        if stamp.exists():
            if stamp.read_text().strip() != patch_digest:
                raise RuntimeError("bridge patch changed; select a fresh --sdk-dir rather than overwrite source")
        else:
            run(["git", "apply", "--check", str(patch)], "bridge-apply-check.log", pinyin_source)
            run(["git", "apply", str(patch)], "bridge-apply.log", pinyin_source)
            stamp.write_text(patch_digest + "\n")
        run(["cmake", "-S", str(pinyin_source), "-B", str(pinyin_build), "-G", "Ninja",
             "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_PREFIX_PATH={prefix};{deps / 'usr'}",
             "-DCMAKE_INSTALL_PREFIX=/usr", "-DCMAKE_SKIP_RPATH=ON",
             "-DENABLE_GUI=OFF", "-DENABLE_BROWSER=OFF", "-DENABLE_OPENCC=OFF",
             "-DENABLE_CLOUDPINYIN=OFF", "-DENABLE_DATA=OFF", "-DENABLE_TEST=OFF"], "pinyin-configure.log")
        run(["cmake", "--build", str(pinyin_build), "--target", "pinyin", "pinyinhelper", "punctuation", "chttrans",
             "-j", str(args.jobs)], "pinyin-build.log")
    bridge = pinyin_build / "bin/libpinyin.so"
    if not bridge.is_file():
        raise RuntimeError("missing compiled pinyin bridge")
    dynamic = subprocess.check_output(["readelf", "-d", str(bridge)], text=True)
    if "(RPATH)" in dynamic or "(RUNPATH)" in dynamic:
        raise RuntimeError("deployment bridge must not retain a workspace RPATH")
    (logs / "pinyin-readelf.txt").write_text(dynamic)
    (logs / "pinyin-ldd.txt").write_text(subprocess.check_output(["ldd", str(bridge)], text=True))

    keyboard = sdk / "build/fcitx5-test-support/lib/libkeyboard.a"
    if args.test_support and not args.verify_only:
        fcitx_source = sdk / "src/fcitx5-5.1.21"
        common.extract_if_absent(downloads / FCITX_SOURCE[0], sdk / "src", fcitx_source)
        spell = fcitx_source / "src/modules/spell/en_dict-20121020.tar.gz"
        if not spell.exists():
            shutil.copy2(downloads / spell.name, spell)
        support = sdk / "build/fcitx5-test-support"
        run(["cmake", "-S", str(fcitx_source), "-B", str(support), "-G", "Ninja",
             "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_INSTALL_PREFIX={sdk / 'test-prefix'}",
             f"-DCMAKE_PREFIX_PATH={prefix};{deps / 'usr'}", "-DCMAKE_INSTALL_LIBDIR=lib",
             "-DENABLE_X11=OFF", "-DENABLE_WAYLAND=OFF", "-DENABLE_DBUS=OFF", "-DENABLE_ENCHANT=OFF",
             "-DENABLE_SERVER=OFF", "-DENABLE_XDGAUTOSTART=OFF", "-DENABLE_EMOJI=OFF",
             "-DBUILD_SPELL_DICT=ON", "-DENABLE_TEST=ON", "-DENABLE_TESTING_ADDONS=ON",
             "-DENABLE_KEYBOARD=ON", "-DEVENT_LOOP_BACKEND=libuv"], "test-support-configure.log")
        run(["cmake", "--build", str(support), "--target", "keyboard", "testui", "testim", "dummyaddondeps", "testaddon", "testinputcontext",
             "-j", str(args.jobs)], "test-support-build.log")
        run(["ctest", "--test-dir", str(support), "-R", "^(testinputcontext|testaddon)$", "--output-on-failure"],
            "test-support-ctest.log")
    report = {"status": "prepared_not_installed", "prefix": str(prefix),
              "cmake_prefix_path": f"{prefix};{deps / 'usr'}", "host_disk_versions": versions,
              "sdk_kind": "extracted matching distro development packages with hash-locked host runtime copies",
              "bridge": str(bridge), "bridge_sha256": common.sha256(bridge), "bridge_patch_sha256": patch_digest,
              "rpath": "none", "keyboard_static_library": str(keyboard) if keyboard.exists() else None,
              "runtime_files": runtime_manifest, "development_packages": PACKAGES,
              "mode": "verify_only" if args.verify_only else "prepare_and_build",
              "desktop_activation": "not_performed", "running_old_process_compatibility": "not_claimed"}
    (logs / "host-sdk-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
