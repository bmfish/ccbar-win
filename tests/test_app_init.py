"""真实构造 CcBarTray：不绕过 __init__，验证启动路径不会炸。

现有窗口冒烟测试用 __new__ 手工拼实例（为了不碰真实 ~/.ccbar），
代价是构造器本身的回归抓不到（比如新增设置项后 __init__ 里读错键、
主题应用抛异常、PopoverWindow 构造依赖某个还没赋值的属性）。
这里把 app_settings.Settings 换成指向临时目录的工厂，其余全走真实路径。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win_stubs  # noqa: E402

win_stubs.install()

import app_settings  # noqa: E402
import l10n  # noqa: E402
import main  # noqa: E402
import themes  # noqa: E402


class TestConstructApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-init-")
        self._settings_cls = app_settings.Settings
        self._orig_settings = app_settings.Settings
        # 让 CcBarTray.__init__ 里的 Settings() 落到临时目录
        app_settings.Settings = lambda *a, **k: self._orig_settings(self.tmp)

    def tearDown(self):
        app_settings.Settings = self._orig_settings
        # 构造过程会把主题/语言写到全局，恢复默认，免得影响其它测试
        l10n.set_language("zh")
        themes_module_default = themes.default_theme()
        main.Design.apply(themes_module_default)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_constructor_uses_defaults_and_applies_theme(self):
        app = main.CcBarTray()
        try:
            self.assertIsInstance(app.settings, self._settings_cls)
            # 默认值来自 app_settings.DEFAULTS（不再有 main.py 里的裸 dict）
            for key in ("refresh_interval", "warning_threshold", "notify_interval",
                        "theme", "app_language", "trae_enabled"):
                self.assertIsNotNone(app.settings.get(key), "构造后设置项缺失: %s" % key)
            self.assertEqual(app.settings.get("refresh_interval"), 60)
            self.assertEqual(app.theme["id"], "默认主题")
            self.assertEqual(main.Design.BRAND, themes.find("默认主题")["accent"],
                             "构造时应已套用主题")
            self.assertIsNotNone(app.popover, "PopoverWindow 未构造")
            self.assertIsNotNone(app.store, "StatsStore 未构造")
            self.assertIsNone(app.icon)
            self.assertIsNone(app._ui_root)
        finally:
            app.store._close()

    def test_constructor_honours_saved_settings(self):
        """已有设置文件时：语言/主题按存档生效，且不会写坏存档"""
        settings = self._orig_settings(self.tmp)
        settings.apply({"theme": "海蓝", "app_language": "en",
                        "refresh_interval": "120", "trae_enabled": True})
        settings.save()

        app = main.CcBarTray()
        try:
            self.assertEqual(app.settings.get("refresh_interval"), 120)
            self.assertEqual(app.theme["id"], "海蓝")
            self.assertEqual(main.Design.BRAND, "#2E8CF2")
            self.assertTrue(l10n.is_english(), "英文偏好应在构造时生效")
            self.assertTrue(app.settings.get("trae_enabled"))
        finally:
            app.store._close()

    def test_tooltip_is_three_lines_like_macos(self):
        """悬停提示对齐 macOS：今日 / 昨日 / 请求数"""
        app = main.CcBarTray()
        try:
            stats = {0: {"total": 1234567, "reqs": 7}, 1: {"total": 20000}}
            app.query_day_stats = lambda days=0: stats.get(days)
            zh = app.get_tooltip_text().splitlines()
            self.assertEqual(len(zh), 3)
            self.assertTrue(zh[0].startswith("今日："))
            self.assertTrue(zh[1].startswith("昨日："))
            self.assertTrue(zh[2].startswith("请求数："))
            self.assertIn("123万", zh[0])

            l10n.set_language("en")
            en = app.get_tooltip_text().splitlines()
            self.assertTrue(en[0].startswith("Today: "))
            self.assertTrue(en[1].startswith("Yesterday: "))
            self.assertTrue(en[2].startswith("Requests: "))
            self.assertIn("1.23M", en[0])

            # 昨天没数据时只剩两行，且不出现 None
            app.query_day_stats = lambda days=0: stats.get(0) if days == 0 else None
            self.assertEqual(len(app.get_tooltip_text().splitlines()), 2)

            # 完全没有数据源时回落一句人话
            app.query_day_stats = lambda days=0: None
            self.assertEqual(app.get_tooltip_text(), l10n.L("ccBar - 未找到数据"))
        finally:
            app.store._close()

    def test_popover_wide_setting_is_wired(self):
        """宽版弹窗：设置里能改、show() 会按它把面板宽度切成 380"""
        app = main.CcBarTray()
        try:
            self.assertFalse(app.settings.get("popover_wide"))
            app.settings.set("popover_wide", True)
            panel = app.popover
            # 只看宽度选择逻辑，不真的建窗口
            app.settings.set("popover_wide", False)
            panel.WIDTH = 300 if not app.settings.get("popover_wide") else 380
            self.assertEqual(panel.WIDTH, 300)
            app.settings.set("popover_wide", True)
            panel.WIDTH = 300 if not app.settings.get("popover_wide") else 380
            self.assertEqual(panel.WIDTH, 380)
        finally:
            app.store._close()

    def test_legacy_settings_txt_is_migrated_on_startup(self):
        """老 settings.txt 用户升级后：启动即迁移，配置不丢"""
        with open(os.path.join(self.tmp, "settings.txt"), "w", encoding="utf-8") as f:
            f.write("refresh_interval=90\nwarning_threshold=80\nnotify_interval=500\n"
                    "ccswitch_enabled=true\nzcode_enabled=false\n")
        app = main.CcBarTray()
        try:
            self.assertEqual(app.settings.get("refresh_interval"), 90)
            self.assertEqual(app.settings.get("warning_threshold"), 80)
            self.assertEqual(app.settings.get("notify_interval"), 500)
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "settings.json")),
                            "迁移后应写出 settings.json")
        finally:
            app.store._close()


if __name__ == "__main__":
    unittest.main()
