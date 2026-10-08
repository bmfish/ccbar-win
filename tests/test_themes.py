"""主题系统：内置 6 套 + JSON 主题包导入导出 + tkinter 令牌换算。

色值全部对照 macOS 版 Design.swift 的内置主题（id 即显示名，老偏好无需迁移）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import themes  # noqa: E402


class TestBuiltinThemes(unittest.TestCase):
    def test_six_builtins(self):
        self.assertEqual([t["id"] for t in themes.BUILTIN_THEMES],
                         ["默认主题", "卡哇伊 01", "海蓝", "翠绿", "星空紫", "CRT 终端"])
        names = [t["name"] for t in themes.BUILTIN_THEMES]
        self.assertEqual(len(set(names)), 6, "主题名不得重复（同名会被当成同一套）")

    def test_builtin_integrity(self):
        """每套主题：6 个模型色、3 个趋势色、色值合法、alpha 在 0~1"""
        for theme in themes.BUILTIN_THEMES:
            with self.subTest(theme=theme["id"]):
                self.assertEqual(len(theme["models"]), 6)
                self.assertEqual(len(theme["trend"]), 3)
                for key in ("accent", "data", "trend", "models"):
                    values = theme[key] if isinstance(theme[key], list) else [theme[key]]
                    for v in values:
                        self.assertTrue(themes.is_valid_hex(v), f"{key}={v} 不是合法色值")
                if theme["bigNumber"]:
                    self.assertTrue(themes.is_valid_hex(theme["bigNumber"]))
                for key in ("glowAlpha", "cardFillAlpha", "cardBorderAlpha", "separatorAlpha"):
                    self.assertGreaterEqual(theme[key], 0.0)
                    self.assertLessEqual(theme[key], 1.0)
                self.assertIn(theme["bigNumberWeight"], ("bold", "heavy"))

    def test_classic_and_crt_values_match_macos(self):
        """抽查两套：默认主题与 CRT 终端（CRT 是唯一带扫描线的）"""
        classic = themes.find("默认主题")
        self.assertEqual(classic["accent"], "#E86E45")
        self.assertEqual(classic["data"], "#F2B373")
        self.assertEqual(classic["bigNumber"], "", "空 = 跟随 accent")
        self.assertEqual(classic["models"][0], "#E86E45")
        self.assertFalse(classic["scanlines"])

        crt = themes.find("CRT 终端")
        self.assertEqual(crt["accent"], "#4DF28C")
        self.assertTrue(crt["scanlines"])
        self.assertEqual(crt["cardBorderAlpha"], 0.20)

    def test_find_falls_back_to_default(self):
        self.assertEqual(themes.find("不存在的主题")["id"], "默认主题")


class TestHexTools(unittest.TestCase):
    def test_parse_hex(self):
        self.assertEqual(themes.parse_hex("#E86E45"), (232, 110, 69))
        self.assertEqual(themes.parse_hex("E86E45"), (232, 110, 69))
        self.assertEqual(themes.parse_hex("#FFF"), (255, 255, 255))
        self.assertEqual(themes.parse_hex("#abc"), (170, 187, 204))

    def test_parse_hex_invalid_returns_white(self):
        """非法色值回落白色，绝不抛异常（mac 版同款行为）"""
        for bad in ("", "zzz", "#12345", None, 123, "#GGGGGG"):
            self.assertEqual(themes.parse_hex(bad), (255, 255, 255))

    def test_blend(self):
        self.assertEqual(themes.blend("#FFFFFF", "#000000", 0.5), "#808080")
        self.assertEqual(themes.blend("#FFFFFF", "#000000", 1.0), "#FFFFFF")
        self.assertEqual(themes.blend("#FFFFFF", "#000000", 0.0), "#000000")
        self.assertEqual(themes.blend("#FFFFFF", "#000000", 5.0), "#FFFFFF", "alpha 必须夹紧")

    def test_card_fill_matches_current_windows_look(self):
        """默认主题叠出来的卡片色应与现有硬编码 #2A2A2A 基本一致（灰度差 <= 2）"""
        tokens = themes.design_tokens(themes.find("默认主题"), background="#1C1C1C")
        r, g, b = themes.parse_hex(tokens["CARD_FILL"])
        for got, want in ((r, 0x2A), (g, 0x2A), (b, 0x2A)):
            self.assertLessEqual(abs(got - want), 2, f"{tokens['CARD_FILL']} 与 #2A2A2A 偏差过大")


class TestDesignTokens(unittest.TestCase):
    def test_tokens_follow_theme(self):
        theme = themes.find("海蓝")
        tokens = themes.design_tokens(theme, background="#1C1C1C")
        self.assertEqual(tokens["BRAND"], theme["accent"])
        self.assertEqual(tokens["DATA"], theme["data"])
        self.assertEqual(tokens["BIG_NUMBER"], theme["bigNumber"])
        self.assertEqual(tokens["MODEL_COLORS"], theme["models"])
        self.assertEqual(tokens["TREND"]["yesterday"], theme["trend"][0])
        self.assertEqual(tokens["TREND"]["total"], theme["accent"])
        self.assertEqual(len(tokens["GRADIENT"]), 6)
        self.assertIn(theme["accent"], tokens["GRADIENT"])
        self.assertFalse(tokens["SCANLINES"])

    def test_big_number_falls_back_to_accent(self):
        tokens = themes.design_tokens(themes.find("默认主题"))
        self.assertEqual(tokens["BIG_NUMBER"], tokens["BRAND"])

    def test_all_tokens_are_valid_hex(self):
        for theme in themes.BUILTIN_THEMES:
            tokens = themes.design_tokens(theme)
            for key in ("BRAND", "DATA", "BIG_NUMBER", "CARD_FILL", "CARD_BORDER",
                        "SEPARATOR", "BTN_BG", "BTN_BG_HOVER", "ROW_HOVER"):
                self.assertTrue(themes.is_valid_hex(tokens[key]),
                                f"{theme['id']} 的 {key}={tokens[key]}")


class TestThemePack(unittest.TestCase):
    def test_json_round_trip(self):
        """导出 → 导入：字段一致，id 重新发（自定义主题包语义）"""
        original = themes.find("星空紫")
        text = themes.theme_to_json(original)
        payload = json.loads(text)
        self.assertEqual(payload["format"], "ccbar-theme")
        self.assertEqual(payload["version"], 1)

        back = themes.theme_from_json(text)
        for key in ("name", "accent", "data", "bigNumber", "trend", "models",
                    "glowRadius", "glowAlpha", "cardFillAlpha", "cardBorderAlpha",
                    "separatorAlpha", "bigNumberWeight", "scanlines"):
            self.assertEqual(back[key], original[key], f"字段 {key} 往返后不一致")

    def test_json_rejects_foreign_pack(self):
        with self.assertRaises(ValueError):
            themes.theme_from_json('{"format": "other-theme", "name": "x"}')
        with self.assertRaises(ValueError):
            themes.theme_from_json("不是 json")
        with self.assertRaises(ValueError):
            themes.theme_from_json("[1, 2, 3]")

    def test_json_tolerates_missing_and_broken_fields(self):
        """手改主题包也要尽量能读：缺字段回落默认主题，trend/models 数量不对则修正"""
        back = themes.theme_from_json('{"format": "ccbar-theme", "name": "手改的"}')
        self.assertEqual(back["name"], "手改的")
        self.assertEqual(back["accent"], themes.find("默认主题")["accent"])
        self.assertEqual(len(back["trend"]), 3)
        self.assertEqual(len(back["models"]), 6)

        broken = themes.theme_from_json(json.dumps({
            "format": "ccbar-theme", "name": "坏值", "trend": ["#FFF"],
            "models": [], "glowAlpha": "很透明", "bigNumberWeight": "fat"}))
        self.assertEqual(len(broken["trend"]), 3)
        self.assertEqual(len(broken["models"]), 6)
        self.assertEqual(broken["glowAlpha"], 0.30)
        self.assertEqual(broken["bigNumberWeight"], "bold")

    def test_pack_without_format_is_accepted(self):
        """没写 format 的裸字段包也接受（宽容导入）"""
        back = themes.theme_from_json('{"name": "裸包", "accent": "#112233"}')
        self.assertEqual(back["accent"], "#112233")


class TestCustomThemes(unittest.TestCase):
    def test_upsert_dedupes_by_name_and_keeps_id(self):
        custom = []
        first = themes.theme_from_json('{"format":"ccbar-theme","name":"我的","accent":"#111111"}')
        custom = themes.upsert_custom(custom, first)
        self.assertEqual(len(custom), 1)
        original_id = custom[0]["id"]

        # 同名再导入 → 覆盖，沿用原 id，不新增
        again = themes.theme_from_json('{"format":"ccbar-theme","name":"我的","accent":"#222222"}')
        custom = themes.upsert_custom(custom, again)
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["id"], original_id)
        self.assertEqual(custom[0]["accent"], "#222222")

        # 不同名 → 新增
        other = themes.theme_from_json('{"format":"ccbar-theme","name":"另一个"}')
        custom = themes.upsert_custom(custom, other)
        self.assertEqual(len(custom), 2)

    def test_rename_and_delete(self):
        custom = themes.upsert_custom(
            [], themes.theme_from_json('{"format":"ccbar-theme","name":"旧名"}'))
        tid = custom[0]["id"]
        custom = themes.rename_custom(custom, tid, "新名")
        self.assertEqual(custom[0]["name"], "新名")
        self.assertTrue(themes.is_custom(tid, custom))

        custom = themes.delete_custom(custom, tid)
        self.assertEqual(custom, [])

    def test_is_custom_distinguishes_builtins(self):
        self.assertFalse(themes.is_custom("默认主题"))
        self.assertFalse(themes.is_custom("CRT 终端", []))

    def test_all_themes_puts_custom_last(self):
        custom = themes.upsert_custom(
            [], themes.theme_from_json('{"format":"ccbar-theme","name":"自定义"}'))
        listing = themes.all_themes(custom)
        self.assertEqual(len(listing), 7)
        self.assertEqual(listing[-1]["name"], "自定义")
        self.assertEqual(listing[0]["id"], "默认主题")


if __name__ == "__main__":
    unittest.main()
