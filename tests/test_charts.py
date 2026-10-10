"""批次 0 回归：图表 helper 与 GUI 线程装饰器。

背景（实测复现的三个坏点）：
  1. `@_on_gui` 悬空装饰到 `_pil_font`（本应是纯 staticmethod）上，
     导致 `CcBarTray._pil_font(13)` 抛 AttributeError: 'int' object has no attribute '_ui'；
  2. `_bar_chart_png` / `_line_chart_png` 因此永远抛异常，而洞察中心费用页
     第一个就调它 → 整个洞察中心构建中断；
  3. 费用页还把 `Design.BRAND`（字符串）当 colors 字典传进去。

本文件在非 Windows 环境下用桩件导入 main（main 只在方法内部用 pystray /
win10toast / pyperclip / ctypes.windll，导入期不需要它们）。
Pillow 是唯一需要的第三方依赖。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win_stubs  # noqa: E402

win_stubs.install()

from PIL import Image  # noqa: E402

import main  # noqa: E402
from main import CcBarTray, Design  # noqa: E402
from stats_store import StatsStore  # noqa: E402


class TestChartHelpers(unittest.TestCase):
    def test_chart_helpers_are_plain_statics(self):
        """_pil_font / 两个 PNG 图 helper 必须是纯 staticmethod，不能被 _on_gui 包住"""
        for name in ("_pil_font", "_bar_chart_png", "_line_chart_png"):
            fn = CcBarTray.__dict__[name]
            # Python 3.10+ 的 staticmethod 对象自带 __wrapped__ 属性，需先解出原函数再判
            func = fn.__func__ if hasattr(fn, "__func__") else fn
            self.assertFalse(
                hasattr(func, "__wrapped__"),
                f"{name} 被装饰器包住了（_on_gui 只能用在窗口方法上）")

        font = CcBarTray._pil_font(13)
        self.assertIsNotNone(font, "_pil_font 不得返回 None")

    def test_bar_chart_png_accepts_color_map(self):
        """series 的值必须是 {名称: 数值}，colors 必须是 {名称: hex}"""
        img = CcBarTray._bar_chart_png(
            [("08-01", {"费用": 1.5}), ("08-02", {"费用": 2.5})],
            {"费用": Design.BRAND})
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (680, 150))

    def test_bar_chart_png_empty_series(self):
        img = CcBarTray._bar_chart_png([], {"费用": Design.BRAND})
        self.assertIsInstance(img, Image.Image)

    def test_line_chart_png(self):
        img = CcBarTray._line_chart_png([("08-01", 61.5), ("08-02", 72.0)],
                                        Design.BRAND, suffix="%")
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (680, 120))

    def test_insights_cost_tab_series_shape(self):
        """洞察页面用到的调用形状：单序列字典 + 名称到色的映射"""
        cost_daily = [("2026-08-01", 1.5), ("2026-08-02", 0.0)]
        series = [(d[5:], {"费用": c}) for d, c in cost_daily]
        img = CcBarTray._bar_chart_png(series, {"费用": Design.BRAND})
        self.assertIsInstance(img, Image.Image)

    def test_show_settings_is_marshalled_to_gui_thread(self):
        """设置窗口会从托盘线程被打开，必须经 _ui 投递"""
        self.assertTrue(hasattr(CcBarTray.__dict__["show_settings"], "__wrapped__"),
                        "show_settings 需要 @_on_gui")

    def test_share_card_store_method_exists(self):
        """_share_card_png 依赖的 store 方法必须存在"""
        self.assertTrue(hasattr(StatsStore, "query_daily_tokens"))


if __name__ == "__main__":
    unittest.main()
