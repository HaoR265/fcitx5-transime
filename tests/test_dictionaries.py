"""Synthetic-only dictionary checks. Run inside the designated test VM."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "settings" / "dictionaries.py"
spec = importlib.util.spec_from_file_location("transime_dictionaries", MODULE)
dictionaries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dictionaries)
DictionaryManager = dictionaries.DictionaryManager
DictionaryError = dictionaries.DictionaryError


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = DictionaryManager(self.root / "fcitx5")

    def source(self, text="词库 ci'ku 0\n", suffix=".txt"):
        path = self.root / ("input" + suffix)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def fake_compile(args, **kwargs):
        # A lossless fake is only for transaction tests, not proof of LibIME support.
        shutil.copyfile(args[-2], args[-1])
        return subprocess.CompletedProcess(args, 0)

    def test_preview_formats_and_metadata(self):
        result = self.manager.preview_file(self.source("word,pinyin,english\n词库,ci'ku,dictionary\n", ".csv"))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["entries"][0]["english"], "dictionary")
        self.assertFalse(result["translator_integration"])
        result = self.manager.preview_file(self.source("word\tpinyin\tscore\n绿色\tlü'se\t4\n", ".tsv"))
        self.assertEqual(result["entries"][0]["pinyin"], "lv'se")
        self.assertFalse(self.manager.data_home.exists())

    def test_bad_entry_aborts_instead_of_skipping(self):
        for content in ("词库 ci'ku 0\n错误 wrong 0\n", "词库 ciku 0\n", "词库 ci''ku 0\n",
                        "词库 ci'ku -1\n", "词库 ci'ku 0\n词库 ci'ku 5\n", "词库 ci 0\n"):
            with self.subTest(content=content), self.assertRaises(DictionaryError):
                self.manager.preview_file(self.source(content))

    def test_invalid_csv(self):
        for content in ("word,pinyin,word\na,b,c\n", "word,pinyin\n词库,ci'ku,unexpected\n",
                        "word,pinyin\n词库\n", "word,pinyin\n"):
            with self.subTest(content=content), self.assertRaises(DictionaryError):
                self.manager.preview_file(self.source(content, ".csv"))

    def test_limits_and_encoding(self):
        with patch.object(dictionaries, "MAX_BYTES", 2), self.assertRaises(DictionaryError):
            self.manager.preview_file(self.source())
        with patch.object(dictionaries, "MAX_ENTRIES", 1), self.assertRaises(DictionaryError):
            self.manager.preview_file(self.source("词库 ci'ku 0\n模型 mo'xing 0\n"))
        source = self.source()
        source.write_bytes(b"\xff\xfe")
        with self.assertRaises(DictionaryError):
            self.manager.preview_file(source)

    def test_missing_tool_makes_no_install(self):
        with patch.object(dictionaries.shutil, "which", return_value=None), self.assertRaisesRegex(DictionaryError, "libime_pinyindict"):
            self.manager.import_file(self.source(), "测试")
        self.assertFalse(self.manager.data_home.exists())

    def test_changed_source_after_preview_rejected(self):
        source = self.source()
        preview = self.manager.preview_file(source)
        source.write_text("模型 mo'xing 0\n", encoding="utf-8")
        with self.assertRaisesRegex(DictionaryError, "预览后发生变化"):
            self.manager.import_file(source, "测试", expected_sha256=preview["sha256"])
        self.assertFalse(self.manager.data_home.exists())

    def test_data_home_symlink_rejected(self):
        other = self.root / "other-home"
        other.mkdir()
        self.manager.data_home.symlink_to(other, target_is_directory=True)
        with patch.object(dictionaries.shutil, "which", return_value="fake"), self.assertRaises(DictionaryError):
            self.manager.import_file(self.source(), "测试")
        self.assertEqual(list(other.iterdir()), [])

    def test_full_lifecycle_preserves_unrelated_files(self):
        # These are invented sentinel bytes, never read from the user's directories.
        pinyin = self.manager.data_home / "pinyin"
        pinyin.mkdir(parents=True)
        sentinel = pinyin / "user.dict"
        sentinel.write_bytes(b"synthetic-personal-dictionary-sentinel")
        with patch.object(dictionaries.shutil, "which", return_value="/fake/libime_pinyindict"), patch.object(dictionaries.subprocess, "run", side_effect=self.fake_compile):
            imported = self.manager.import_file(self.source(), "Literal $(touch nowhere)")
        self.assertEqual(self.manager.list_packs()[0]["name"], "Literal $(touch nowhere)")
        exported = self.root / "export.tsv"
        self.manager.export_pack(imported["id"], exported)
        self.assertEqual(self.manager.preview_file(exported)["count"], 1)
        with self.assertRaises(DictionaryError):
            self.manager.export_pack(imported["id"], exported)
        self.manager.remove_pack(imported["id"])
        self.assertEqual(self.manager.list_packs(), [])
        self.assertEqual(sentinel.read_bytes(), b"synthetic-personal-dictionary-sentinel")

    def test_tampered_binary_and_path_traversal_rejected(self):
        with patch.object(dictionaries.shutil, "which", return_value="fake"), patch.object(dictionaries.subprocess, "run", side_effect=self.fake_compile):
            imported = self.manager.import_file(self.source(), "测试")
        binary = Path(imported["path"])
        binary.write_bytes(b"external-change")
        with self.assertRaises(DictionaryError):
            self.manager.remove_pack(imported["id"])
        self.assertTrue(binary.exists())
        self.assertEqual(self.manager.list_packs()[0]["status"], "error")
        with self.assertRaises(DictionaryError):
            self.manager.remove_pack("../../user.dict")

    def test_roundtrip_mismatch_leaves_no_pack(self):
        def lossy(args, **kwargs):
            Path(args[-1]).write_text("模型 mo'xing 0\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        with patch.object(dictionaries.shutil, "which", return_value="fake"), patch.object(dictionaries.subprocess, "run", side_effect=lossy), self.assertRaises(DictionaryError):
            self.manager.import_file(self.source(), "测试")
        self.assertEqual(self.manager.list_packs(), [])
        self.assertEqual(list(self.manager.target.iterdir()), [])

    def test_symlink_destination_rejected(self):
        self.manager.data_home.mkdir()
        other = self.root / "elsewhere"
        other.mkdir()
        (self.manager.data_home / "pinyin").symlink_to(other, target_is_directory=True)
        with patch.object(dictionaries.shutil, "which", return_value="fake"), self.assertRaises(DictionaryError):
            self.manager.import_file(self.source(), "测试")
        self.assertEqual(list(other.iterdir()), [])

    @unittest.skipUnless(shutil.which("libime_pinyindict"), "VM integration needs libime_pinyindict")
    def test_real_libime_roundtrip(self):
        imported = self.manager.import_file(self.source("词库 ci'ku 0\n模型 mo'xing 12\n"), "合成集成测试")
        self.assertEqual(imported["count"], 2)
        self.assertEqual(self.manager.list_packs()[0]["status"], "ready")
        self.manager.remove_pack(imported["id"])


if __name__ == "__main__":
    unittest.main()
