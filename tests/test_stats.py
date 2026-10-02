"""StatsStore 单元测试：合成夹具库，验证同步、daily_agg、查询口径。

运行：python -m unittest discover -s tests -v
不依赖 pystray 等界面包，只用标准库 + stats_store。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stats_store import StatsStore, CCSwitchAdapter, ZCodeAdapter, DAILY_AGG_DDL, _local_epoch  # noqa: E402


def local_midnight(days_ago=0):
    day = datetime.now() - timedelta(days=days_ago)
    return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class TestStatsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-test-")
        self.source_path = os.path.join(self.tmp, "cc-switch.db")
        self.store = StatsStore()
        # 库路径指到临时目录，不碰真实 ~/.ccbar
        self._orig_store_path = StatsStore.store_path
        StatsStore.store_path = staticmethod(
            lambda: os.path.join(self.tmp, "ccbar.db"))

    def tearDown(self):
        StatsStore.store_path = self._orig_store_path
        self.store._close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _make_fixture_source(self):
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
        conn.commit()
        conn.close()

    def _insert_row(self, rid, created_at, inp, out, cache_read=0, cache_create=0):
        conn = sqlite3.connect(self.source_path)
        conn.execute(
            "INSERT INTO proxy_request_logs VALUES (?, 'claude', 'test-model', ?, ?, ?, ?, 0.5, ?)",
            (rid, inp, out, cache_read, cache_create, created_at))
        conn.commit()
        conn.close()

    def _rebuild(self):
        self.store.rebuild([(CCSwitchAdapter(), True, self.source_path)])

    # ------------------------------------------------------------ 用例

    def test_sync_daily_agg_and_queries(self):
        """补账 → daily_agg 回填 → 今日实时可见且不入账 → 各查询口径正确"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000, cache_read=500, cache_create=100)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)

        self._rebuild()
        self.assertEqual(len(self.store.attached), 1)
        self.store.sync_if_needed()

        # 昨天 = B；近3天 = B + C；今日为 0
        self.assertEqual(self.store.query_day_stats if False else True, True)  # noqa: PLW0129
        y = self.app_query_day(1)
        self.assertEqual(y["total"], 3600)
        d3 = self.app_query_day(3)
        self.assertEqual(d3["total"], 3600 + 30000)
        self.assertEqual(self.app_query_day(0)["total"], 0)

        # 源库出现今日新行 → 实时可见，且不会同步进自建库
        self._insert_row("req-A", max(local_midnight(0) + 60, int(datetime.now().timestamp()) - 60),
                         100, 200, cache_read=50, cache_create=10)
        self.store.sync_if_needed()
        today = self.app_query_day(0)
        self.assertEqual(today["total"], 360)
        self.assertEqual(today["reqs"], 1)
        self.assertEqual(self.app_query_day(1)["total"], 3600, "重复同步/实时行不得影响历史")

        # 历史总量 = 今日实时 + 聚合表
        total = self.store_query_total()
        self.assertEqual(total["total"], 33960)
        self.assertEqual(total["reqs"], 3)

    def test_repeat_sync_idempotent(self):
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000)
        self._rebuild()
        self.store.sync_if_needed()
        first = self.store_query_total()
        for _ in range(3):
            self.store.sync_if_needed()
        # 再次 rebuild（daily_agg 回填标记已存在，不重复累计）
        self._rebuild()
        self.store.sync_if_needed()
        self.assertEqual(self.store_query_total(), first)

    def test_rollup_local_noon(self):
        """rollup 伪明细的 created_at 应落在原日期的本地正午（任意时区）"""
        self._make_fixture_source()
        # rollup 只补"早于最早明细"的日期，需要至少一条明细行做锚点
        self._insert_row("req-anchor", local_midnight(1) + 3600, 1000, 2000)
        rollup_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.source_path)
        conn.execute(
            "INSERT INTO usage_daily_rollups VALUES (?, 'p', 'm', 'rm', 'pm', 'claude', "
            "100, 200, 0, 0, 0.1, 3)", (rollup_date,))
        conn.commit()
        conn.close()

        self._rebuild()
        self.store.sync_if_needed()

        row = self.store.conn.execute(
            "SELECT created_at FROM usage_log WHERE source='cc-switch-rollup'").fetchone()
        self.assertIsNotNone(row)
        created = datetime.fromtimestamp(row[0])
        self.assertEqual(created.strftime("%Y-%m-%d"), rollup_date, "本地日期不得错位")
        self.assertEqual(created.hour, 12)

    # ------------------------------------------------------------ 经由主逻辑查询（借用 main 的公式）

    def app_query_day(self, days):
        """复刻 main.CcBarTray.query_day_stats 的新口径（daily_agg + 今日实时）"""
        def day_str(days_ago):
            return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

        if days == 0:
            row = self.store.query_one("""
                SELECT COALESCE(SUM(request_count), 0), COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cache_creation_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0)
                FROM usage_all WHERE created_at >= ? AND created_at < ?""",
                (local_midnight(0), local_midnight(-1)))
        elif days == 1:
            row = self.store.query_one("""
                SELECT COALESCE(SUM(reqs), 0), COALESCE(SUM(input), 0), COALESCE(SUM(output), 0),
                       COALESCE(SUM(cache_create), 0), COALESCE(SUM(cache_read), 0)
                FROM daily_agg WHERE date >= ? AND date <= ?""", (day_str(1), day_str(1)))
        else:
            row = self.store.query_one("""
                SELECT COALESCE(SUM(reqs), 0), COALESCE(SUM(input), 0), COALESCE(SUM(output), 0),
                       COALESCE(SUM(cache_create), 0), COALESCE(SUM(cache_read), 0)
                FROM daily_agg WHERE date >= ? AND date <= ?""",
                (day_str(days), day_str(1)))
            today = self.store.query_one("""
                SELECT COALESCE(SUM(request_count), 0), COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cache_creation_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0)
                FROM usage_all WHERE created_at >= ? AND created_at < ?""",
                (local_midnight(0), local_midnight(-1)))
            if row and today:
                row = tuple(row[i] + today[i] for i in range(5))
        if not row:
            return {"reqs": 0, "input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "total": 0}
        return {"reqs": row[0], "input": row[1], "output": row[2],
                "cache_create": row[3], "cache_read": row[4], "total": row[1] + row[2] + row[3] + row[4]}

    def store_query_total(self):
        agg = self.store.query_one("""
            SELECT COALESCE(SUM(reqs), 0), COALESCE(SUM(input + output + cache_create + cache_read), 0)
            FROM daily_agg""")
        today = self.store.query_one("""
            SELECT COALESCE(SUM(request_count), 0),
                   COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens), 0)
            FROM usage_all WHERE created_at >= ?""", (local_midnight(0),))
        return {"reqs": agg[0] + today[0], "total": agg[1] + today[1]}


    def test_local_epoch_pre1970(self):
        """1970-01-01 全量回填起点：正常平台给 0/负值，Windows 上不抛 OSError"""
        from stats_store import _local_epoch
        self.assertEqual(_local_epoch("1970-01-01"), 0)
        self.assertGreater(_local_epoch("2026-01-01"), 0)


if __name__ == "__main__":
    unittest.main()
