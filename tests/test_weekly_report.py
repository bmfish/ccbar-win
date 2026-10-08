"""周报调度逻辑：日期窗口、幂等文件名、补生成、通知时机。

不依赖 tkinter/PIL——渲染和通知都是注入的，方便单测。
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_report as wr  # noqa: E402


class FakeStore:
    """记录被查过的窗口，按窗口返回预设数据"""

    def __init__(self, total=7000, reqs=7, daily=None):
        self.total = total
        self.reqs = reqs
        self.daily = daily
        self.calls = []

    def query_window_stats(self, days_ago_from, days_ago_to):
        self.calls.append(("window", days_ago_from, days_ago_to))
        if self.total <= 0:
            return None
        return {"total": self.total, "reqs": self.reqs}

    def query_daily_tokens_between(self, days_ago_from, days_ago_to):
        self.calls.append(("daily", days_ago_from, days_ago_to))
        if self.daily is not None:
            return self.daily
        days = list(range(days_ago_from, days_ago_to - 1, -1))
        return [("2026-01-%02d" % (i + 1), 1000) for i, _ in enumerate(days)]


class TestWindowMath(unittest.TestCase):
    """窗口必须与 macOS 版 min(...) 口径一致：报告周 = 上一个完整周"""

    def test_window_for_each_weekday(self):
        # 2026-08-03 是周一
        monday = datetime(2026, 8, 3, 10, 0)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(wr.last_week_window(monday), (7, 1))
        for offset, expected in enumerate([(7, 1), (8, 2), (9, 3), (10, 4),
                                           (11, 5), (12, 6), (13, 7)]):
            now = monday + timedelta(days=offset)
            self.assertEqual(wr.last_week_window(now), expected,
                             f"{now:%Y-%m-%d} 的窗口不对")

    def test_report_key_is_this_week_monday(self):
        # 周一当天：报告周是"上周一~上周日"，文件名日期就是今天（本周一）
        monday = datetime(2026, 8, 3, 22, 30)
        self.assertEqual(wr.report_key(monday), "2026-08-03")
        # 同周周三 / 周日补生成，仍然是同一个文件名（周一没开机也不错过）
        for offset in (2, 6):
            now = monday + timedelta(days=offset)
            self.assertEqual(wr.report_key(now), "2026-08-03")
            self.assertEqual(wr.file_name(now), "ccbar-weekly-2026-08-03.png")

    def test_date_range_covers_last_full_week(self):
        # 2026-08-03 周一 → 报告周 2026-07-27(一) ~ 2026-08-02(日)
        text = wr.date_range_text(datetime(2026, 8, 3, 9, 0))
        self.assertEqual(text, "2026年7月27日 ~ 2026年8月2日")
        english = wr.date_range_text(datetime(2026, 8, 3, 9, 0), english=True)
        self.assertEqual(english, "Jul 27, 2026 ~ Aug 2, 2026")

    def test_trend_labels_are_seven_days_ascending(self):
        labels = wr.trend_labels(datetime(2026, 8, 3, 9, 0))
        self.assertEqual(len(labels), 7)
        self.assertEqual(labels[0], "07-27")
        self.assertEqual(labels[-1], "08-02")
        self.assertEqual(labels, sorted(labels))


class TestGenerateIfNeeded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-weekly-")
        self.now = datetime(2026, 8, 3, 9, 0)     # 周一
        self.notified = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _renderer(self, calls=None, payload=b"PNGDATA"):
        def render(date_text, total, reqs, peak, trend):
            if calls is not None:
                calls.append({"date_text": date_text, "total": total,
                              "reqs": reqs, "peak": peak, "trend": trend})
            return payload
        return render

    def test_generates_then_is_idempotent(self):
        store = FakeStore()
        calls = []
        path = wr.generate_if_needed(store, self._renderer(calls),
                                     output_dir=self.tmp, now=self.now,
                                     notify=lambda t, b: self.notified.append((t, b)))
        self.assertTrue(path and path.endswith("ccbar-weekly-2026-08-03.png"))
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"PNGDATA")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["total"], 7000)
        self.assertEqual(calls[0]["reqs"], 7)
        self.assertEqual(len(calls[0]["trend"]), 7)
        self.assertEqual(self.notified, [("上周周报已生成",
                                          "已存到 CCBar 周报目录，点击打开洞察中心查看")])

        # 同周再跑（含补生成场景）→ 跳过，不再渲染也不再通知
        path2 = wr.generate_if_needed(store, self._renderer(calls),
                                      output_dir=self.tmp,
                                      now=self.now + timedelta(days=2),
                                      notify=lambda t, b: self.notified.append((t, b)))
        self.assertIsNone(path2)
        self.assertEqual(len(calls), 1, "文件已存在不得重复渲染")
        self.assertEqual(len(self.notified), 1, "不得重复通知")

    def test_skips_when_week_has_no_usage(self):
        store = FakeStore(total=0)
        calls = []
        path = wr.generate_if_needed(store, self._renderer(calls),
                                     output_dir=self.tmp, now=self.now)
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp), [], "没用量不落文件")
        self.assertEqual(calls, [], "没数据不必渲染")

    def test_renderer_failure_writes_nothing(self):
        store = FakeStore()
        path = wr.generate_if_needed(store, lambda *a: None,
                                     output_dir=self.tmp, now=self.now)
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_auto_off_is_noop(self):
        store = FakeStore()
        path = wr.generate_if_needed(store, self._renderer(), output_dir=self.tmp,
                                     now=self.now, auto=False)
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(store.calls, [], "关闭时不该查库")

    def test_directory_is_created(self):
        nested = os.path.join(self.tmp, "CCBar 周报")
        path = wr.generate_if_needed(FakeStore(), self._renderer(),
                                     output_dir=nested, now=self.now)
        self.assertTrue(os.path.isdir(nested))
        self.assertTrue(os.path.exists(path))

    def test_notify_failure_does_not_lose_report(self):
        def boom(title, body):
            raise RuntimeError("toast 挂了")
        path = wr.generate_if_needed(FakeStore(), self._renderer(),
                                     output_dir=self.tmp, now=self.now, notify=boom)
        self.assertTrue(os.path.exists(path), "通知失败不能影响周报落盘")

    def test_output_dir_default_is_documents(self):
        self.assertTrue(wr.directory().endswith(os.path.join("Documents", "CCBar 周报")))


if __name__ == "__main__":
    unittest.main()
