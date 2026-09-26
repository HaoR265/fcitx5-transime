"""Synthetic config editing tests; execute only in the designated test VM."""
import importlib.util
import multiprocessing
from pathlib import Path
from queue import Empty
import stat
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("transime_settings_configuration", Path(__file__).resolve().parents[1] / "settings" / "configuration.py")
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)
SettingsConfig = configuration.SettingsConfig
SettingsError = configuration.SettingsError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "conf" / "transime.conf"

    def put(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_defaults_and_atomic_private_save(self):
        config = SettingsConfig(self.path)
        self.assertFalse(config.values["Enabled"])
        self.assertFalse(config.values["ShowModeHints"])
        self.assertEqual(config.keylists["RawCommitKey"], ["Return", "KP_Enter"])
        report = config.save({"Enabled": True}, {})
        self.assertIsNone(report["backup_path"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(SettingsConfig(self.path).values["Enabled"])

    def test_preserve_unknown_sections_options_and_extra_keys(self):
        original = "# heading\nEnabled=False\nModelDirectory=/synthetic/model\n\n[TranslateKey]\n0=Control+Return\n4=Alt+F8\nVendorField=keep this\n\n[VendorSection]\nFoo=bar\n"
        self.put(original)
        config = SettingsConfig(self.path)
        self.assertEqual(config.keylists["TranslateKey"], ["Control+Return", "Alt+F8"])
        report = config.save({"ShowModeHints": True}, {})
        actual = self.path.read_text(encoding="utf-8")
        self.assertIn("ModelDirectory=/synthetic/model", actual)
        self.assertIn("VendorField=keep this", actual)
        self.assertIn("[VendorSection]\nFoo=bar", actual)
        self.assertEqual(SettingsConfig(self.path).keylists["TranslateKey"], ["Control+Return", "Alt+F8"])
        self.assertEqual(Path(report["backup_path"]).read_text(encoding="utf-8"), original)
        self.assertEqual(stat.S_IMODE(Path(report["backup_path"]).stat().st_mode), 0o600)

    def test_external_change_is_not_overwritten(self):
        self.put("Enabled=False\n")
        config = SettingsConfig(self.path)
        self.put("Enabled=True\n# external edit\n")
        with self.assertRaises(SettingsError):
            config.save({}, {})
        self.assertIn("# external edit", self.path.read_text())

    def test_external_creation_and_removal_detected(self):
        config = SettingsConfig(self.path)
        self.put("Enabled=True\n")
        with self.assertRaises(SettingsError):
            config.save({}, {})
        config = SettingsConfig(self.path)
        self.path.unlink()
        with self.assertRaises(SettingsError):
            config.save({}, {})

    def test_concurrent_settings_saves_do_not_silently_overwrite(self):
        self.put("Enabled=False\nShowModeHints=False\n")
        first = SettingsConfig(self.path)
        second = SettingsConfig(self.path)
        context = multiprocessing.get_context("fork")
        replacing = context.Event()
        second_started = context.Event()
        second_done = context.Event()
        failures = context.Queue()
        original_replace = configuration.os.replace

        def delayed_replace(source, destination):
            if multiprocessing.current_process().name == "first-settings-save":
                replacing.set()
                self.assertTrue(second_started.wait(5))
                # Without a lock, the second save finishes while the first
                # is paused after its final check, and is silently overwritten.
                second_done.wait(0.3)
            return original_replace(source, destination)

        def save_first():
            try:
                first.save({"Enabled": True}, {})
            except Exception as exc:
                failures.put(("first", type(exc).__name__, str(exc)))

        def save_second():
            second_started.set()
            try:
                second.save({"ShowModeHints": True}, {})
            except Exception as exc:
                failures.put(("second", type(exc).__name__, str(exc)))
            finally:
                second_done.set()

        with mock.patch.object(configuration.os, "replace", delayed_replace):
            one = context.Process(target=save_first, name="first-settings-save")
            two = context.Process(target=save_second, name="second-settings-save")
            one.start()
            self.assertTrue(replacing.wait(5))
            two.start()
            one.join(5)
            two.join(5)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual(one.exitcode, 0)
        self.assertEqual(two.exitcode, 0)
        errors = []
        while True:
            try:
                errors.append(failures.get_nowait())
            except Empty:
                break
        failures.close()
        failures.join_thread()
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0][0], "second")
        self.assertEqual(errors[0][1], "SettingsError")
        loaded = SettingsConfig(self.path)
        self.assertTrue(loaded.values["Enabled"])
        self.assertFalse(loaded.values["ShowModeHints"])

    def test_failed_replace_releases_lock_and_preserves_config(self):
        original = "Enabled=False\n"
        self.put(original)
        config = SettingsConfig(self.path)
        with mock.patch.object(configuration.os, "replace", side_effect=OSError("synthetic replacement failure")):
            with self.assertRaises(SettingsError):
                config.save({"Enabled": True}, {})
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.glob(".transime.conf-*")), [])
        config.save({"Enabled": True}, {})
        self.assertTrue(SettingsConfig(self.path).values["Enabled"])

    def test_normalized_duplicates_rejected(self):
        config = SettingsConfig(self.path)
        with self.assertRaises(SettingsError):
            config.save({}, {"TranslateKey": ["Ctrl+a"], "ToggleKey": ["Control+A"]})
        self.assertFalse(self.path.exists())

    def test_control_shift_letter_is_distinct(self):
        config = SettingsConfig(self.path)
        config.save({}, {"TranslateKey": ["Ctrl+a"], "ToggleKey": ["Control+Shift+a"]})
        loaded = SettingsConfig(self.path)
        self.assertEqual(loaded.keylists["TranslateKey"], ["Control+A"])
        self.assertEqual(loaded.keylists["ToggleKey"], ["Control+Shift+A"])

    def test_unsafe_shortcuts_rejected(self):
        config = SettingsConfig(self.path)
        for key in ("a", "Shift+a", "Left", "Shift+BackSpace", "Control+space", "Control+Shift+space", "Shift", "Bogus+F2"):
            with self.subTest(key=key), self.assertRaises(SettingsError):
                config.save({}, {"ToggleKey": [key]})

    def test_required_keys_and_allowed_swaps(self):
        config = SettingsConfig(self.path)
        for name in configuration.REQUIRED_KEYS:
            with self.subTest(name=name), self.assertRaises(SettingsError):
                config.save({}, {name: []})
        config.save({}, {"ChineseCommitKey": ["Return"], "RawCommitKey": ["space"], "ToggleKey": ["Alt+F8"]})
        loaded = SettingsConfig(self.path)
        self.assertEqual(loaded.keylists["ChineseCommitKey"], ["Return"])
        self.assertEqual(loaded.keylists["RawCommitKey"], ["space"])

    def test_empty_optional_list_persists(self):
        config = SettingsConfig(self.path)
        config.save({}, {"TranslateKey": [], "ClearHistoryKey": []})
        self.assertEqual(SettingsConfig(self.path).keylists["TranslateKey"], [])

    def test_no_final_newline_and_unknown_data(self):
        self.put("Unknown=unchanged")
        SettingsConfig(self.path).save({}, {})
        self.assertIn("Unknown=unchanged\n", self.path.read_text())

    def test_symlink_rejected(self):
        target = Path(self.tmp.name) / "outside.conf"
        target.write_text("Enabled=False\n")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(SettingsError):
            SettingsConfig(self.path)


if __name__ == "__main__":
    unittest.main()
