import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("numbers_guard", Path(__file__).parents[1] / "worker/numbers.py")
numbers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(numbers)


class NumberFidelityTests(unittest.TestCase):
    def test_cardinal_format(self):
        self.assertEqual(numbers.normalize_numbers("总计 24 项。", "Twenty-four items in total."), "24 items in total.")

    def test_percent_format(self):
        self.assertEqual(numbers.normalize_numbers("增加了 3.75%。", "It increased by 3.75 per cent."), "It increased by 3.75%.")

    def test_repeated_values_and_precision(self):
        self.assertEqual(numbers.normalize_numbers("0.50 和 0.5", "0.5 and 0.5"), "0.50 and 0.5")

    def test_leading_zero_identifier_preserved(self):
        self.assertEqual(numbers.normalize_numbers("编号 007", "Number 7"), "Number 007")

    def test_numeric_value_not_repaired(self):
        for output in ("There are 8.", "There are nine.", "There are 7 and 7.", "There are none."):
            with self.subTest(output=output), self.assertRaises(ValueError):
                numbers.normalize_numbers("有 7 个。", output)

    def test_added_spelled_quantity_rejected(self):
        with self.assertRaises(ValueError):
            numbers.normalize_numbers("有2个", "There are two items and three backups.")

    def test_percent_semantics_not_added(self):
        with self.assertRaises(ValueError):
            numbers.normalize_numbers("有 8%。", "There are 8.")

    def test_chinese_numerals_untouched(self):
        self.assertEqual(numbers.normalize_numbers("有两个。", "There are two."), "There are two.")

    def test_negative_and_thousands(self):
        self.assertEqual(numbers.normalize_numbers("-4 到 1,200", "From -4 to one thousand two hundred"), "From -4 to 1,200")

    def test_ambiguous_or_unsupported_cardinal_is_not_invented(self):
        self.assertIsNone(numbers.cardinal_value("one and two"))
        self.assertIsNone(numbers.cardinal_value("twenty twelve"))
        self.assertIsNone(numbers.cardinal_value("one hundred hundred"))
        with self.assertRaises(ValueError):
            numbers.normalize_numbers("1.5", "one and a half")


if __name__ == "__main__":
    unittest.main()
