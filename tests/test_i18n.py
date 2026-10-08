"""批次 4：中英界面切换的守门员。

三件事：
1. 静态扫描 main.py：所有中文字面量（docstring 除外）都必须包在 L() 里；
   允许清单为空——新增一个漏包的中文界面串就失败。
2. 英文模式真建窗口（设置页 / 洞察中心 6 页 / 5 个详情窗口 / 面板），
   遍历控件树收集用户可见文案，断言里面一个汉字都没有。
   唯一例外是语言下拉里那个"中文"项（语言选择器按设计永远显示 "中文"，
   见 l10n.language_display_name）。
3. 语言是全局状态：EN 出 K/M/B、ZH 出 万/亿；周报日期区间同理
   （含分享页周报卡预览真的把 english= 传下去）。
"""
import ast
import os
import re
import sys
import unittest
from datetime import datetime

from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import l10n  # noqa: E402
import ui_fixture  # noqa: E402  （公共夹具：装好 win_stubs 并真建 tk 窗口）
import weekly_report  # noqa: E402
from ui_fixture import main  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:  # pragma: no cover - 没有 tkinter 的构建
    tk = ttk = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 汉字 + 中文标点 + 全角字符
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


class TestMainStringsAreWrapped(unittest.TestCase):
    """main.py 里的中文字面量必须全部包在 L() 里（静态扫描，不需要显示环境）"""

    # 允许清单（≤ 12 条，每条都要写清"为什么不翻译"）。当前为空：
    # 语言下拉的 "中文" 一项来自 l10n.language_display_name（main.py 里没有字面量），
    # 万/亿 单位由 l10n.format_tokens 负责（main.py 里也没有字面量）。
    ALLOWED_UNWRAPPED = {}

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            cls.source = f.read()
        cls.tree = ast.parse(cls.source)

    def _unwrapped_cjk_literals(self):
        """{中文原文: [行号, ...]}：docstring 之外、且没有被 L(...) 包住的字面量"""
        tree = self.tree
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

        # "被 L 包住" = 该字面量落在某个 L(...) 调用的实参里
        wrapped = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "L":
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant):
                            wrapped.add(id(sub))

        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or id(node) in wrapped:
                continue
            if CJK.search(node.value):
                found.setdefault(node.value, []).append(node.lineno)
        return found

    def test_every_ui_string_goes_through_L(self):
        unwrapped = self._unwrapped_cjk_literals()
        self.assertEqual(
            sorted(unwrapped), sorted(self.ALLOWED_UNWRAPPED),
            "main.py 里有没包 L() 的中文界面串：%s"
            % {k: v for k, v in sorted(unwrapped.items())})

    def test_allow_list_is_small_and_documented(self):
        self.assertLessEqual(len(self.ALLOWED_UNWRAPPED), 12,
                             "允许清单超过 12 条：说明漏包的地方太多了")
        for key, reason in self.ALLOWED_UNWRAPPED.items():
            self.assertTrue(CJK.search(key), "允许清单里不该有非中文字面量: %r" % key)
            self.assertTrue(reason and reason.strip(),
                            "允许清单 %r 没写理由" % key)

    def test_scan_really_looks_at_f_strings(self):
        """f-string 里的中文片段也是 ast.Constant：漏包必须能被扫出来"""
        tree = ast.parse('x = f"共读取 {n} 行"')
        consts = [n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and CJK.search(n.value)]
        self.assertEqual(consts, ["共读取 ", " 行"])


@unittest.skipIf(tk is None, "没有 tkinter")
class TestEnglishUi(ui_fixture.TrayFixture):
    """英文模式下把所有窗口真建一遍，控件树里不得出现汉字"""

    LANGUAGE = "en"

    # 唯一允许的汉字：语言下拉里的 "中文" 项本身
    ALLOWED_EN_TEXT = {"中文"}

    # ------------------------------------------------------------ 遍历

    def _menu_labels(self, owner, menu):
        """OptionMenu / Menubutton / Menu 的下拉项文案"""
        if menu is None:
            return []
        if isinstance(menu, str):
            try:
                menu = owner.nametowidget(menu)
            except Exception:
                return []
        labels = []
        try:
            end = menu.index("end")
        except Exception:
            return labels
        if end is None:
            return labels
        for i in range(int(end) + 1):
            for option in ("text", "label"):
                try:
                    labels.append(menu.entrycget(i, option))
                    break
                except Exception:
                    continue
        return labels

    def _visible_texts(self, widget):
        """递归收集用户可见文案：text / 窗口标题 / 页签 / 表头 / 下拉项 / Canvas 文本"""
        out = []
        if isinstance(widget, (tk.Toplevel, tk.Tk)):
            try:
                out.append(widget.title())
            except Exception:
                pass
        try:
            text = widget.cget("text")
            if isinstance(text, str) and text:
                out.append(text)
        except Exception:
            pass
        if tk is not None and isinstance(widget, tk.Canvas):
            for item in widget.find_all():
                if widget.type(item) == "text":
                    out.append(widget.itemcget(item, "text"))
        if isinstance(widget, ttk.Notebook):
            out.extend(widget.tab(t, "text") for t in widget.tabs())
        if isinstance(widget, ttk.Treeview):
            out.extend(widget.heading(c, "text") for c in widget["columns"])
        if isinstance(widget, ttk.Combobox):
            out.extend(str(v) for v in widget.cget("values"))
        if isinstance(widget, tk.Menubutton):
            out.extend(self._menu_labels(widget, widget.cget("menu")))
        if isinstance(widget, tk.Menu):
            out.extend(self._menu_labels(widget, widget))
        for child in widget.winfo_children():
            out.extend(self._visible_texts(child))
        return out

    def _assert_english(self, what, roots, minimum=5):
        texts = []
        for root in roots:
            texts.extend(self._visible_texts(root))
        self.assertGreaterEqual(len(texts), minimum,
                                "%s 没收集到文案，遍历器坏了？" % what)
        offenders = {}
        for text in texts:
            if CJK.search(text) and text not in self.ALLOWED_EN_TEXT:
                offenders.setdefault(text, []).append(what)
        self.assertEqual(offenders, {},
                         "%s 在英文模式下还有中文文案：%s"
                         % (what, sorted(offenders)))
        return texts

    # ------------------------------------------------------------ 用例

    def test_settings_window_is_english(self):
        main.CcBarTray.show_settings.__wrapped__(self.app)
        wins = self._toplevels(main.L("ccBar 设置"))
        self.assertEqual(len(wins), 1, "设置窗口没建出来（标题没跟着语言走？）")
        self._assert_english("设置窗口", wins, minimum=20)

    def test_language_picker_keeps_chinese_option(self):
        """语言下拉的 "中文" 项刻意保留；顺带证明遍历器真能读到菜单项文案"""
        main.CcBarTray.show_settings.__wrapped__(self.app)
        win = self._toplevels(main.L("ccBar 设置"))[0]
        texts = self._visible_texts(win)
        self.assertIn("中文", texts, "语言选择的 中文 项没了")
        self.assertIn(main.L("界面语言"), texts)

    def test_insights_center_is_english(self):
        win, nb = self._build_insights()
        labels = [nb.tab(t, "text") for t in nb.tabs()]
        self.assertEqual(labels,
                         [main.L(t) for t in
                          ("费用", "洞察", "分享", "渠道", "流水", "积分")])
        for tab_id in nb.tabs():
            self.assertTrue(nb.nametowidget(tab_id).winfo_children(),
                            "页签 %s 是空的" % nb.tab(tab_id, "text"))
        self._assert_english("洞察中心", [win], minimum=40)

    def test_detail_windows_are_english(self):
        cases = [
            (lambda: self._build_window(main.CcBarTray.show_hourly_detail,
                                        main.L("每小时用量详情")),
             "每小时用量详情"),
            (lambda: self._build_window(main.CcBarTray.show_model_detail,
                                        main.L("模型分布详情")),
             "模型分布详情"),
            (lambda: self._build_window(main.CcBarTray.show_daily_detail,
                                        main.L("近7天用量"),
                                        days=7, title=main.L("近7天用量")),
             "近7天用量"),
            (lambda: self._build_window(main.CcBarTray.show_daily_detail,
                                        main.L("近30天用量"),
                                        days=30, title=main.L("近30天用量")),
             "近30天用量"),
            (lambda: self._build_window(main.CcBarTray.show_all_time_detail,
                                        main.L("历史总量")),
             "历史总量"),
        ]
        for build, name in cases:
            win = build()
            self._assert_english("详情窗口 %s" % name, [win], minimum=8)
            win.destroy()

    def test_popover_is_english(self):
        _popover, body = self._build_popover()
        self.assertNotEqual(body.winfo_children(), [])
        self._assert_english("面板", [body], minimum=8)

    def test_tray_menu_and_titles_are_english(self):
        """托盘菜单 / 托盘标题 / 悬停提示也要跟着语言走"""

        class FakeMenuItem:
            def __init__(self, text="", action=None, **kwargs):
                self.text = text
                self.action = action
                self.kwargs = kwargs

        class FakeMenu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        self.app.check_warning = lambda stats: None       # 不碰 ~/.notified.txt
        fake = mock.MagicMock(Menu=FakeMenu, MenuItem=FakeMenuItem)
        with mock.patch.object(main, "pystray", fake):
            items = self.app.build_menu()
        texts = [i.text for i in items if isinstance(i, FakeMenuItem)]
        self.assertGreaterEqual(len(texts), 10, "托盘菜单项少了：%s" % texts)
        self.assertEqual([t for t in texts if CJK.search(t)], [],
                         "托盘菜单在英文模式下还有中文：%s" % texts)

        self.app.query_day_stats = lambda days=0: None    # 无数据时的托盘文案
        self.assertEqual(self.app.get_menu_text(), main.L("未找到数据源"))
        self.assertEqual(self.app.get_tooltip_text(), main.L("ccBar - 未找到数据"))

    def test_weekly_card_preview_range_follows_language(self):
        """分享页周报卡预览：EN 传下去的是英文区间，ZH 是中文区间"""
        captured = {}

        def fake_card(date_text, *args, **kwargs):
            captured["date"] = date_text
            return None

        with mock.patch.object(main.share_card, "weekly_card_image", fake_card):
            l10n.set_language("en")
            self.app._weekly_card_png()
            self.assertRegex(captured["date"],
                             r"^[A-Z][a-z]{2} \d{1,2}, \d{4} ~ "
                             r"[A-Z][a-z]{2} \d{1,2}, \d{4}$")
            l10n.set_language("zh")
            self.app._weekly_card_png()
            self.assertRegex(captured["date"],
                             r"^\d{4}年\d{1,2}月\d{1,2}日 ~ "
                             r"\d{4}年\d{1,2}月\d{1,2}日$")


class TestLanguageIsGlobalState(unittest.TestCase):
    """语言是全局开关：切 EN/ZH 必须立刻在两个方向都生效（防泄漏）"""

    def setUp(self):
        l10n.set_language("zh")

    def tearDown(self):
        l10n.set_language("zh")

    def test_fmt_tokens_switches_both_ways(self):
        l10n.set_language("en")
        self.assertEqual(main.Design.fmt_tokens(2_500_000), "2.50M")
        self.assertEqual(main.Design.fmt_tokens(1_500_000_000), "1.50B")
        self.assertEqual(main.CcBarTray.fmt_tokens_static(10_000), "10.0K")

        l10n.set_language("zh")
        self.assertEqual(main.Design.fmt_tokens(2_500_000), "250万")
        self.assertEqual(main.Design.fmt_tokens(1_500_000_000), "15.00亿")
        self.assertEqual(main.CcBarTray.fmt_tokens_static(10_000), "1万")

    def test_weekly_date_range_switches_both_ways(self):
        monday = datetime(2026, 8, 3)          # 周一：报告周 = 7/27 ~ 8/2
        self.assertEqual(weekly_report.date_range_text(monday, english=True),
                         "Jul 27, 2026 ~ Aug 2, 2026")
        self.assertEqual(weekly_report.date_range_text(monday),
                         "2026年7月27日 ~ 2026年8月2日")

    def test_share_card_labels_follow_language(self):
        """卡片文案也要跟着切（英文卡片不能写中文）"""
        args = (1_000, 2_000, 3_000, 4_000, [])
        l10n.set_language("en")
        self.assertEqual(l10n.L("AI 用量战报"), "AI Usage Report")
        self.assertEqual(l10n.L("今日消耗"), "Today's burn")
        for key in ("近 7 天", "近 30 天", "累计", "AI 用量周报", "周消耗",
                    "日均", "峰值", "请求数"):
            self.assertFalse(CJK.search(l10n.L(key)), key)
        self.assertIsNotNone(main.share_card.share_card_image(*args))
        l10n.set_language("zh")
        self.assertIsNotNone(main.share_card.share_card_image(*args))


if __name__ == "__main__":
    unittest.main()
