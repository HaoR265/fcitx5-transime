#!/usr/bin/env python3
"""Build the experimental fcitx5-rime 5.1.14 bridge in a disposable VM.

Never installs into a user's input method, changes an active profile, or runs
on the host. The exact upstream source archive is required as an input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


SOURCE_SHA256 = "8f6513eeb06f3d28d831eb0a33e439f63258c58673107aaa05ecef401cf393f6"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tar", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--sdk-prefix", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    if subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True).returncode:
        parser.error("Rime bridge builds are permitted only inside a VM")
    root = args.work_root.absolute()
    if not root.is_relative_to(Path("/var/tmp")) or root == Path("/var/tmp") or root.exists():
        parser.error("--work-root must be a fresh directory below /var/tmp")
    if args.jobs < 1 or not args.sdk_prefix.is_dir():
        parser.error("--jobs must be positive and --sdk-prefix must exist")
    if digest(args.source_tar) != SOURCE_SHA256:
        parser.error("upstream fcitx5-rime 5.1.14 source SHA-256 does not match")

    project = Path(__file__).resolve().parents[1]
    patch = project / "bridge/rime-5.1.14-transime-snapshot-v1.patch"
    public_header = project / "bridge/rime_public.h"
    root.mkdir(parents=True)
    source = root / "fcitx5-rime-5.1.14"
    run("tar", "-xf", str(args.source_tar.absolute()), "-C", str(root))
    if not (source / "src/rimeengine.cpp").is_file():
        raise RuntimeError("unexpected upstream source layout")
    shutil.copy2(public_header, source / "src/rime_public.h")
    run("git", "apply", "--recount", "--check", str(patch), cwd=source)
    run("git", "apply", "--recount", str(patch), cwd=source)
    build = root / "build"
    run("cmake", "-S", str(source), "-B", str(build),
        f"-DCMAKE_PREFIX_PATH={args.sdk_prefix.absolute()}",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo")
    run("cmake", "--build", str(build), "--target", "rime", "-j", str(args.jobs))
    library = build / "bin/librime.so"
    if not library.is_file():
        raise RuntimeError("Rime addon missing after build")
    manifest = {
        "source_sha256": SOURCE_SHA256,
        "patch_sha256": digest(patch),
        "header_sha256": digest(public_header),
        "library_sha256": digest(library),
        "library": str(library),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
