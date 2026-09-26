"""Run only inside a test VM with QT_QPA_PLATFORM=offscreen."""
import os
from pathlib import Path
import tempfile
import time
import shutil
import unittest
from unittest.mock import patch
import subprocess

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from settings.app import SettingsWindow, KeyEditor
from settings.configuration import SettingsConfig


class SettingsUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = SettingsWindow(self.root / "config", self.root / "data")

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_launch_has_no_filesystem_side_effects(self):
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.tabs.count(), 4)
        self.assertFalse(self.window.flags["ShowModeHints"].isChecked())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_save_round_trip(self):
        self.window.flags["Enabled"].setChecked(True)
        self.window.mode.setCurrentIndex(1)
        self.window.keys["ToggleKey"].text.setText("Control+F9")
        self.assertTrue(self.window.save())
        saved = SettingsConfig(self.root / "config/conf/transime.conf")
        self.assertTrue(saved.values["Enabled"])
        self.assertFalse(saved.values["ContinuousComposition"])
        self.assertEqual(saved.keylists["ToggleKey"], ["Control+F9"])

    def test_key_editor_keeps_secondary_binding(self):
        self.assertEqual(self.window.keys["RawCommitKey"].keys(), ["Return", "KP_Enter"])

    def test_isolated_paths_do_not_send_session_requests(self):
        self.assertFalse(self.window.reload_button.isEnabled())
        self.assertFalse(self.window.load_packs_button.isEnabled())
        with patch("settings.app.subprocess.run") as run:
            self.window.reload_input_method()
            self.window.load_dictionary_packs()
            run.assert_not_called()

    def test_dictionary_refresh_uses_targeted_api(self):
        self.window.session_actions = True
        with patch("settings.app.QMessageBox.question", return_value=QMessageBox.Yes), \
             patch("settings.app.shutil.which", return_value="/usr/bin/gdbus"), \
             patch("settings.app.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, "(true,)", ""), subprocess.CompletedProcess([], 0, "()", "")]) as run:
            self.window.load_dictionary_packs()
            deadline = time.monotonic() + 5
            while self.window.job is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(self.window.job)
            argv = run.call_args.args[0]
            self.assertIn("org.fcitx.Fcitx.Controller1.SetConfig", argv)
            self.assertIn("fcitx://config/addon/pinyin/dictmanager", argv)
            self.assertEqual(argv[-1], "<@a{sv} {}>")

    def test_settings_refresh_targets_addon_not_global_config(self):
        self.window.session_actions = True
        with patch("settings.app.QMessageBox.question", return_value=QMessageBox.Yes), \
             patch("settings.app.shutil.which", return_value="/usr/bin/gdbus"), \
             patch("settings.app.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, "(true,)", ""), subprocess.CompletedProcess([], 0, "()", "")]) as run:
            self.window.reload_input_method()
            deadline = time.monotonic() + 5
            while self.window.job is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNone(self.window.job)
            self.assertEqual(run.call_args.args[0][-2:], ["org.fcitx.Fcitx.Controller1.ReloadAddonConfig", "transime"])

    def test_synthetic_dictionary_preview(self):
        sample = Path(__file__).resolve().parents[1] / "examples/dictionaries/academic-demo.csv"
        report = self.window.dictionary.preview_file(sample)
        self.assertGreater(report["count"], 0)
        self.assertFalse(report["translator_integration"])
        self.assertFalse((self.root / "data").exists())

    def test_background_job_releases_before_callback(self):
        results = []
        self.window.run_job(lambda: {"done": True}, lambda value: results.append((value, self.window.job)))
        deadline = time.monotonic() + 5
        while self.window.job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.window.job)
        self.assertEqual(results, [({"done": True}, None)])
        self.assertTrue(self.window.save_button.isEnabled())

    @unittest.skipUnless(shutil.which("libime_pinyindict"), "requires real LibIME compiler")
    def test_preview_confirm_import_populates_list(self):
        sample = Path(__file__).resolve().parents[1] / "examples/dictionaries/academic-demo.csv"
        report = self.window.dictionary.preview_file(sample)
        def accept_preview():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            dialog.accept()
        QTimer.singleShot(50, accept_preview)
        self.window.preview_dictionary(str(sample), report)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.processEvents()
            if self.window.pack_list.count() and self.window.job is None:
                break
            time.sleep(0.01)
        self.assertEqual(self.window.pack_list.count(), 1)
        self.assertIsNone(self.window.job)
        self.assertIn("个人学习词库未读取或修改", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
