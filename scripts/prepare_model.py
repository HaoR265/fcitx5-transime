#!/usr/bin/env python3
"""Prepare a pinned, local CPU INT8 model and two isolated Python environments.

Network use requires --allow-download. The worker itself never downloads. All
files, package caches and logs are confined to an explicit --work-dir. This tool
does not modify Fcitx, the system Python, or any input-method configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
MODEL_ID = "Helsinki-NLP/opus-mt-zh-en"
REVISION = "cf109095479db38d6df799875e34039d4938aaa6"
MODEL_NAME = "opus-mt-zh-en-ct2-int8"
SOURCE_HASHES = {
    "README.md": "7f4b7f90672cd55b85b213bbe21f4b2d0c92327e885734b7e86b019feb145a6b",
    "config.json": "d84104da12fa90da59acf5f0ed30428e43cc92574ad4b9dfa88f29eeeb2b0eb0",
    "generation_config.json": "81f06422302b9f4666f20c6f20f43b904e38ccce9326a208f82ff97c2c9699e5",
    "metadata.json": "b412c4bd90f9161deb21e018b9b9bb0d5c1ddcfa528b78a550f82c261b9f8ddc",
    "pytorch_model.bin": "9d8ceb91d103ef89400c9d9d62328b4858743cf8924878aee3b8afc594242ce0",
    "source.spm": "e27a3a1b539f4959ec72ea60e453f49156289f95d4e6000b29332efc45616203",
    "target.spm": "6a881f4717cd7265f53fea54fd3dc689c767c05338fac7a4590f3088cb2d7855",
    "tokenizer_config.json": "53cc7a6f05224f287476af4798d96b1b85da2edae60257e58633f092c7b5b823",
    "vocab.json": "c0f79ee4c413ccb24cd808a0c727eec85b2130f23c82802cc1caeb07b5f0d458",
}
# These hashes are from the actual pinned conversion, not an upstream CT2 model.
CONVERTED_HASHES = {
    "config.json": "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
    "model.bin": "e4955858cae9542bef37424a9b79720e3db2f32501fe62264c0cd3eac6319777",
    "shared_vocabulary.json": "acbf13516c56af2bb78ebdb770c2e4648fd8232ddd17af75298119cbfa7ee33d",
    "source.spm": SOURCE_HASHES["source.spm"],
    "target.spm": SOURCE_HASHES["target.spm"],
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check_files(directory: Path, expected: dict[str, str]) -> list[dict]:
    records = []
    for name, checksum in expected.items():
        path = directory / name
        if not path.is_file() or digest(path) != checksum:
            raise RuntimeError(f"missing_or_mismatched_file: {path}")
        records.append({"name": name, "sha256": checksum, "bytes": path.stat().st_size})
    return records


def prepare_source(directory: Path, allow_download: bool) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    for name, checksum in SOURCE_HASHES.items():
        destination = directory / name
        if destination.exists():
            if digest(destination) != checksum:
                raise RuntimeError(f"refusing_to_overwrite_mismatched_file: {destination}")
            continue
        if not allow_download:
            raise RuntimeError(f"download_permission_required_for: {name}")
        temporary = destination.with_name(destination.name + ".partial")
        url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}?download=true"
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        if digest(temporary) != checksum:
            raise RuntimeError(f"download_hash_mismatch: {name}; partial retained for inspection")
        temporary.replace(destination)
    return check_files(directory, SOURCE_HASHES)


def package_versions(python: Path) -> dict[str, str]:
    code = "import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}))"
    return json.loads(subprocess.check_output([str(python), "-I", "-c", code], text=True))


def lock_versions(path: Path) -> dict[str, str]:
    return dict(line.split("==", 1) for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#"))


def run(command: list[str], work: Path, log_name: str, offline: bool = False):
    env = dict(os.environ, UV_CACHE_DIR=str(work / "uv-cache"),
               UV_PYTHON_INSTALL_DIR=str(work / "python"), PYTHONDONTWRITEBYTECODE="1",
               CUDA_VISIBLE_DEVICES="", OMP_WAIT_POLICY="PASSIVE", KMP_BLOCKTIME="0",
               OPENBLAS_NUM_THREADS="1")
    if offline:
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    with (work / "logs" / log_name).open("w") as stream:
        subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)


def prepare_environment(kind: str, args) -> tuple[Path, dict[str, str]]:
    directory = args.work_dir / kind
    python = directory / "bin/python"
    lock = PROJECT / "worker" / f"requirements-{kind}.lock.txt"
    expected = lock_versions(lock)
    if kind == "conversion":
        expected["torch"] = "2.6.0+cpu"
    actual = package_versions(python) if python.is_file() else {}
    if actual != expected:
        if not args.allow_download:
            raise RuntimeError(f"isolated_{kind}_environment_missing_or_different; use --allow-download to prepare")
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv_not_found; provide an installed uv executable on PATH")
        if not python.is_file():
            version = json.loads(subprocess.check_output(
                [str(args.python), "-I", "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"], text=True))
            if version != [3, 12]:
                raise RuntimeError("preparation_requires_existing_python_3_12")
            run([uv, "venv", "--python", str(args.python), "--no-python-downloads", str(directory)],
                args.work_dir, f"create-{kind}.log")
        if kind == "conversion":
            run([uv, "pip", "install", "--python", str(python), "torch==2.6.0+cpu",
                 "--index-url", "https://download.pytorch.org/whl/cpu"],
                args.work_dir, "install-conversion-torch.log")
            run([uv, "pip", "install", "--python", str(python), "-r", str(lock)],
                args.work_dir, "install-conversion-locked.log")
        else:
            run([uv, "pip", "sync", "--python", str(python), str(lock)],
                args.work_dir, "install-runtime-locked.log")
        actual = package_versions(python)
    if actual != expected:
        raise RuntimeError(f"{kind}_package_lock_mismatch")
    version = json.loads(subprocess.check_output(
        [str(python), "-I", "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"], text=True))
    if version != [3, 12]:
        raise RuntimeError(f"{kind}_python_version_mismatch")
    return python, actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="Existing Python 3.12 interpreter used to create venvs; never replaced")
    parser.add_argument("--allow-download", action="store_true",
                        help="Explicitly allow pinned model and Python package downloads")
    parser.add_argument("--verify-only", action="store_true", help="Check hashes and versions; do not create or modify artifacts")
    args = parser.parse_args()
    args.work_dir = args.work_dir.resolve()
    if args.verify_only and args.allow_download:
        parser.error("--verify-only cannot be combined with --allow-download")
    if args.work_dir == PROJECT or PROJECT in args.work_dir.parents:
        parser.error("--work-dir must be outside the deliverable source tree")
    if not args.verify_only:
        (args.work_dir / "logs").mkdir(parents=True, exist_ok=True)
    source = args.work_dir / "source-model"
    model = args.work_dir / "models" / MODEL_NAME
    source_records = (check_files(source, SOURCE_HASHES) if args.verify_only
                      else prepare_source(source, args.allow_download))
    runtime_python, runtime_versions = prepare_environment("runtime", args)
    conversion_python, conversion_versions = prepare_environment("conversion", args)
    if not model.exists():
        if args.verify_only:
            raise RuntimeError("converted_model_missing")
        model.parent.mkdir(parents=True, exist_ok=True)
        temporary = model.with_name(MODEL_NAME + ".converting")
        if temporary.exists():
            raise RuntimeError("incomplete_conversion_present; inspect it before retrying")
        run([str(conversion_python), "-I", "-c", "from ctranslate2.converters.transformers import main; main()",
             "--model", str(source), "--output_dir", str(temporary), "--quantization", "int8",
             "--copy_files", "source.spm", "target.spm"], args.work_dir, "convert-int8.log", offline=True)
        check_files(temporary, CONVERTED_HASHES)
        temporary.replace(model)
    converted_records = check_files(model, CONVERTED_HASHES)
    manifest = {
        "model_id": MODEL_ID, "revision": REVISION, "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": f"https://huggingface.co/{MODEL_ID}/tree/{REVISION}",
        "modification": "Converted Marian weights to CTranslate2 CPU INT8; model behavior not retrained",
        "source_files": source_records, "converted_files": converted_records,
        "converted_total_bytes": sum(record["bytes"] for record in converted_records),
        "runtime_python": str(runtime_python), "runtime_packages": runtime_versions,
        "conversion_python": str(conversion_python), "conversion_packages": conversion_versions,
        "torch_in_runtime": "torch" in runtime_versions,
        "translation_quality": "requires independent human review",
    }
    if not args.verify_only:
        (model / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copyfile(source / "README.md", model / "README.source.md")
        (model / "ATTRIBUTION.md").write_text(
            "# Model attribution\n\nHelsinki-NLP / OPUS-MT, Chinese to English.\n\n"
            f"Source: https://huggingface.co/{MODEL_ID}/tree/{REVISION}\n\n"
            "License: CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/\n\n"
            "Modification: converted to CTranslate2 INT8 with CTranslate2 4.8.0; "
            "no fine-tuning. See README.source.md for upstream authorship and model information.\n")
    print(json.dumps({"status": "artifacts_verified", "model_dir": str(model),
                      "runtime_python": str(runtime_python), "model_bytes": manifest["converted_total_bytes"],
                      "translation_quality": "not_assessed_by_preparation"}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
