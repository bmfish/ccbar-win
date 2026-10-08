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
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win_stubs  # noqa: E402

win_stubs.install()

import main  # noqa: E402
import app_settings  # noqa: E402
import themes  # noqa: E402
from main import CcBarTray, Design  # noqa: E402
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

    EXPECTED_TABS = ["费用", "洞察", "分享", "渠道", "流水"]

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
        # 历史两天 + 今日实时，保证各页都有数据可画
        rows = [
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
        """分享页同时预览战报卡（640x420）与周报卡（460x360）"""
        self.assertEqual(self.app._share_card_png().size, (640, 420))
        self.assertEqual(self.app._weekly_card_png().size, (460, 400))

    def test_timeline_page_has_day_nav_and_csv_export(self):
        """流水页新增：任意日期回看 + 导出 CSV"""
        _win, nb = self._build_insights()
        texts = self._texts(nb.nametowidget(nb.tabs()[4]))
        for expect in ("‹ 前一天", "后一天 ›", "今天", "导出 CSV"):
            self.assertTrue(any(expect in t for t in texts), f"流水页缺少 {expect}")

    def test_insights_restores_last_tab(self):
        """页签记忆：按设置选中，非法值回落「费用」"""
        self.app.settings.set("insights_last_page", "分享")
        win, nb = self._build_insights()
        self.assertEqual(nb.tab(nb.select(), "text"), "分享")
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

    def test_daily_tokens_backs_the_share_card(self):
        """分享卡折线的数据源：历史来自 daily_agg（昨天），今天走实时"""
        from datetime import datetime, timedelta

        rows = self.app.store.query_daily_tokens(7)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertIn(today, [d for d, _ in rows], "今日实时行必须出现")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertIn(yesterday, [d for d, _ in rows], "昨天的历史行必须出现")


if __name__ == "__main__":
    unittest.main()
