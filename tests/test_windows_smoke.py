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
from main import CcBarTray  # noqa: E402
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
        self.app.settings = {
            "refresh_interval": 60,
            "db_path": self.source_path,
            "ccswitch_enabled": True,
            "zcode_enabled": False,
            "zcode_path": "",
            "warning_threshold": 50,
            "warning_enabled": True,
            "notify_interval": 1000,
        }
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

    # ------------------------------------------------------------ 用例

    def test_insights_builds_all_tabs(self):
        # 只构建、不跑事件循环：macOS Tk 8.5 在非 app bundle 进程里
        # update()/update_idletasks() 会挂住（Windows 无此问题），
        # 而本用例要断言的是"构建过程中没抛异常 + 控件树完整"，两者都不需要泵事件。
        # 绕过 @_on_gui：直接在当前线程跑窗口构建体
        CcBarTray.show_insights.__wrapped__(self.app)

        toplevels = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        insights = [w for w in toplevels if w.title() == "洞察中心"]
        self.assertEqual(len(insights), 1, "洞察中心窗口没建出来")

        nb = self._find_notebook(insights[0])
        self.assertIsNotNone(nb, "洞察中心里没有 Notebook")

        labels = [nb.tab(t, "text") for t in nb.tabs()]
        self.assertEqual(labels, self.EXPECTED_TABS,
                         f"页签不完整：实际 {labels}（构建中途抛异常会缺后面的页）")

        # 每个页签里都要有内容（费用页的图表就是原 bug 的爆点）
        for tab_id in nb.tabs():
            frame = nb.nametowidget(tab_id)
            self.assertTrue(frame.winfo_children(),
                            f"页签 {nb.tab(tab_id, 'text')} 是空的")

    def test_share_tab_renders_card(self):
        """分享页依赖 store.query_daily_tokens（曾缺失导致 AttributeError）"""
        img = self.app._share_card_png()
        self.assertEqual(img.size, (640, 420))


if __name__ == "__main__":
    unittest.main()
