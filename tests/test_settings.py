"""Settings（JSON 设置存储）单元测试：默认值、旧 txt 迁移、损坏回落、校验与落盘。

运行：python3 -m unittest discover -s tests -v
全程注入临时目录，绝不碰真实 ~/.ccbar。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_settings import DEFAULT_PATHS, DEFAULTS, SOURCE_ORDER, Settings  # noqa: E402


class SettingsTestCase(unittest.TestCase):
    """公共夹具：每个用例一个临时目录，settings.json / settings.txt 都写在这里"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-settings-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def json_path(self):
        return os.path.join(self.tmp, "settings.json")

    def legacy_path(self):
        return os.path.join(self.tmp, "settings.txt")

    def write_legacy(self, text):
        with open(self.legacy_path(), "w", encoding="utf-8") as f:
            f.write(text)

    def write_json(self, text):
        with open(self.json_path(), "w", encoding="utf-8") as f:
            f.write(text)

    def read_json(self):
        with open(self.json_path(), encoding="utf-8") as f:
            return json.load(f)

    def read_json_text(self):
        with open(self.json_path(), encoding="utf-8") as f:
            return f.read()


class TestDefaults(SettingsTestCase):
    def test_missing_file_falls_back_to_defaults(self):
        """文件不存在 → 全默认值，且不主动写盘"""
        s = Settings(self.tmp)
        self.assertEqual(s.get("refresh_interval"), 60)
        self.assertEqual(s.get("warning_threshold"), 50)
        self.assertEqual(s.get("notify_interval"), 1000)
        self.assertTrue(s.get("warning_enabled"))
        self.assertTrue(s.get("ccswitch_enabled"))
        self.assertFalse(s.get("zcode_enabled"))
        self.assertFalse(s.get("trae_enabled"))
        self.assertEqual(s.get("db_path"), DEFAULT_PATHS["ccswitch"])
        self.assertEqual(s.get("zcode_path"), DEFAULT_PATHS["zcode"])
        self.assertEqual(s.get("led_red_threshold"), 100)
        self.assertEqual(s.get("monthly_budget_usd"), 0.0)
        self.assertEqual(s.get("default_token_price"), 0.0)
        self.assertTrue(s.get("auto_weekly_report"))
        self.assertFalse(s.get("launch_at_login"))
        self.assertEqual(s.get("app_language"), "system")
        self.assertEqual(s.get("theme"), "默认主题")
        self.assertEqual(s.get("custom_themes"), [])
        self.assertEqual(s.get("insights_last_page"), "费用")
        self.assertEqual(s.get("trae_sessionid"), "")
        self.assertFalse(os.path.exists(self.json_path()))

    def test_required_keys_all_present(self):
        """KEY 清单里的每个键都必须有默认值"""
        required = ("refresh_interval", "warning_threshold", "warning_enabled", "notify_interval",
                    "db_path", "ccswitch_enabled", "zcode_enabled", "zcode_path", "trae_enabled",
                    "trae_sessionid", "led_red_threshold", "monthly_budget_usd",
                    "default_token_price", "auto_weekly_report", "launch_at_login",
                    "app_language", "theme", "custom_themes", "insights_last_page",
                    "last_auto_backup_date", "last_update_check_date")
        s = Settings(self.tmp)
        for key in required:
            self.assertIn(key, DEFAULTS, f"DEFAULTS 缺少 {key}")
            self.assertEqual(s.get(key), DEFAULTS[key])

    def test_mutable_defaults_are_not_shared(self):
        """custom_themes 这类 list 默认值必须深拷贝，实例之间互不污染"""
        a, b = Settings(self.tmp), Settings(self.tmp)
        a.set("custom_themes", [{"id": "custom-1"}])
        self.assertEqual(b.get("custom_themes"), [])
        self.assertEqual(DEFAULTS["custom_themes"], [])

    def test_file_path_injection_creates_parents(self):
        """path 传文件路径也认；save 自动建父目录"""
        target = os.path.join(self.tmp, "sub", "my-settings.json")
        s = Settings(target)
        s.set("theme", "海蓝")
        s.save()
        self.assertTrue(os.path.exists(target))
        self.assertEqual(Settings(target).get("theme"), "海蓝")


class TestLegacyMigration(SettingsTestCase):
    def test_legacy_settings_txt_migrates_all_keys(self):
        """旧 settings.txt 的 8 个键整体迁移，并写出 settings.json"""
        self.write_legacy("\n".join([
            "refresh_interval=120",
            "db_path=/tmp/cc-switch.db",
            "ccswitch_enabled=false",
            "zcode_enabled=true",
            "zcode_path=/tmp/zcode.sqlite",
            "warning_threshold=80",
            "warning_enabled=false",
            "notify_interval=500",
        ]))
        s = Settings(self.tmp)
        self.assertEqual(s.get("refresh_interval"), 120)
        self.assertEqual(s.get("db_path"), "/tmp/cc-switch.db")
        self.assertIs(s.get("ccswitch_enabled"), False)
        self.assertIs(s.get("zcode_enabled"), True)
        self.assertEqual(s.get("zcode_path"), "/tmp/zcode.sqlite")
        self.assertEqual(s.get("warning_threshold"), 80)
        self.assertIs(s.get("warning_enabled"), False)
        self.assertEqual(s.get("notify_interval"), 500)
        # 没在旧文件里的键保持默认；迁移后立刻落盘
        self.assertEqual(s.get("led_red_threshold"), 100)
        self.assertTrue(os.path.exists(self.json_path()))
        self.assertEqual(self.read_json()["refresh_interval"], 120)

    def test_malformed_legacy_lines_do_not_raise(self):
        """坏数值/坏行只跳过，不再像旧版 int(value) 那样把启动打崩"""
        self.write_legacy("\n".join([
            "refresh_interval=abc",
            "warning_threshold=",
            "notify_interval=5x",
            "zcode_enabled=maybe",
            "",
            "这行没有等号",
            "=空键",
            "unknown_key=1",
            "db_path=/tmp/x.db",
        ]))
        s = Settings(self.tmp)
        self.assertEqual(s.get("refresh_interval"), 60)     # 坏值 → 默认
        self.assertEqual(s.get("warning_threshold"), 50)
        self.assertEqual(s.get("notify_interval"), 1000)
        self.assertIs(s.get("zcode_enabled"), False)        # "maybe" 不是 true
        self.assertEqual(s.get("db_path"), "/tmp/x.db")     # 好行照常迁移
        self.assertNotIn("unknown_key", s)

    def test_out_of_range_legacy_value_keeps_default(self):
        """旧值超出校验范围时保留默认（0 秒刷新间隔会把定时器变成死循环）"""
        self.write_legacy("refresh_interval=0\nwarning_threshold=-3\n")
        s = Settings(self.tmp)
        self.assertEqual(s.get("refresh_interval"), 60)
        self.assertEqual(s.get("warning_threshold"), 50)

    def test_legacy_locale_encoded_file(self):
        """旧版在中文 Windows 上按 GBK 写盘：中文路径要能读出来（UTF-8 解不开就退回本地编码）"""
        with open(self.legacy_path(), "wb") as f:
            f.write("db_path=D:/用户/cc-switch.db\nrefresh_interval=90\n".encode("gbk"))
        s = Settings(self.tmp)
        self.assertEqual(s.get("db_path"), "D:/用户/cc-switch.db")
        self.assertEqual(s.get("refresh_interval"), 90)

    def test_migration_runs_only_once(self):
        """settings.json 一旦存在，就不再回读旧 txt"""
        self.write_legacy("refresh_interval=120\n")
        self.assertEqual(Settings(self.tmp).get("refresh_interval"), 120)
        self.assertTrue(os.path.exists(self.json_path()))
        # 改旧文件后重新打开：仍读 JSON，不被旧值覆盖
        self.write_legacy("refresh_interval=999\n")
        self.assertEqual(Settings(self.tmp).get("refresh_interval"), 120)

    def test_no_legacy_no_migration_file(self):
        """两个文件都没 → 不写盘、不报错"""
        s = Settings(self.tmp)
        self.assertEqual(s.get("refresh_interval"), 60)
        self.assertFalse(os.path.exists(self.json_path()))


class TestPersistence(SettingsTestCase):
    def test_save_reload_round_trip(self):
        """存盘再读回：数值保持类型，list 值原样回来"""
        themes = [{"id": "custom-1", "name": "暗夜", "accentHex": "#000000"}]
        s = Settings(self.tmp)
        s.set("refresh_interval", 120)
        s.set("monthly_budget_usd", 12.5)
        s.set("custom_themes", themes)
        s.save()

        again = Settings(self.tmp)
        self.assertEqual(again.get("refresh_interval"), 120)
        self.assertIsInstance(again.get("refresh_interval"), int)
        self.assertEqual(again.get("monthly_budget_usd"), 12.5)
        self.assertIsInstance(again.get("monthly_budget_usd"), float)
        self.assertEqual(again.get("custom_themes"), themes)

    def test_reload_discards_memory_changes(self):
        """reload() 丢掉内存里的未保存改动"""
        s = Settings(self.tmp)
        s.set("refresh_interval", 90)
        s.save()
        s.set("refresh_interval", 999)
        s.reload()
        self.assertEqual(s.get("refresh_interval"), 90)

    def test_unknown_keys_preserved(self):
        """文件里的未知键（未来版本/手工加的）在保存后仍在"""
        self.write_json(json.dumps({"future_key": {"a": 1}, "another": [1, 2],
                                    "null_key": None}, ensure_ascii=False))
        s = Settings(self.tmp)
        self.assertEqual(s.get("future_key"), {"a": 1})
        s.set("warning_threshold", 70)
        s.save()
        raw = self.read_json()
        self.assertEqual(raw["future_key"], {"a": 1})
        self.assertEqual(raw["another"], [1, 2])
        self.assertIsNone(raw["null_key"])
        self.assertEqual(raw["warning_threshold"], 70)

    def test_corrupt_json_falls_back_to_defaults(self):
        """损坏 JSON 一律回落默认，不抛异常"""
        for bad in ('{"refresh_interval": 12,', "[]", "not json at all",
                    '{"refresh_interval": "abc"}', ""):
            with self.subTest(bad=bad):
                self.write_json(bad)
                s = Settings(self.tmp)
                self.assertEqual(s.get("refresh_interval"), 60)
                self.assertEqual(s.get("warning_threshold"), 50)

    def test_partial_json_keeps_other_defaults(self):
        """部分字段的 JSON：只覆盖出现的键，其余仍默认"""
        self.write_json('{"db_path": "/tmp/only.db", "warning_enabled": false}')
        s = Settings(self.tmp)
        self.assertEqual(s.get("db_path"), "/tmp/only.db")
        self.assertIs(s.get("warning_enabled"), False)
        self.assertEqual(s.get("warning_threshold"), 50)
        self.assertEqual(s.get("custom_themes"), [])

    def test_bad_typed_json_value_falls_back(self):
        """类型不对的单个值丢弃（回落默认），不影响同文件里的好值"""
        self.write_json('{"led_red_threshold": "x", "notify_interval": 300, "custom_themes": 5}')
        s = Settings(self.tmp)
        self.assertEqual(s.get("led_red_threshold"), 100)
        self.assertEqual(s.get("notify_interval"), 300)
        self.assertEqual(s.get("custom_themes"), [])

    def test_json_is_pretty_utf8(self):
        """ensure_ascii=False + indent=2，中文不转义、便于人工查看"""
        s = Settings(self.tmp)
        s.set("theme", "海蓝")
        s.save()
        text = self.read_json_text()
        self.assertIn('"theme": "海蓝"', text)
        self.assertIn('\n  "refresh_interval": 60', text)


class TestAccess(SettingsTestCase):
    def test_dict_and_attribute_access(self):
        """dict 式与属性式读法都能用"""
        s = Settings(self.tmp)
        self.assertEqual(s["refresh_interval"], 60)
        self.assertEqual(s.refresh_interval, 60)
        self.assertIn("theme", s)
        self.assertIsNone(s["nope"])             # 未知键返回 None，不抛
        s["warning_threshold"] = "66"            # 字符串也能存成 int
        self.assertEqual(s.get("warning_threshold"), 66)
        self.assertEqual(s.as_dict()["theme"], "默认主题")
        with self.assertRaises(AttributeError):
            s.definitely_missing


class TestValidation(SettingsTestCase):
    LABELS = [
        ("refresh_interval", "刷新间隔（5 ~ 3000 秒）"),
        ("warning_threshold", "预警阈值（正整数，万）"),
        ("notify_interval", "通知间隔（≥ 0 的整数，万，0=关闭）"),
        ("led_red_threshold", "红色门槛（≥ 0 的整数，万，0=不变红）"),
        ("monthly_budget_usd", "月度预算（≥ 0 的数字，$，0=关闭）"),
        ("default_token_price", "默认单价（≥ 0 的数字，$/M tokens，0=关闭）"),
    ]

    def test_validate_reports_all_invalid_in_order(self):
        """6 个字段全非法 → 按 macOS 版顺序给出全部中文标签"""
        s = Settings(self.tmp)
        errors = s.validate({
            "refresh_interval": "abc",
            "warning_threshold": "0",
            "notify_interval": "-1",
            "led_red_threshold": -5,
            "monthly_budget_usd": "-1",
            "default_token_price": "x",
        })
        self.assertEqual(errors, [label for _, label in self.LABELS])

    def test_validate_each_field_label(self):
        """逐个字段的非法值 → 精确标签"""
        cases = [
            ("refresh_interval", 4), ("refresh_interval", 3001),
            ("refresh_interval", ""), ("refresh_interval", "12.5"),
            ("warning_threshold", 0), ("warning_threshold", -1),
            ("warning_threshold", "一万"),
            ("notify_interval", -1), ("notify_interval", "x"),
            ("led_red_threshold", -1), ("led_red_threshold", None),
            ("monthly_budget_usd", -0.1), ("monthly_budget_usd", "abc"),
            ("default_token_price", -1), ("default_token_price", "abc"),
        ]
        s = Settings(self.tmp)
        for key, value in cases:
            label = dict(self.LABELS)[key]
            with self.subTest(key=key, value=value):
                self.assertEqual(s.validate({key: value}), [label])

    def test_validate_accepts_boundaries(self):
        """边界值全通过：5/3000、阈值 1、0=关闭的几个字段"""
        s = Settings(self.tmp)
        self.assertEqual(s.validate({"refresh_interval": 5}), [])
        self.assertEqual(s.validate({"refresh_interval": "3000"}), [])
        self.assertEqual(s.validate({"warning_threshold": 1}), [])
        self.assertEqual(s.validate({"notify_interval": 0}), [])
        self.assertEqual(s.validate({"led_red_threshold": "0"}), [])
        self.assertEqual(s.validate({"monthly_budget_usd": 0}), [])
        self.assertEqual(s.validate({"default_token_price": "0"}), [])
        self.assertEqual(s.validate({"monthly_budget_usd": ""}), [])   # 空输入按 0（同 macOS）
        self.assertEqual(s.validate({"default_token_price": None}), [])
        self.assertEqual(s.validate({}), [])
        self.assertEqual(s.validate(None), [])

    def test_validate_ignores_non_numeric_keys(self):
        """非数值键（主题/布尔/列表）不参与数值校验"""
        s = Settings(self.tmp)
        self.assertEqual(s.validate({"theme": "海蓝", "warning_enabled": True,
                                     "custom_themes": [], "unknown": "x"}), [])


class TestApply(SettingsTestCase):
    def test_apply_coerces_and_saves(self):
        """合法输入：字符串转成 int/float 后写入并落盘"""
        s = Settings(self.tmp)
        self.assertEqual(s.apply({"refresh_interval": "120", "monthly_budget_usd": "12.5",
                                  "custom_themes": [{"id": "custom-1"}]}), [])
        self.assertEqual(s.get("refresh_interval"), 120)
        self.assertIsInstance(s.get("refresh_interval"), int)
        self.assertEqual(s.get("monthly_budget_usd"), 12.5)
        self.assertIsInstance(s.get("monthly_budget_usd"), float)
        self.assertEqual(self.read_json()["refresh_interval"], 120)
        self.assertEqual(Settings(self.tmp).get("monthly_budget_usd"), 12.5)

    def test_apply_empty_budget_stores_zero(self):
        """预算/单价留空 → 0.0（macOS 版空输入按 0）"""
        s = Settings(self.tmp)
        self.assertEqual(s.apply({"monthly_budget_usd": "", "default_token_price": ""}), [])
        self.assertEqual(s.get("monthly_budget_usd"), 0.0)
        self.assertEqual(s.get("default_token_price"), 0.0)

    def test_apply_invalid_changes_nothing(self):
        """一批里只要有一个非法：整批不写入、不落盘、返回错误列表"""
        s = Settings(self.tmp)
        s.set("refresh_interval", 90)
        s.save()
        errors = s.apply({"refresh_interval": "1", "warning_threshold": "70"})
        self.assertEqual(errors, ["刷新间隔（5 ~ 3000 秒）"])
        self.assertEqual(s.get("refresh_interval"), 90)      # 内存没被改
        self.assertEqual(s.get("warning_threshold"), 50)     # 同一批的合法值也不写
        self.assertEqual(Settings(self.tmp).get("refresh_interval"), 90)

    def test_apply_invalid_does_not_create_file(self):
        """非法输入不应产生 settings.json"""
        s = Settings(self.tmp)
        self.assertEqual(s.apply({"refresh_interval": "abc"}), ["刷新间隔（5 ~ 3000 秒）"])
        self.assertFalse(os.path.exists(self.json_path()))

    def test_apply_unknown_key_is_saved(self):
        """未知键也走 apply 落盘（向前兼容）"""
        s = Settings(self.tmp)
        self.assertEqual(s.apply({"future_key": 1}), [])
        self.assertEqual(Settings(self.tmp).get("future_key"), 1)


class TestReset(SettingsTestCase):
    def test_reset_restores_defaults(self):
        """reset() 与 macOS 版同口径：间隔 30、只留 ccswitch、阈值 50、预警开、不自启"""
        s = Settings(self.tmp)
        s.apply({"refresh_interval": "120", "warning_threshold": "80",
                 "monthly_budget_usd": "20", "default_token_price": "3",
                 "led_red_threshold": "5", "notify_interval": "10",
                 "auto_weekly_report": False, "trae_enabled": True})
        s.set("theme", "海蓝")
        s.set("ccswitch_enabled", False)
        s.set("zcode_enabled", True)
        s.set("launch_at_login", True)
        s.reset()

        self.assertEqual(s.get("refresh_interval"), 30)
        self.assertTrue(s.get("ccswitch_enabled"))
        self.assertFalse(s.get("zcode_enabled"))
        self.assertFalse(s.get("trae_enabled"))
        self.assertEqual(s.get("db_path"), DEFAULT_PATHS["ccswitch"])
        self.assertEqual(s.get("zcode_path"), DEFAULT_PATHS["zcode"])
        self.assertEqual(s.get("warning_threshold"), 50)
        self.assertTrue(s.get("warning_enabled"))
        self.assertFalse(s.get("launch_at_login"))
        self.assertEqual(s.get("led_red_threshold"), 100)
        self.assertEqual(s.get("monthly_budget_usd"), 0.0)
        self.assertEqual(s.get("default_token_price"), 0.0)
        self.assertTrue(s.get("auto_weekly_report"))
        self.assertEqual(s.get("notify_interval"), 1000)
        self.assertEqual(s.get("theme"), "默认主题")
        # 重置结果已落盘
        self.assertEqual(Settings(self.tmp).get("refresh_interval"), 30)


class TestSourceConfigs(SettingsTestCase):
    def test_order_paths_and_trae(self):
        """顺序固定 ccswitch/zcode/trae，路径用默认值，trae 是 HTTP 源（空路径）"""
        s = Settings(self.tmp)
        configs = s.source_configs()
        self.assertEqual([c[0] for c in configs], SOURCE_ORDER)
        self.assertEqual(SOURCE_ORDER, ["ccswitch", "zcode", "trae"])
        self.assertEqual(configs[0], ("ccswitch", True, DEFAULT_PATHS["ccswitch"]))
        self.assertEqual(configs[1], ("zcode", False, DEFAULT_PATHS["zcode"]))
        self.assertEqual(configs[2], ("trae", False, ""))
        self.assertEqual(s.enabled_sources(), ["ccswitch"])

    def test_empty_path_falls_back_to_default(self):
        """路径留空/纯空白 → 回落该源默认路径"""
        s = Settings(self.tmp)
        s.set("db_path", "   ")
        s.set("zcode_path", "")
        configs = s.source_configs()
        self.assertEqual(configs[0][2], DEFAULT_PATHS["ccswitch"])
        self.assertEqual(configs[1][2], DEFAULT_PATHS["zcode"])

    def test_custom_paths_and_enabled_sources(self):
        """自定义路径 + 启用开关；~ 会被展开"""
        s = Settings(self.tmp)
        s.apply({"db_path": "/tmp/my-cc-switch.db", "zcode_enabled": True, "trae_enabled": True})
        configs = s.source_configs()
        self.assertEqual(configs[0][2], "/tmp/my-cc-switch.db")
        self.assertEqual(s.enabled_sources(), ["ccswitch", "zcode", "trae"])
        s.set("zcode_path", "~/zcode/db.sqlite")
        self.assertEqual(s.source_configs()[1][2], os.path.expanduser("~/zcode/db.sqlite"))

    def test_trae_credentials_and_language(self):
        """Trae 凭据与语言键：非法语言回落 system"""
        s = Settings(self.tmp)
        s.apply({"trae_sessionid": "abc123", "app_language": "zh"})
        self.assertEqual(s.get("trae_sessionid"), "abc123")
        self.assertEqual(s.get("app_language"), "zh")
        s.apply({"app_language": "fr"})
        self.assertEqual(s.get("app_language"), "system")


if __name__ == "__main__":
    unittest.main()
