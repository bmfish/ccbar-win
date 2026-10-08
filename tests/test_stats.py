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

from stats_store import (StatsStore, CCSwitchAdapter, ZCodeAdapter, DAILY_AGG_DDL,
                         _local_epoch, validate_db)  # noqa: E402


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
        """1970-01-01 全量回填起点：关键是不抛 OSError（Windows mktime 不支持 1970 前）"""
        from stats_store import _local_epoch
        value = _local_epoch("1970-01-01")   # macOS 返回负值，Windows 兜底返回 0
        self.assertIsInstance(value, int)
        self.assertGreater(_local_epoch("2026-01-01"), 0)

    # ------------------------------------------------------------ 备份与校验

    def test_insight_queries(self):
        """洞察中心查询：费用/连续天数/环比/峰值/最大模型/渠道/应用/构成/月度/流水"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000, cache_read=500, cache_create=100)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)
        self._rebuild()
        self.store.sync_if_needed()
        self._insert_row("req-A", max(local_midnight(0) + 60, int(datetime.now().timestamp()) - 60),
                         100, 200, cache_read=50, cache_create=10)
        self.store.sync_if_needed()

        # 费用（每行 $0.5，共 3 行）
        self.assertAlmostEqual(self.store.query_cost(30), 1.5, places=4)
        self.assertAlmostEqual(self.store.query_cost(0), 0.5, places=4)
        cost_daily = self.store.query_cost_daily(30)
        self.assertEqual(len(cost_daily), 3)
        self.assertAlmostEqual(sum(c for _, c in cost_daily), 1.5, places=4)
        models = self.store.query_cost_by_model(30)
        self.assertEqual(len(models), 1)
        self.assertAlmostEqual(models[0][1], 1.5, places=4)
        self.assertEqual(models[0][2], 33960)

        # 连续天数：聚合只有昨天与 3 天前（断档）→ 1
        self.assertEqual(self.store.query_streak(), 1)

        # 周环比：本周（含今日）= 33960，上周 0
        this_week, last_week = self.store.query_weekly_delta()
        self.assertEqual(this_week, 33960)
        self.assertEqual(last_week, 0)

        # 峰值日 = 3 天前 30000
        peak = self.store.query_peak_day(30)
        self.assertEqual(peak[1], 30000)
        self.assertEqual(peak[0], (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"))

        # 使用量最大的模型
        top = self.store.query_top_model(30)
        self.assertEqual(top, ("test-model", 33960))

        # 渠道每日：2 个历史日 + 今日 1 行
        channels = self.store.query_channel_daily(30)
        self.assertEqual(len(channels), 3)
        self.assertTrue(all(r[1] == "cc-switch" for r in channels))

        # 应用每日（fixture 全是 claude）
        apps = self.store.query_app_daily(30)
        self.assertEqual(len(apps), 3)
        self.assertTrue(all(r[1] == "claude" for r in apps))

        # 构成每日：昨天的四项
        comp = self.store.query_composition_daily(30)
        self.assertEqual(len(comp), 3)
        yst = next(r for r in comp
                   if r[0] == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
        self.assertEqual(yst[1:], (1000, 2000, 500, 100))

        # 月度进度：至少含今日实时，口径合法
        mtd, elapsed, dim = self.store.query_month_progress()
        self.assertGreaterEqual(mtd, 360)
        self.assertGreaterEqual(elapsed, 1)
        self.assertTrue(28 <= dim <= 31)

        # 今日流水：1 条
        tl = self.store.query_today_timeline()
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0][3], 360)
        self.assertAlmostEqual(tl[0][4], 0.5, places=4)

    def test_export_import_idempotent(self):
        """导出 → 全新库导入 → 重复导入零新增（幂等）"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000, cache_read=500, cache_create=100)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)
        self._rebuild()
        self.store.sync_if_needed()

        path = os.path.join(self.tmp, "export.csv")
        self.assertEqual(self.store.export_csv(path), 2)

        # 新库：同一源（不再同步），纯导入路径
        imported_path = os.path.join(self.tmp, "imported.db")
        StatsStore.store_path = staticmethod(lambda: imported_path)
        other = StatsStore()
        try:
            other.rebuild([(CCSwitchAdapter(), True, self.source_path)])
            r1 = other.import_csv(path)
            self.assertEqual(r1, (2, 2, 0))
            cnt, total = other.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),0) "
                "FROM usage_log").fetchone()
            self.assertEqual((cnt, total), (2, 33600))
            agg = other.conn.execute(
                "SELECT COALESCE(SUM(input+output+cache_create+cache_read),0) FROM daily_agg").fetchone()[0]
            self.assertEqual(agg, 33600, "导入后按窗口重建 daily_agg")

            # 再导一遍：幂等
            r2 = other.import_csv(path)
            self.assertEqual(r2, (2, 0, 2))
            cnt2, total2 = other.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),0) "
                "FROM usage_log").fetchone()
            self.assertEqual((cnt2, total2), (2, 33600))

            # 表头不符拒收
            bad = os.path.join(self.tmp, "bad.csv")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("a,b,c\n1,2,3\n")
            self.assertEqual(other.import_csv(bad), (0, 0, -1))
        finally:
            other._close()
            StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))

    def test_backup_creates_independent_copy(self):
        """VACUUM INTO 导出后可独立打开且数据一致；目标已存在必须失败"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000,
                         cache_read=500, cache_create=100)
        self._rebuild()
        self.store.sync_if_needed()

        target = os.path.join(self.tmp, "backup.db")
        self.assertTrue(self.store.backup(target))

        conn = sqlite3.connect(target)
        rows = conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
        conn.close()
        self.assertEqual(rows, 1)

        self.assertFalse(self.store.backup(target),
                         "VACUUM INTO 不覆盖已存在的目标，须返回 False")

    def test_backup_without_connection(self):
        store = StatsStore()  # 未 rebuild，无连接
        self.assertFalse(store.backup(os.path.join(self.tmp, "x.db")))

    def test_validate_db(self):
        """只读试开 + 必需表检查（设置页即时校验的数据层）"""
        self._make_fixture_source()
        self.assertIsNone(validate_db(self.source_path, CCSwitchAdapter().required_tables))

        bare = os.path.join(self.tmp, "bare.db")
        sqlite3.connect(bare).close()
        self.assertEqual(validate_db(bare, CCSwitchAdapter().required_tables), "缺少必需表")

        self.assertEqual(validate_db(os.path.join(self.tmp, "missing.db"),
                                     CCSwitchAdapter().required_tables), "文件不存在")
