"""Installer lifecycle tests; run only in disposable VM directories."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("settings_installer", Path(__file__).resolve().parents[1] / "tools/install_settings.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "settings").mkdir(parents=True)
        (self.source / "examples/dictionaries").mkdir(parents=True)
        (self.source / "settings/app.py").write_text("# version 1\n")
        (self.source / "examples/dictionaries/example.txt").write_text("人工示例\n")
        self.data = self.root / "data"
        self.destination = self.data / "transime-settings"
        self.desktop = self.data / "applications/transime-settings.desktop"

    def install(self):
        return installer.install(self.source, self.data)

    def test_install_upgrade_uninstall_preserves_user_data(self):
        personal = self.data / "fcitx5/pinyin/user.dict"
        personal.parent.mkdir(parents=True)
        personal.write_bytes(b"synthetic personal sentinel")
        self.install()
        self.assertIn("python3 -B", self.desktop.read_text())
        (self.source / "settings/app.py").write_text("# version 2\n")
        installer.install(self.source, self.data, upgrade=True)
        self.assertEqual((self.destination / "settings/app.py").read_text(), "# version 2\n")
        installer.uninstall(self.data)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.desktop.exists())
        self.assertEqual(personal.read_bytes(), b"synthetic personal sentinel")

    def test_existing_install_requires_explicit_upgrade(self):
        self.install()
        with self.assertRaises(ValueError):
            self.install()

    def test_upgrade_requires_install(self):
        with self.assertRaises(ValueError):
            installer.install(self.source, self.data, upgrade=True)

    def test_legacy_unowned_install_rejected(self):
        self.destination.mkdir(parents=True)
        marker = self.destination / "keep"
        marker.write_text("untouched")
        with self.assertRaises(ValueError):
            installer.install(self.source, self.data, upgrade=True)
        self.assertEqual(marker.read_text(), "untouched")

    def test_modified_code_refuses_upgrade_and_uninstall(self):
        self.install()
        (self.destination / "settings/app.py").write_text("user modifications")
        with self.assertRaises(ValueError):
            installer.install(self.source, self.data, upgrade=True)
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)
        self.assertTrue(self.desktop.exists())

    def test_extra_file_refuses_uninstall(self):
        self.install()
        (self.destination / "important.txt").write_text("user file")
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)

    def test_extra_empty_directory_refuses_uninstall(self):
        self.install()
        (self.destination / "unowned").mkdir()
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)

    def test_modified_launcher_refuses_uninstall(self):
        self.install()
        self.desktop.write_text("another application")
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)

    def test_symlink_ancestor_refuses_install(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(self.source, link / "nested")
        self.assertFalse((real / "nested").exists())

    def test_symlink_source_refuses_install(self):
        (self.source / "settings/link.py").symlink_to(self.source / "settings/app.py")
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.data.exists())

    def test_symlink_owned_file_refuses_uninstall(self):
        self.install()
        app = self.destination / "settings/app.py"
        app.unlink()
        app.symlink_to(self.source / "settings/app.py")
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)
        self.assertTrue((self.source / "settings/app.py").exists())

    def test_failed_upgrade_rolls_back(self):
        self.install()
        previous = (self.destination / "settings/app.py").read_bytes()
        launcher = self.desktop.read_bytes()
        (self.source / "settings/app.py").write_text("# new version")
        original = Path.rename
        def fail_payload(path, target):
            if path.name == "payload":
                raise OSError("synthetic rename failure")
            return original(path, target)
        with patch.object(Path, "rename", fail_payload):
            with self.assertRaises(OSError):
                installer.install(self.source, self.data, upgrade=True)
        self.assertEqual((self.destination / "settings/app.py").read_bytes(), previous)
        self.assertEqual(self.desktop.read_bytes(), launcher)

    def test_invalid_manifest_refuses_uninstall(self):
        self.install()
        (self.destination / installer.MANIFEST).write_text("[]")
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)
        self.assertTrue(self.desktop.exists())

    def test_symlink_manifest_refuses_uninstall(self):
        self.install()
        manifest = self.destination / installer.MANIFEST
        original = self.root / "manifest.json"
        manifest.rename(original)
        manifest.symlink_to(original)
        with self.assertRaises(ValueError):
            installer.uninstall(self.data)
        self.assertTrue(self.desktop.exists())


if __name__ == "__main__":
    unittest.main()
