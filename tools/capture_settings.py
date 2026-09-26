#!/usr/bin/env python3
"""Render settings with synthetic temporary data; execute inside a VM only."""
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from settings.app import SettingsWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="transime-ui-capture-") as directory:
        root = Path(directory)
        window = SettingsWindow(root / "config", root / "data")
        window.show()
        for index, name in enumerate(("input", "shortcuts", "dictionaries", "translation")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            if not window.grab().save(str(args.output / (name + ".png"))):
                raise RuntimeError("Screenshot save failed")
        window.close()
    print("Rendered four settings pages using temporary synthetic state: " + str(args.output))


if __name__ == "__main__":
    main()
