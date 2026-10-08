"""真建 tkinter 窗口的公共夹具（窗口冒烟测试 + i18n 英文模式遍历共用）。

窗口构建本身不需要事件循环：macOS Tk 8.5 在非 app bundle 进程里跑
update()/focus_force() 会挂住，Windows 上不存在这个问题，所以这里一律不泵事件。
无显示环境（CI 的 ubuntu runner）自动跳过。

子类用 LANGUAGE 选界面语言（"zh"/"en"）；语言是全局状态，tearDown 一律复位成
中文，免得泄漏给别的测试文件（它们断言的是中文文案）。
"""
import os
import queue
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

import app_settings  # noqa: E402
import l10n  # noqa: E402
import main  # noqa: E402
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
class TrayFixture(unittest.TestCase):
    """临时统计库 + 真设置对象 + 建窗助手（不绕过 __init__ 之外的真实路径）。"""

    # 子类可覆盖：界面语言（"zh" 中文 / "en" 英文）
    LANGUAGE = "zh"

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
        # 界面语言按 LANGUAGE 固定；语言是全局状态，tearDown 统一复位成中文
        self.app.settings.set("app_language", self.LANGUAGE)
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
        l10n.set_language("zh")
        CcBarTray._bring_to_front = self._orig_front
        self.app.store._close()
        StatsStore.store_path = self._orig_store_path
        shutil.rmtree(self.tmp, ignore_errors=True)

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
        title = l10n.L("洞察中心")      # 标题跟着当前语言走
        insights = [w for w in toplevels if w.title() == title]
        self.assertEqual(len(insights), 1, "%s 窗口没建出来" % title)
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

    def _build_popover(self):
        popover = main.PopoverWindow(self.app)
        body = tk.Frame(self.root, bg=Design.BACKGROUND)
        popover._build(body)
        return popover, body

    def _tab_frame(self, nb, title):
        for tab_id in nb.tabs():
            if nb.tab(tab_id, "text") == title:
                return nb.nametowidget(tab_id)
        self.fail("找不到页签 %s" % title)


if __name__ == "__main__":
    unittest.main()
