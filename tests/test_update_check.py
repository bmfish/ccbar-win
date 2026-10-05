"""is_newer_version 语义化比较单测（与 macOS 版 UpdateChecker 同口径）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from update_check import is_newer_version  # noqa: E402


class TestIsNewerVersion(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(is_newer_version("1.5.1", "1.5.0"))
        self.assertTrue(is_newer_version("v1.6", "1.5.9"))
        self.assertTrue(is_newer_version("2.0", "1.9.9"))
        self.assertTrue(is_newer_version("V2.0", "1.9.9"))

    def test_not_newer(self):
        self.assertFalse(is_newer_version("1.5.0", "1.5.0"))
        self.assertFalse(is_newer_version("1.4.9", "1.5"))
        self.assertFalse(is_newer_version("", "1.5.0"))
        self.assertFalse(is_newer_version(None, "1.5.0"))

    def test_padding(self):
        self.assertTrue(is_newer_version("1.5", "1.4.9"))
        self.assertTrue(is_newer_version("1.5.0.1", "1.5"))

    def test_non_numeric_segments_count_as_zero(self):
        # 与 macOS 版 Int($0) ?? 0 同口径：含后缀的段按 0 处理
        self.assertFalse(is_newer_version("1.5.1-beta", "1.5.0"))


if __name__ == "__main__":
    unittest.main()
