"""窗口构建冒烟测试：真正建出 tkinter 窗口，验证没有中途抛异常。

这是批次 0 那个 bug 的守门员——`_bar_chart_png` 抛异常时，`show_insights`
会在费用页中断，导致后面的洞察/分享/渠道/流水页根本没建出来，
而单看代码是发现不了的（异常在 GUI 线程的队列里被吞成一行打印）。

无显示环境（CI 的 ubuntu runner）自动跳过；本机 macOS / Windows 会真跑。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win_stubs  # noqa: E402

win_stubs.install()

import main  # noqa: E402
import app_settings  # noqa: E402
import themes  # noqa: E402
import weekly_report  # noqa: E402
from main import CcBarTray, Design, blend, usage_color  # noqa: E402
from stats_store import StatsStore  # noqa: E402

try:
    import tkinter as tk
except Exception:  # pragma: no cover - 没有 tkinter 的构建
    tk = None


def local_midnight(days_ago=0):
    day = datetime.now() - timedelta(days=days_ago)
    return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


@unittest.skipIf(tk is None, "没有 tkinter")
class TestInsightsWindowBuilds(unittest.TestCase):
    """洞察中心必须完整建出全部页签"""

    EXPECTED_TABS = ["费用", "洞察", "分享", "渠道", "流水", "积分"]

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except Exception as e:  # 无显示环境
            raise unittest.SkipTest(f"无可用显示: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-smoke-")
        self._orig_store_path = StatsStore.store_path
        StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))

        self.source_path = os.path.join(self.tmp, "cc-switch.db")
        self._make_fixture()

        # macOS Tk 8.5 在非 app bundle 进程里跑完整事件循环（update/focus_force）
        # 会挂住，Windows 上不存在这个问题。窗口构建本身不需要它。
        self._orig_front = CcBarTray._bring_to_front
        CcBarTray._bring_to_front = staticmethod(lambda win: None)

        self.app = CcBarTray.__new__(CcBarTray)   # 不走 __init__，避开真实设置文件
        self.app.icon = None
        self.app._ui_queue = __import__("queue").Queue()
        self.app._ui_root = self.root
        self.app._last_icon_color = None
        # 用真实的设置对象（临时目录），但不碰 ~/.ccbar
        self.app.settings = app_settings.Settings(self.tmp)
        self.app.settings.set("db_path", self.source_path)
        self.app.settings.set("ccswitch_enabled", True)
        self.app.settings.set("zcode_enabled", False)
        self.app.settings.set("zcode_path", "")
        self.app.theme = None
        self.app.apply_language()
        self.app.apply_theme()
        self.app.store = StatsStore()
        self.app.connect_store()

    def tearDown(self):
        for w in list(self.root.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        CcBarTray._bring_to_front = self._orig_front
        self.app.store._close()
        StatsStore.store_path = self._orig_store_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _make_fixture(self):
        conn = sqlite3.connect(self.source_path)
        conn.executescript("""
        CREATE TABLE proxy_request_logs (
            request_id TEXT PRIMARY KEY, app_type TEXT, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
            total_cost_usd REAL, created_at INTEGER
        );
        CREATE TABLE usage_daily_rollups (
            date TEXT, provider_id TEXT, model TEXT, request_model TEXT,
            pricing_model TEXT, app_type TEXT, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER, total_cost_usd REAL, request_count INTEGER
        );
        """)
        # 上一个完整周窗口内的数据（周报自动生成的测试依赖它；
        # 窗口随"今天星期几"浮动，所以按窗口边界取，不能写死天数）
        week_from, _week_to = weekly_report.last_week_window()
        rows = [
            ("r-week", local_midnight(week_from) + 3600, 5000, 6000, 0, 0, 1.25),
            ("r-1", local_midnight(1) + 3600, 1000, 2000, 500, 100, 0.5),
            ("r-2", local_midnight(2) + 7200, 3000, 4000, 100, 50, 0.25),
            ("r-3", max(local_midnight(0) + 60, int(datetime.now().timestamp()) - 60),
             700, 900, 200, 30, 0.75),
        ]
        for rid, ts, i, o, cr, cc, cost in rows:
            conn.execute(
                "INSERT INTO proxy_request_logs VALUES (?, 'claude', 'claude-sonnet-4', "
                "?, ?, ?, ?, ?, ?)", (rid, i, o, cr, cc, cost, ts))
        conn.commit()
        conn.close()

    def _find_notebook(self, widget):
        """递归找 ttk.Notebook"""
        import tkinter.ttk as ttk
        for child in widget.winfo_children():
            if isinstance(child, ttk.Notebook):
                return child
            found = self._find_notebook(child)
            if found is not None:
                return found
        return None

    def _build_insights(self):
        """绕过 @_on_gui 直接在当前线程建窗口（不泵事件），返回 (窗口, Notebook)"""
        CcBarTray.show_insights.__wrapped__(self.app)
        toplevels = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        insights = [w for w in toplevels if w.title() == "洞察中心"]
        self.assertEqual(len(insights), 1, "洞察中心窗口没建出来")
        nb = self._find_notebook(insights[0])
        self.assertIsNotNone(nb, "洞察中心里没有 Notebook")
        return insights[0], nb

    def _texts(self, widget, acc=None):
        """递归收集控件树里所有 label/button 文案（Canvas 等没有 text 选项会抛，忽略）"""
        if acc is None:
            acc = []
        try:
            text = widget.cget("text")
        except Exception:
            text = None
        if isinstance(text, str) and text:
            acc.append(text)
        for child in widget.winfo_children():
            self._texts(child, acc)
        return acc

    # ------------------------------------------------------------ 详情窗口/面板助手

    def _walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)

    def _text_of(self, widget):
        try:
            value = widget.cget("text")
        except Exception:
            return None
        return value if isinstance(value, str) else None

    def _find_by_text(self, widget, text):
        return [w for w in self._walk(widget) if self._text_of(w) == text]

    def _toplevels(self, title):
        return [w for w in self.root.winfo_children()
                if isinstance(w, tk.Toplevel) and w.title() == title]

    def _build_window(self, method, win_title, *args, **kwargs):
        """绕过 @_on_gui 在当前线程建详情窗口（不泵事件），返回窗口"""
        method.__wrapped__(self.app, *args, **kwargs)
        wins = self._toplevels(win_title)
        self.assertEqual(len(wins), 1,
                         "%s 窗口没建出来（构建中途抛异常？）" % win_title)
        return wins[0]

    def _invoke_button(self, window, text):
        buttons = [w for w in self._find_by_text(window, text)
                   if isinstance(w, tk.Button)]
        self.assertTrue(buttons, "找不到按钮 %r" % text)
        buttons[0].invoke()
        return buttons[0]

    # ------------------------------------------------------------ 用例

    def test_insights_builds_all_tabs(self):
        # 只构建、不跑事件循环：macOS Tk 8.5 在非 app bundle 进程里
        # update()/update_idletasks() 会挂住（Windows 无此问题），
        # 而本用例要断言的是"构建过程中没抛异常 + 控件树完整"，两者都不需要泵事件。
        _win, nb = self._build_insights()

        labels = [nb.tab(t, "text") for t in nb.tabs()]
        self.assertEqual(labels, self.EXPECTED_TABS,
                         f"页签不完整：实际 {labels}（构建中途抛异常会缺后面的页）")

        # 每个页签里都要有内容（费用页的图表就是原 bug 的爆点）
        for tab_id in nb.tabs():
            frame = nb.nametowidget(tab_id)
            self.assertTrue(frame.winfo_children(),
                            f"页签 {nb.tab(tab_id, 'text')} 是空的")

    def test_insights_builds_with_budget_and_price(self):
        """月度预算 > 0 要点出预算卡与日预算虚线；默认单价 > 0 要在费用页标注估算"""
        self.app.settings.set("monthly_budget_usd", 100.0)
        self.app.settings.set("default_token_price", 3.0)
        _win, nb = self._build_insights()
        self.assertEqual([nb.tab(t, "text") for t in nb.tabs()], self.EXPECTED_TABS)

        texts = self._texts(nb.nametowidget(nb.tabs()[0]))
        self.assertTrue(any(t.startswith("本月预算 $100.00") for t in texts),
                        f"费用页缺少月度预算卡：{texts}")
        self.assertTrue(any("已用预算" in t for t in texts), "缺少预算进度副行")
        self.assertTrue(any("性价比榜" in t for t in texts), "费用页缺性价比榜")
        self.assertTrue(any("估算" in t for t in texts), "默认单价 > 0 时费用卡要标注估算")

    def test_cost_page_has_richer_content(self):
        """费用页在 batch 2b 之后内容变多（卡片/走势/排行/性价比/脚注）"""
        _win, nb = self._build_insights()
        self.assertGreaterEqual(len(self._texts(nb.nametowidget(nb.tabs()[0]))), 12,
                                "费用页内容比 batch 2a 少")

    def test_insights_page_new_sections(self):
        """洞察页新增：今日会话 / 星期分布 / 热力图 / 模型编年史 / 导出长图"""
        _win, nb = self._build_insights()
        texts = self._texts(nb.nametowidget(nb.tabs()[1]))
        for expect in ("今日会话", "星期分布（近 90 天）", "近 90 天用量热力图",
                       "模型编年史", "导出长图"):
            self.assertTrue(any(expect in t for t in texts), f"洞察页缺少 {expect}")

    def test_share_page_renders_two_cards(self):
        """分享页同时预览战报卡（640x420）与周报卡（460x400）"""
        self.assertEqual(self.app._share_card_png().size, (640, 420))
        self.assertEqual(self.app._weekly_card_png().size, (460, 400))

    def test_timeline_page_has_day_nav_and_csv_export(self):
        """流水页新增：任意日期回看 + 导出 CSV"""
        _win, nb = self._build_insights()
        texts = self._texts(nb.nametowidget(nb.tabs()[4]))
        for expect in ("‹ 前一天", "后一天 ›", "今天", "导出 CSV"):
            self.assertTrue(any(expect in t for t in texts), f"流水页缺少 {expect}")

    def test_insights_restores_last_tab(self):
        """页签记忆：按设置选中，非法值回落「费用」；新增的「积分」页同样可记住"""
        self.app.settings.set("insights_last_page", "分享")
        win, nb = self._build_insights()
        self.assertEqual(nb.tab(nb.select(), "text"), "分享")
        win.destroy()

        self.app.settings.set("insights_last_page", "积分")
        win, nb = self._build_insights()
        self.assertEqual(nb.tab(nb.select(), "text"), "积分")
        win.destroy()

        self.app.settings.set("insights_last_page", "不存在的页")
        _win, nb = self._build_insights()
        self.assertEqual(nb.tab(nb.select(), "text"), "费用")

    def test_session_stats_matches_mac(self):
        """会话判定：相邻间隔 > 30 分钟切新会话，时长取会话内末次-首次"""
        base = int(datetime(2026, 1, 1, 9, 0).timestamp())
        rows = [(base, "m", "cc-switch", 0, 0.0, 0.0),
                (base + 600, "m", "cc-switch", 0, 0.0, 0.0),
                (base + 3 * 3600, "m", "cc-switch", 0, 0.0, 0.0)]
        self.assertEqual(CcBarTray._session_stats(rows), (2, 5, 10))
        self.assertEqual(CcBarTray._session_stats([]), (0, 0, 0))

    def test_bar_chart_accepts_budget_line(self):
        """费用走势图的日预算虚线：新增可选参数后仍返回同尺寸图"""
        img = CcBarTray._bar_chart_png(
            [("08-01", {"费用": 1.0}), ("08-02", {"费用": 3.0})], {"费用": Design.BRAND},
            budget_line=2.0, value_fmt=lambda v: "$%.2f" % v)
        self.assertEqual(img.size, (680, 150))

    def test_share_tab_renders_card(self):
        """分享页依赖 store.query_daily_tokens（曾缺失导致 AttributeError）"""
        img = self.app._share_card_png()
        self.assertEqual(img.size, (640, 420))

    def test_settings_window_builds(self):
        """设置页新增长列表后仍要能完整建出来（内容滚动 + 钉底按钮栏）"""
        CcBarTray.show_settings.__wrapped__(self.app)
        toplevels = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        settings = [w for w in toplevels if w.title() == "ccBar 设置"]
        self.assertEqual(len(settings), 1, "设置窗口没建出来")
        self.assertTrue(settings[0].winfo_children(), "设置窗口是空的")

    def test_theme_switch_applies_to_design(self):
        """换主题后 Design 令牌要跟着变（海蓝主色 #2E8CF2）"""
        self.app.settings.set("theme", "海蓝")
        self.app.apply_theme()
        self.assertEqual(Design.BRAND, "#2E8CF2")
        self.assertEqual(Design.MODEL_COLORS, themes.find("海蓝")["models"])

        self.app.settings.set("theme", "默认主题")
        self.app.apply_theme()
        # 默认主题的主色以主题包为准（mac 版 #E86E45；旧硬编码 #E86D45 差一位）
        self.assertEqual(Design.BRAND, themes.find("默认主题")["accent"])
        self.assertEqual(Design.BIG_NUMBER, themes.find("默认主题")["accent"],
                         "bigNumber 为空时跟随 accent")

    def test_weekly_report_generates_then_is_idempotent(self):
        """周一自动周报：真生成 PNG（460x400）+ 通知一次，重复调用不重复生成"""
        report_dir = os.path.join(self.tmp, "weekly")
        orig_dir = weekly_report.directory
        weekly_report.directory = lambda documents_dir=None: report_dir
        notified = []
        orig_toast = self.app._toast
        self.app._toast = lambda title, body, duration=5: notified.append((title, body))
        try:
            path = self.app.maybe_generate_weekly_report()
            self.assertTrue(path and os.path.exists(path), "周报没生成")
            self.assertTrue(os.path.basename(path).startswith("ccbar-weekly-"))
            from PIL import Image
            with Image.open(path) as img:
                self.assertEqual(img.size, (460, 400))
            self.assertEqual(len(notified), 1, "周报生成后应通知一次")
            self.assertIn("周报", notified[0][0])

            # 幂等：同一周再次调用不再生成、不再通知
            self.assertIsNone(self.app.maybe_generate_weekly_report())
            self.assertEqual(len(notified), 1)
        finally:
            weekly_report.directory = orig_dir
            self.app._toast = orig_toast

    def test_weekly_report_respects_setting(self):
        """关了开关就不生成"""
        report_dir = os.path.join(self.tmp, "weekly-off")
        orig_dir = weekly_report.directory
        weekly_report.directory = lambda documents_dir=None: report_dir
        self.app.settings.set("auto_weekly_report", False)
        try:
            self.assertIsNone(self.app.maybe_generate_weekly_report())
            self.assertFalse(os.path.exists(report_dir))
        finally:
            weekly_report.directory = orig_dir
            self.app.settings.set("auto_weekly_report", True)

    def test_sync_trae_is_skipped_when_disabled(self):
        """没启用 Trae 时不该发请求"""
        calls = []
        self.app.store.sync_trae_if_needed = lambda interactive=False: calls.append(interactive)
        self.app.settings.set("trae_enabled", False)
        self.app.sync_trae(interactive=False)
        import time as _t
        _t.sleep(0.2)
        self.assertEqual(calls, [], "未启用 Trae 仍发起了同步")

    def test_daily_tokens_backs_the_share_card(self):
        """分享卡折线的数据源：历史来自 daily_agg（昨天），今天走实时"""
        from datetime import datetime, timedelta

        rows = self.app.store.query_daily_tokens(7)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertIn(today, [d for d, _ in rows], "今日实时行必须出现")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertIn(yesterday, [d for d, _ in rows], "昨天的历史行必须出现")



    # ------------------------------------------------------------ 详情窗口：mac 对齐

    def test_detail_windows_have_stat_cards(self):
        """每个详情窗口顶部一排统计卡（mac DetailStat：小标签 + 大数字）"""
        cases = [
            (lambda: self._build_window(CcBarTray.show_hourly_detail,
                                        "每小时用量详情", days_ago=0),
             ("总 Token", "请求数", "峰值时段")),
            (lambda: self._build_window(CcBarTray.show_model_detail, "模型分布详情"),
             ("总 Token", "模型数", "Top1 占比")),
            (lambda: self._build_window(CcBarTray.show_daily_detail, "近7天用量",
                                        days=7, title="近7天用量"),
             ("总 Token", "日均", "请求数", "缓存读")),
            (lambda: self._build_window(CcBarTray.show_daily_detail, "近30天用量",
                                        days=30, title="近30天用量"),
             ("总 Token", "日均", "请求数", "缓存读")),
            (lambda: self._build_window(CcBarTray.show_all_time_detail, "历史总量"),
             ("历史总量", "月均", "最佳月", "请求数")),
        ]
        for build, labels in cases:
            win = build()
            for label in labels:
                self.assertTrue(self._find_by_text(win, label),
                                "%s 缺少统计卡 %r" % (win.title(), label))
            self.assertFalse(self._find_by_text(win, "暂无数据"),
                             "%s 有数据却显示暂无数据" % win.title())
            win.destroy()

    def test_hourly_detail_token_total_includes_cache_create(self):
        """每小时详情的总 Token 含缓存创建（与 mac hourly SQL 同口径）"""
        win = self._build_window(CcBarTray.show_hourly_detail, "每小时用量详情")
        # 夹具今日唯一一行：input 700 + output 900 + cache_read 200 + cache_create 30
        self.assertTrue(self._find_by_text(win, "1830"),
                        "总 Token 少了缓存创建：%s" % self._texts(win))
        win.destroy()

    def test_detail_windows_empty_state_is_defensive(self):
        """没数据时显示「暂无数据」，绝不抛"""
        self.app.query_hourly_stats = lambda days_ago=0: {}
        win = self._build_window(CcBarTray.show_hourly_detail, "每小时用量详情")
        self.assertTrue(self._find_by_text(win, "暂无数据"))
        win.destroy()

        self.app.query_model_breakdown_by_day = lambda days_ago=0: []
        win = self._build_window(CcBarTray.show_model_detail, "模型分布详情")
        self.assertTrue(self._find_by_text(win, "暂无数据"))
        win.destroy()

        self.app.query_daily_stats_for_range = lambda start, end: {}
        win = self._build_window(CcBarTray.show_daily_detail, "近7天用量",
                                 days=7, title="近7天用量")
        self.assertTrue(self._find_by_text(win, "暂无数据"))
        win.destroy()

        self.app.store.query_monthly_totals = lambda limit=36: []
        self.app.query_day_stats = lambda days=0: {"reqs": 0, "total": 0, "cache_read": 0,
                                                   "input": 0, "output": 0,
                                                   "cache_create": 0}
        win = self._build_window(CcBarTray.show_all_time_detail, "历史总量")
        self.assertTrue(self._find_by_text(win, "暂无数据"))
        win.destroy()

    def test_detail_window_nav_reorder_and_today_chip(self):
        """导航重排：‹ / 日期 / › / 回到今天；› 在最新周期禁用，chip 只在非当前周期出现"""
        win = self._build_window(CcBarTray.show_hourly_detail, "每小时用量详情")
        nav = None
        for frame in [w for w in self._walk(win) if isinstance(w, tk.Frame)]:
            texts = [self._text_of(c) for c in frame.winfo_children()]
            if "‹" in texts and "›" in texts:
                nav = frame
                break
        self.assertIsNotNone(nav, "找不到导航栏")
        order = [self._text_of(w) for w in nav.pack_slaves()]
        self.assertEqual((order[0], order[-2], order[-1]), ("‹", "导出 CSV", "›"),
                         "导航顺序不对：%s" % order)

        chip = self._find_by_text(win, "回到今天")[0]
        nxt = self._find_by_text(win, "›")[0]
        self.assertEqual(chip.winfo_manager(), "")
        self.assertEqual(str(nxt.cget("state")), "disabled")

        self._invoke_button(win, "‹")           # 退到昨天
        self.assertEqual(chip.winfo_manager(), "pack")
        self.assertEqual(str(nxt.cget("state")), "normal")
        order = [self._text_of(w) for w in nav.pack_slaves()]
        self.assertEqual(order[-3:], ["导出 CSV", "回到今天", "›"],
                         "「回到今天」不在 › 右侧：%s" % order)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%y-%m-%d")
        self.assertTrue(self._find_by_text(win, yesterday), "日期文本没跟上")

        self._invoke_button(win, "回到今天")     # 回到今天
        self.assertEqual(chip.winfo_manager(), "")
        self.assertEqual(str(nxt.cget("state")), "disabled")
        win.destroy()

        for title, days in (("近7天用量", 7), ("近30天用量", 30)):
            win = self._build_window(CcBarTray.show_daily_detail, title,
                                     days=days, title=title)
            chip = self._find_by_text(win, "回到今天")[0]
            nxt = self._find_by_text(win, "›")[0]
            self.assertEqual(chip.winfo_manager(), "", title)
            self.assertEqual(str(nxt.cget("state")), "disabled", title)
            self._invoke_button(win, "‹")
            self.assertEqual(chip.winfo_manager(), "pack", title)
            self.assertEqual(str(nxt.cget("state")), "normal", title)
            win.destroy()

    def test_detail_windows_export_csv(self):
        """每个详情窗口的导出按钮：BOM + 表头 + 默认文件名都与 mac 一致"""
        import csv
        import io

        self.app.store.query_monthly_totals = lambda limit=36: [
            (datetime.now().strftime("%Y-%m"), 3, 3000, 30), ("2020-01", 1, 100, 10)]

        cases = [
            (lambda: self._build_window(CcBarTray.show_hourly_detail,
                                        "每小时用量详情"),
             "ccbar-每小时.csv", ["时间", "请求数", "总Token", "缓存读"]),
            (lambda: self._build_window(CcBarTray.show_model_detail, "模型分布详情"),
             "ccbar-模型分布.csv", ["模型", "请求数", "总Token", "缓存读"]),
            (lambda: self._build_window(CcBarTray.show_daily_detail, "近7天用量",
                                        days=7, title="近7天用量"),
             "ccbar-近7天.csv", ["日期", "请求数", "总Token", "缓存读"]),
            (lambda: self._build_window(CcBarTray.show_daily_detail, "近30天用量",
                                        days=30, title="近30天用量"),
             "ccbar-近30天.csv", ["日期", "请求数", "总Token", "缓存读"]),
            (lambda: self._build_window(CcBarTray.show_all_time_detail, "历史总量"),
             "ccbar-按月汇总.csv", ["月份", "请求数", "总Token", "缓存读"]),
        ]
        for build, filename, header in cases:
            win = build()
            path = os.path.join(self.tmp, filename)
            calls = {}

            def fake_save(**kwargs):
                calls.update(kwargs)
                return path

            with mock.patch("tkinter.filedialog.asksaveasfilename",
                            side_effect=fake_save), \
                    mock.patch("tkinter.messagebox.showinfo", return_value=None), \
                    mock.patch("tkinter.messagebox.showerror", return_value=None):
                self._invoke_button(win, "导出 CSV")
            self.assertEqual(calls.get("initialfile"), filename,
                             "%s 的默认文件名不对" % win.title())
            with open(path, "rb") as f:
                raw = f.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                            "%s 的 CSV 缺 UTF-8 BOM" % filename)
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
            self.assertEqual(rows[0], header, filename)
            self.assertGreater(len(rows), 1, "%s 只有表头没有数据行" % filename)
            win.destroy()

    def test_all_time_detail_merges_today_and_highlights_peak(self):
        """历史总量：今日实时并入当前月、峰值月高亮、最佳月取最大月"""
        import csv

        month = datetime.now().strftime("%Y-%m")
        self.app.store.query_monthly_totals = lambda limit=36: [
            (month, 1, 100, 5), ("2020-01", 2, 200, 7)]
        self.app.query_day_stats = lambda days=0: {"reqs": 7, "total": 700,
                                                   "cache_read": 70, "input": 0,
                                                   "output": 0, "cache_create": 0}
        win = self._build_window(CcBarTray.show_all_time_detail, "历史总量")
        self.assertTrue(self._find_by_text(win, "按月汇总"))
        self.assertTrue(self._find_by_text(win, month), "最佳月卡片没显示当前月")

        # 峰值月（合并后的当前月）铺主题色淡底
        highlight_bg = blend(Design.BRAND, Design.BACKGROUND, 0.10)
        frames = [w for w in self._walk(win) if isinstance(w, tk.Frame)]
        self.assertTrue(any(str(w.cget("bg")) == highlight_bg for w in frames),
                        "峰值月没有高亮底色")

        path = os.path.join(self.tmp, "alltime.csv")
        with mock.patch("tkinter.filedialog.asksaveasfilename", return_value=path), \
                mock.patch("tkinter.messagebox.showinfo", return_value=None):
            self._invoke_button(win, "导出 CSV")
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1], [month, "8", "800", "75"], "今日实时没并进当前月")
        self.assertEqual(rows[2], ["2020-01", "2", "200", "7"])
        win.destroy()

        # 当前月不在历史行里时要插一行（而不是丢掉今日实时）
        self.app.store.query_monthly_totals = lambda limit=36: [("2020-01", 2, 200, 7)]
        win = self._build_window(CcBarTray.show_all_time_detail, "历史总量")
        with mock.patch("tkinter.filedialog.asksaveasfilename", return_value=path), \
                mock.patch("tkinter.messagebox.showinfo", return_value=None):
            self._invoke_button(win, "导出 CSV")
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1], [month, "7", "700", "70"])
        win.destroy()

    def test_detail_window_geometry_memory(self):
        """窗口位置记忆：解析、还原、坏值/出屏/未映射回落默认"""
        self.assertEqual(CcBarTray._parse_geometry("520x620+100+50"), (520, 620, 100, 50))
        self.assertEqual(CcBarTray._parse_geometry("520x620-10+50"), (520, 620, -10, 50))
        self.assertEqual(CcBarTray._parse_geometry("520x620"), (520, 620, 0, 0))
        for bad in ("garbage", None, "", "0x620+1+1", "520x+1+1", 42):
            self.assertIsNone(CcBarTray._parse_geometry(bad), bad)

        # 关闭时存（直接拿保存函数，等价于 <Destroy> 回调）
        probe = tk.Toplevel(self.root)
        original_geometry = probe.geometry
        probe.geometry = lambda *a: "540x660+140+90"
        try:
            save = self.app._remember_window_geometry(probe, "hourly")
            save()
        finally:
            probe.geometry = original_geometry
            probe.destroy()
        self.assertEqual(self.app.settings.get("window_geometry_hourly"),
                         "540x660+140+90")

        # 打开时还原（未映射时 Tk 只报位置，尺寸显示为 1x1）
        probe = tk.Toplevel(self.root)
        self.assertTrue(self.app._apply_window_geometry(probe, "hourly", "520x620"))
        self.assertTrue(probe.geometry().endswith("+140+90"),
                        "没还原保存的位置：%s" % probe.geometry())
        probe.destroy()

        win = self._build_window(CcBarTray.show_hourly_detail, "每小时用量详情")
        self.assertTrue(win.geometry().endswith("+140+90"), win.geometry())
        win.destroy()

        # 坏值 / 出屏 / 未映射（1x1）都回落默认
        probe = tk.Toplevel(self.root)
        for bad in ("garbage", "1x1+0+0", "520x620+999999+999999", "", None):
            self.app.settings.set("window_geometry_hourly", bad)
            self.assertFalse(self.app._apply_window_geometry(probe, "hourly", "520x620"),
                             "坏几何串 %r 被当真了" % bad)
        probe.destroy()

        # 未映射（1x1）的几何串不落盘
        probe = tk.Toplevel(self.root)
        original_geometry = probe.geometry
        probe.geometry = lambda *a: "1x1+0+0"
        try:
            save = self.app._remember_window_geometry(probe, "week")
            save()
        finally:
            probe.geometry = original_geometry
            probe.destroy()
        self.assertIsNone(self.app.settings.get("window_geometry_week"))

    def test_hourly_detail_drag_readout(self):
        """拖选读数：按 x 取最近的数据点，显示「标签  数值」并钳制在画布内"""
        canvas = tk.Canvas(self.root, width=200, height=60, bg=Design.BACKGROUND,
                           highlightthickness=0)
        state = {"labels": ["a", "b", "c", "d"], "values": [1, 2, 300, 4]}
        readout = main.ChartReadout(canvas, state, kind="bars")
        canvas.winfo_width = lambda: 200
        canvas.winfo_height = lambda: 60

        self.assertEqual(readout.index_at(0), 0)
        self.assertEqual(readout.index_at(110), 2)
        self.assertEqual(readout.index_at(9999), 3)

        readout.show_at(110)
        items = canvas.find_withtag(main.ChartReadout.TAG)
        texts = [canvas.itemcget(i, "text") for i in items if canvas.type(i) == "text"]
        self.assertEqual(texts, ["c  300"], "读数文案不对：%s" % texts)
        self.assertTrue([i for i in items if canvas.type(i) == "line"], "缺少参考线")

        # 左右两端都不越界
        readout.show_at(1)
        item = [i for i in canvas.find_withtag(main.ChartReadout.TAG)
                if canvas.type(i) == "text"][0]
        self.assertGreaterEqual(canvas.bbox(item)[0], 2)
        readout.show_at(199)
        item = [i for i in canvas.find_withtag(main.ChartReadout.TAG)
                if canvas.type(i) == "text"][0]
        self.assertLessEqual(canvas.bbox(item)[2], 198)

        # 松开 / 空数据 / 画布没布局都不能抛
        readout._on_release()
        self.assertEqual(canvas.find_withtag(main.ChartReadout.TAG), ())
        state.clear()
        readout.show_at(50)
        self.assertEqual(canvas.find_withtag(main.ChartReadout.TAG), ())

        line = main.ChartReadout(canvas, {"labels": ["a", "b", "c"], "values": [1, 2, 3]},
                                 kind="line")
        self.assertEqual(line.index_at(100), 1)
        canvas.destroy()

    def test_chart_peak_highlight(self):
        """图表峰值高亮：峰值柱用警示色、峰值点加警示色圈，其余保持主题配色"""
        canvas = tk.Canvas(self.root, width=300, height=100, bg=Design.BACKGROUND,
                           highlightthickness=0)
        main.ChartCanvas.draw_bar_chart(canvas, [1, 5, 2], 300, 100,
                                        use_gradient=False, highlight_index=1)
        # 每根柱子两个图元（柱身 + 顶部圆角），峰值柱那两个都换成警示色
        fills = [canvas.itemcget(i, "fill") for i in canvas.find_all()]
        self.assertEqual(fills.count(Design.WARNING), 2, fills)
        self.assertEqual(fills.count(Design.BRAND), 4, fills)

        canvas.delete("all")
        main.ChartCanvas.draw_sparkline(canvas, [1, 5, 2], 300, 100,
                                        highlight_index=1)
        outlines = [canvas.itemcget(i, "outline") for i in canvas.find_all()
                    if canvas.type(i) == "oval"]
        self.assertEqual(outlines.count(Design.WARNING), 1, outlines)
        canvas.destroy()

    def test_add_months_handles_month_ends(self):
        """按月加减先归 1 号：3/31 减一个月不会撞上不存在的 2/31（旧实现会抛）"""
        d = datetime(2026, 3, 31)
        self.assertEqual(self.app._add_months(d, -1), datetime(2026, 2, 1))
        self.assertEqual(self.app._add_months(d, 1), datetime(2026, 4, 1))
        self.assertEqual(self.app._add_months(datetime(2026, 1, 15), -1),
                         datetime(2025, 12, 1))
        self.assertEqual(self.app._add_months(datetime(2026, 12, 5), 1),
                         datetime(2027, 1, 1))

    def test_led_red_threshold_forces_red_on_delta(self):
        """闪电 LED 红色门槛：增量 ≥ 门槛强制红；首次/低于门槛/回退都不红；0=关闭"""
        app = self.app
        app.icon = None
        red = usage_color(1.0)
        stats = {"reqs": 0, "input": 0, "output": 0, "cache_create": 0,
                 "cache_read": 0, "total": 0}
        app.query_day_stats = lambda days=0: dict(stats)
        app.settings.set("led_red_threshold", 1)          # 1 万 = 10_000

        def tick(total):
            stats["total"] = total
            app._last_icon_color = None                   # 强制重算颜色
            app.update_icon_color()
            return app._last_icon_color

        app._led_last_total = None
        self.assertNotEqual(tick(20_000_000), red, "首次没有基准不该标红")
        self.assertEqual(tick(20_000_000 + 10_000), red, "增量达到门槛要强制红")
        app._led_last_total = None
        self.assertNotEqual(tick(1_000_000), red)
        self.assertNotEqual(tick(1_000_000 + 9_999), red, "增量低于门槛不该标红")
        self.assertEqual(app._last_icon_color, main.today_usage_color(1_009_999))
        self.assertNotEqual(tick(100), red, "跨天回退不该标红")

        app.settings.set("led_red_threshold", 0)          # 关闭
        app._led_last_total = 1_000
        self.assertNotEqual(tick(2_000_000), red, "门槛为 0 时不该强制红")

    def test_format_credits_matches_mac(self):
        """积分格式化：整数不带小数点，小数两位，≥10 万走两位（mac fmtCredits）"""
        self.assertEqual(main.format_credits(12.0), "12")
        self.assertEqual(main.format_credits(12.5), "12.50")
        self.assertEqual(main.format_credits(100_000.0), "100000.00")
        self.assertEqual(main.format_credits(None), "0")
        self.assertEqual(CcBarTray._fmt_credits(3.0), "3")

    # ------------------------------------------------------------ 面板（mac PopoverRootView）

    def _build_popover(self):
        popover = main.PopoverWindow(self.app)
        body = tk.Frame(self.root, bg=Design.BACKGROUND)
        popover._build(body)
        return popover, body

    def test_popover_five_buttons_and_insights_keeps_panel(self):
        """按钮栏 5 格（复制/刷新/洞察/设置/退出）；洞察故意不收面板，设置先收"""
        popover, body = self._build_popover()
        self.assertEqual([b[1] for b in popover._buttons],
                         ["复制", "刷新", "洞察", "设置", "退出"])
        usable = popover.WIDTH - 2 - 2 * popover.PAD
        self.assertGreaterEqual(usable / 5, 40, "5 格按钮在 300px 面板里放不下")

        class FakeWin:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

            def focus_get(self):
                return None

        insights = [b for b in popover._buttons if b[1] == "洞察"][0]
        popover.win = FakeWin()
        insights[2]()
        self.assertIsNotNone(popover.win, "「洞察」把面板收起来了")
        self.assertFalse(popover.win.destroyed)

        settings = [b for b in popover._buttons if b[1] == "设置"][0]
        fake = FakeWin()
        popover.win = fake
        settings[2]()
        self.assertIsNone(popover.win, "「设置」应先收起面板")
        self.assertTrue(fake.destroyed)
        body.destroy()

    def test_popover_all_time_row_targets_all_time_window(self):
        """「历史总量」行必须开历史总量窗口（此前误开近30天）"""
        _popover, body = self._build_popover()
        rows = [w for w in self._walk(body)
                if getattr(w, "_row_command", None) is not None
                and any(self._text_of(c) == "历史总量" for c in self._walk(w))]
        self.assertEqual(len(rows), 1, "面板里找不到「历史总量」行")
        target = getattr(rows[0]._row_command, "_target", None)
        self.assertEqual(target, self.app.show_all_time_detail)
        self.assertNotEqual(target, self.app.show_monthly_detail)
        body.destroy()

    def test_popover_today_card_shows_credits_prediction_sessions(self):
        """今日卡：积分缀在大数字后、速率预测行、今日会话行"""
        self.app.store.query_today_credits = lambda: 42.0
        self.app.query_work_hours = lambda: "4.0"
        _popover, body = self._build_popover()
        texts = self._texts(body)
        self.assertTrue(any(t == "42 积分" for t in texts), texts)
        total = (self.app.query_day_stats(0) or {"total": 0})["total"]
        expected = "按当前速率到 24:00 约 %s" % Design.fmt_tokens(int(total / 4.0 * 24))
        self.assertIn(expected, texts)
        self.assertTrue(any("今日会话" in t for t in texts), texts)
        body.destroy()

    def test_popover_prediction_needs_work_hours(self):
        """工时 ≤ 0.2h 不外推（避免清早给出爆表预测）"""
        today = {"total": 1000}
        self.assertIsNone(main.PopoverWindow._prediction_text(today, None))
        self.assertIsNone(main.PopoverWindow._prediction_text(today, "0.1"))
        self.assertIsNone(main.PopoverWindow._prediction_text(today, "abc"))
        self.assertEqual(main.PopoverWindow._prediction_text(today, "2.0"),
                         "按当前速率到 24:00 约 %s" % Design.fmt_tokens(12_000))

    def test_popover_hour_values_matches_mac_hour_points(self):
        """sparkline 序列：起点 min(首个有数据小时, 9)，终点最后一个有数据小时"""
        def row(tokens):
            return {"reqs": 1, "output": tokens, "input": 0, "cache_read": 0}

        self.assertEqual(main.PopoverWindow._hour_values({}), [])
        # 只有一个数据点且落在 9 点前 → 展开也只有 1 个点，不画线
        self.assertEqual(main.PopoverWindow._hour_values({3: row(5)}), [])
        # 单个数据点在下午：锚到 9 点起线（mac hourPoints 同款）
        self.assertEqual(main.PopoverWindow._hour_values({14: row(100)}),
                         [0, 0, 0, 0, 0, 100])
        values = main.PopoverWindow._hour_values({14: row(100), 15: row(200)})
        self.assertEqual(len(values), 7)
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 200)
        self.assertEqual(main.PopoverWindow._hour_values({3: row(5), 4: row(7)}), [5, 7])

    def test_popover_sparkline_canvas_only_with_two_points(self):
        """≥2 个点才建 sparkline 画布"""
        _popover, body = self._build_popover()
        popover = main.PopoverWindow(self.app)
        self.app.query_hourly_stats = lambda days_ago=0: {3: {"reqs": 1, "output": 9,
                                                             "input": 0, "cache_read": 0}}
        self.assertIsNone(popover._hourly_sparkline(body))
        self.app.query_hourly_stats = lambda days_ago=0: {
            3: {"reqs": 1, "output": 5, "input": 0, "cache_read": 0},
            15: {"reqs": 1, "output": 7, "input": 0, "cache_read": 0},
        }
        canvas = popover._hourly_sparkline(body)
        self.assertIsNotNone(canvas)
        self.assertEqual(int(canvas.cget("height")), 42)
        body.destroy()

    def test_popover_empty_state_never_raises(self):
        """面板没数据也要建完（全部行隐去，按钮栏仍在）"""
        self.app.query_day_stats = lambda days=0: None
        self.app.query_total_stats = lambda: None
        self.app.query_model_breakdown = lambda: None
        self.app.query_work_hours = lambda: None
        self.app.query_hourly_stats = lambda days_ago=0: None
        popover = main.PopoverWindow(self.app)
        body = tk.Frame(self.root, bg=Design.BACKGROUND)
        popover._build(body)
        self.assertEqual([b[1] for b in popover._buttons],
                         ["复制", "刷新", "洞察", "设置", "退出"])
        self.assertTrue(self._find_by_text(body, "📊 今日暂无数据"))
        body.destroy()

    def test_build_menu_has_all_time_item(self):
        """托盘菜单的「历史总量」要开历史总量窗口（不是近30天）"""
        class FakeMenuItem:
            def __init__(self, text="", action=None, **kwargs):
                self.text = text
                self.action = action
                self.kwargs = kwargs

        class FakeMenu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        fake = types.SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem)
        self.app.check_warning = lambda stats: None       # 不碰 ~/.ccbar/notified.txt
        with mock.patch.object(main, "pystray", fake):
            items = self.app.build_menu()
        hist = [i for i in items
                if getattr(i, "text", "").startswith("📈 历史总量")]
        self.assertEqual(len(hist), 1, [getattr(i, "text", None) for i in items])
        self.assertEqual(hist[0].action, self.app.show_all_time_detail)

    # ------------------------------------------------------------ 积分页 + Trae 设置行

    def _tab_frame(self, nb, title):
        for tab_id in nb.tabs():
            if nb.tab(tab_id, "text") == title:
                return nb.nametowidget(tab_id)
        self.fail("找不到页签 %s" % title)

    def test_credits_tab_without_trae_shows_hint(self):
        """积分页：三张卡 + 无 Trae 用量时的灰字提示 + 脚注"""
        _win, nb = self._build_insights()
        self.assertEqual([nb.tab(t, "text") for t in nb.tabs()][-1], "积分")
        texts = self._texts(self._tab_frame(nb, "积分"))
        for expect in ("今日积分", "近 7 天", "近 30 天", "近 30 天积分走势",
                       "接入 Trae 并产生用量后展示积分消耗",
                       "积分为 Trae 官方计费口径；历史数据自接入起最多回溯 90 天"):
            self.assertTrue(any(expect in t for t in texts),
                            "积分页缺少 %r：%s" % (expect, texts))

    def test_credits_tab_with_trae_data(self):
        """积分页：官方账单余额卡（已用/共 + 百分比 + 进度条）与格式化后的数值"""
        self.app.store.query_credits_sum = lambda days: {0: 12.5, 7: 100.0, 30: 3000.0}[days]
        self.app.store.query_credits_daily = lambda days: [("2026-08-01", 1.0),
                                                           ("2026-08-02", 2.0)]
        self.app.store.trae_ent_summary = lambda: (250.0, 1000.0)
        _win, nb = self._build_insights()
        frame = self._tab_frame(nb, "积分")
        texts = self._texts(frame)
        self.assertTrue(any("积分余额（官方账单）" in t for t in texts))
        self.assertIn("已用 / 共 1000", texts)
        self.assertIn("25%", texts)
        self.assertIn("12.50", texts)
        self.assertIn("3000", texts)
        self.assertFalse(any("接入 Trae 并产生用量后展示积分消耗" in t for t in texts))

    def test_settings_window_has_trae_row_and_saves_it(self):
        """设置页数据源区的 Trae 行：勾选 + sessionid + 状态行，保存时带上两个键"""
        import autostart

        CcBarTray.show_settings.__wrapped__(self.app)
        wins = self._toplevels("ccBar 设置")
        self.assertEqual(len(wins), 1, "设置窗口没建出来")
        win = wins[0]
        self.assertTrue(self._find_by_text(win, "Trae"))
        self.assertTrue(self._find_by_text(win, "粘贴 trae.cn 登录后的 sessionid cookie 值"))

        entries = [w for w in self._walk(win) if isinstance(w, tk.Entry)]
        trae_entries = [e for e in entries
                        if any(self._text_of(c) == "Trae"
                               for c in e.master.winfo_children())]
        self.assertEqual(len(trae_entries), 1, "找不到 Trae 的 sessionid 输入框")
        trae_entries[0].insert(0, "session-abc")

        captured = {}
        original_apply = self.app.settings.apply

        def spy_apply(updates):
            captured.update(updates)
            return original_apply(updates)

        original_autostart = autostart.apply
        original_connect = self.app.connect_store
        self.app.settings.apply = spy_apply
        self.app.connect_store = lambda: None
        autostart.apply = lambda flag: None
        try:
            with mock.patch("tkinter.messagebox.showinfo", return_value=None), \
                    mock.patch("tkinter.messagebox.showerror", return_value=None):
                self._invoke_button(win, "保存")
        finally:
            autostart.apply = original_autostart
            self.app.settings.apply = original_apply
            self.app.connect_store = original_connect

        self.assertIn("trae_enabled", captured)
        self.assertEqual(captured["trae_sessionid"], "session-abc")
        self.assertEqual(self.app.settings.get("trae_sessionid"), "session-abc")


if __name__ == "__main__":
    unittest.main()
