#!/usr/bin/env python3
"""Run model-free component checks; use a disposable Linux test VM."""
from pathlib import Path
import os
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
MODULES = (
    "test_context.py", "test_numbers.py", "test_worker.py",
    "test_worker_resilience.py", "test_evaluation.py", "test_dictionaries.py",
    "test_settings_configuration.py", "test_settings_install.py",
)


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required.")
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for module in MODULES:
        print(f"Checking {module}", flush=True)
        subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
             "-p", module, "-v"],
            cwd=PROJECT, env=environment, check=True,
        )
    print("All selected component suites passed; GUI, full installation, "
          "real-app input and model quality are not tested by this command.")


if __name__ == "__main__":
    main()
