"""界面语言词条表（与 macOS 版 L10n.swift 同款机制）。

注意：这里不读设置文件——语言偏好由 app_settings 存，启动时调 set_language()。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l10n  # noqa: E402


class TestL10n(unittest.TestCase):
    def setUp(self):
        l10n.set_language("zh")

    def tearDown(self):
        l10n.set_language("zh")

    # ------------------------------------------------------------ 词条

    def test_dict_is_complete_and_clean(self):
        # macOS 版 L10n.swift 共 284 条；Windows 端另有 127 条独有文案（设置页新项、
        # 主题包管理、多机合并、周报目录、托盘菜单等），合并在同一个表里。
        self.assertGreaterEqual(len(l10n.L10N_EN), 284,
                                "词条不得少于 macOS 版（缺词条会静默回落中文）")
        for key in ("今日用量", "费用", "跟随系统", "连续使用", "上周周报已生成"):
            self.assertIn(key, l10n.L10N_EN, "macOS 版词条不得被覆盖删除")
        for key, value in l10n.L10N_EN.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, str)
            self.assertTrue(key.strip(), "中文原文当 key，不得为空")
            self.assertTrue(value.strip(), f"词条 {key!r} 的英文为空")
            self.assertNotEqual(key, value, f"词条 {key!r} 中英一致，疑似漏译")

    def test_l_falls_back_to_chinese(self):
        """缺词条回落中文——允许渐进式翻译"""
        self.assertEqual(l10n.L("这个键肯定不存在"), "这个键肯定不存在")
        self.assertEqual(l10n.L("今日用量"), "今日用量")

    def test_l_returns_english_when_enabled(self):
        l10n.set_language("en")
        self.assertTrue(l10n.is_english())
        self.assertEqual(l10n.L("今日用量"), "Today")
        self.assertEqual(l10n.L("费用"), "Cost")
        # 未翻译的键仍然回落中文，不能变成空串
        self.assertEqual(l10n.L("这个键肯定不存在"), "这个键肯定不存在")

    def test_resolves_to_english(self):
        self.assertTrue(l10n.resolves_to_english("en"))
        self.assertFalse(l10n.resolves_to_english("zh"))
        self.assertIn(l10n.resolves_to_english("system"), (True, False))

    def test_language_display_name(self):
        self.assertEqual(l10n.language_display_name("zh"), "中文")
        self.assertEqual(l10n.language_display_name("en"), "English")
        self.assertEqual(l10n.language_display_name("system"), "跟随系统")

    # ------------------------------------------------------------ 数字口径

    def test_format_tokens_chinese(self):
        """中文：>=1亿 两位小数的亿；>=1万 整数万（截断，不带小数）；否则原样"""
        self.assertEqual(l10n.format_tokens(9_999), "9999")
        self.assertEqual(l10n.format_tokens(10_000), "1万")
        self.assertEqual(l10n.format_tokens(19_999), "1万")
        self.assertEqual(l10n.format_tokens(99_999_999), "9999万")
        self.assertEqual(l10n.format_tokens(100_000_000), "1.00亿")
        self.assertEqual(l10n.format_tokens(123_456_789), "1.23亿")
        self.assertEqual(l10n.format_tokens(0), "0")

    def test_format_tokens_english(self):
        """英文：B / M / K（与 macOS 版逐字一致）"""
        l10n.set_language("en")
        self.assertEqual(l10n.format_tokens(9_999), "9999")
        self.assertEqual(l10n.format_tokens(10_000), "10.0K")
        self.assertEqual(l10n.format_tokens(999_999), "1000.0K")
        self.assertEqual(l10n.format_tokens(1_000_000), "1.00M")
        self.assertEqual(l10n.format_tokens(1_000_000_000), "1.00B")
        self.assertEqual(l10n.format_tokens(123_456_789), "123.46M")


if __name__ == "__main__":
    unittest.main()
