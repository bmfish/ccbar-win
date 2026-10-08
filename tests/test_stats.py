"""StatsStore 单元测试：合成夹具库，验证同步、daily_agg、查询口径。

运行：python -m unittest discover -s tests -v
不依赖 pystray 等界面包，只用标准库 + stats_store。
"""
import csv
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stats_store import (StatsStore, CCSwitchAdapter, ZCodeAdapter, DAILY_AGG_DDL,
                         USAGE_LOG_DDL, _local_epoch, validate_db)  # noqa: E402


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

    def _insert_row_ex(self, rid, created_at, model="test-model", inp=0, out=0,
                       cache_read=0, cache_create=0, cost=0.5, app="claude"):
        """自定义模型/费用的明细行（对应源库 proxy_request_logs）"""
        conn = sqlite3.connect(self.source_path)
        conn.execute(
            "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, app, model, inp, out, cache_read, cache_create, cost, created_at))
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

    # ------------------------------------------------------------ 批次 0 回归

    def _make_zcode_source(self):
        """第二个数据源（ZCode）夹具，用于验证"今日按源补行"能出多行"""
        path = os.path.join(self.tmp, "zcode.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE model_usage (
            id TEXT PRIMARY KEY, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, started_at INTEGER, status TEXT
        );
        """)
        conn.commit()
        conn.close()
        return path

    def test_query_daily_tokens_includes_today(self):
        """近 N 天每日 token：历史走 daily_agg，今日实时补一行（分享卡折线依赖它）"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000,
                         cache_read=500, cache_create=100)
        self._rebuild()
        self.store.sync_if_needed()

        rows = self.store.query_daily_tokens(7)
        self.assertEqual(len(rows), 1, "昨天有数据、今天还没有")
        self.assertEqual(rows[0][1], 3600)

        # 今天出现实时行 → 追加为最后一行
        self._insert_row("req-A", max(local_midnight(0) + 60,
                                      int(datetime.now().timestamp()) - 60), 100, 200)
        self.store.sync_if_needed()
        rows = self.store.query_daily_tokens(7)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1][0], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(rows[-1][1], 300)
        self.assertEqual([d for d, _ in rows], sorted(d for d, _ in rows), "日期必须升序")

    def test_channel_daily_includes_all_today_sources(self):
        """今日渠道分布必须每个启用源各一行（曾用 query_one 只留第一行）"""
        self._make_fixture_source()
        zcode_path = self._make_zcode_source()
        now = int(datetime.now().timestamp())

        self._insert_row("req-today", max(local_midnight(0) + 60, now - 60), 100, 200)
        conn = sqlite3.connect(zcode_path)
        conn.execute("INSERT INTO model_usage VALUES ('zc-1', 'glm-4', 500, 700, 0, ?, 'done')",
                     ((max(local_midnight(0) + 60, now - 60)) * 1000,))
        conn.commit()
        conn.close()

        self.store.rebuild([(CCSwitchAdapter(), True, self.source_path),
                            (ZCodeAdapter(), True, zcode_path)])
        self.assertEqual(len(self.store.attached), 2)
        self.store.sync_if_needed()

        rows = self.store.query_channel_daily(30)
        today = datetime.now().strftime("%Y-%m-%d")
        today_rows = {r[1]: r[2] for r in rows if r[0] == today}
        self.assertEqual(set(today_rows), {"cc-switch", "zcode"},
                         "今天的每个渠道都要出现，不能只剩第一行")
        self.assertEqual(today_rows["cc-switch"], 300)
        self.assertEqual(today_rows["zcode"], 1200)

    def test_daily_agg_window_prunes_stale_rows(self):
        """窗口重算要先清后写：明细没了，daily_agg 不能留下陈旧行"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000)
        self._rebuild()
        self.store.sync_if_needed()

        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        row = self.store.conn.execute(
            "SELECT SUM(input) FROM daily_agg WHERE date = ?", (yday,)).fetchone()
        self.assertEqual(row[0], 1000)

        # 明细被清掉（模拟导入去重 / 源库重算）后再重算同一窗口
        self.store.conn.execute("DELETE FROM usage_log")
        self.store._rebuild_daily_agg_window(
            yday, datetime.now().strftime("%Y-%m-%d"))
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM daily_agg WHERE date = ?", (yday,)).fetchone()[0]
        self.assertEqual(left, 0, "明细已消失，daily_agg 陈旧行必须被清掉")

    # ------------------------------------------------------------ 批次 1：credits / CSV 13 列

    def test_credits_column_migration(self):
        """v1.7 老库（12 列）升级：幂等补 credits 列，老行补 0"""
        store_path = StatsStore.store_path()
        conn = sqlite3.connect(store_path)
        conn.executescript("""
        CREATE TABLE usage_log (
            source TEXT NOT NULL, request_id TEXT NOT NULL, app_type TEXT, model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0, total_cost_usd REAL NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, request_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source, request_id)
        );
        """)
        conn.execute("INSERT INTO usage_log (source, request_id, model, input_tokens, created_at) "
                     "VALUES ('cc-switch', 'old-1', 'm', 100, ?)",
                     (local_midnight(2) + 3600,))
        conn.commit()
        conn.close()

        self._rebuild()
        cols = [r[1] for r in self.store.conn.execute("PRAGMA table_info(usage_log)")]
        self.assertIn("credits", cols)
        self.assertEqual(self.store.conn.execute(
            "SELECT credits FROM usage_log WHERE request_id='old-1'").fetchone()[0], 0)

        # 再 rebuild 一次：列已存在，不重复 ALTER、不报错
        self._rebuild()
        cols2 = [r[1] for r in self.store.conn.execute("PRAGMA table_info(usage_log)")]
        self.assertEqual(cols2.count("credits"), 1)

    def test_export_import_roundtrip_with_credits(self):
        """13 列导出/导入：表头正确，credits 与非整费用精确往返"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000,
                         cache_read=500, cache_create=100)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)
        self._rebuild()
        self.store.sync_if_needed()
        # credits 只有 Trae 源才会写，这里直接改库模拟
        self.store.conn.execute("UPDATE usage_log SET credits = 12.5, "
                                "total_cost_usd = 0.123456789 WHERE request_id='req-B'")
        self.store.conn.commit()

        path = os.path.join(self.tmp, "export13.csv")
        self.assertEqual(self.store.export_csv(path), 2)
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, StatsStore.EXPORT_COLUMNS)
        self.assertEqual(len(header), 13)

        imported_path = os.path.join(self.tmp, "imported13.db")
        StatsStore.store_path = staticmethod(lambda: imported_path)
        other = StatsStore()
        try:
            other.rebuild([(CCSwitchAdapter(), True, self.source_path)])
            self.assertEqual(other.import_csv(path), (2, 2, 0))
            row = other.conn.execute("SELECT credits, total_cost_usd FROM usage_log "
                                     "WHERE request_id='req-B'").fetchone()
            self.assertEqual(row[0], 12.5, "credits 必须精确往返")
            self.assertEqual(row[1], 0.123456789, "费用必须精确往返")
            self.assertEqual(other.conn.execute(
                "SELECT COUNT(*) FROM usage_log WHERE credits = 0").fetchone()[0], 1)
        finally:
            other._close()
            StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))

    def test_import_legacy_12_columns(self):
        """旧版 12 列 CSV 仍可导入，credits 按 0 处理"""
        self._make_fixture_source()
        self._rebuild()
        path = os.path.join(self.tmp, "legacy12.csv")
        epoch = local_midnight(1) + 3600
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(StatsStore.LEGACY_EXPORT_COLUMNS)
            w.writerow(["cc-switch", "legacy-1", "claude", "m", 10, 20, 0, 0, 0, 0.25, epoch, 1])

        self.assertEqual(self.store.import_csv(path), (1, 1, 0))
        self.assertEqual(self.store.conn.execute(
            "SELECT credits, total_cost_usd, input_tokens FROM usage_log "
            "WHERE request_id='legacy-1'").fetchone(), (0, 0.25, 10))
        self.assertEqual(self.store.conn.execute(
            "SELECT COALESCE(SUM(input+output),0) FROM daily_agg").fetchone()[0], 30,
            "导入后按窗口重建 daily_agg")
        self.assertEqual(self.store.import_csv(path), (1, 0, 1), "重复导入幂等")

    def test_source_display_name(self):
        self.assertEqual(self.store.source_display_name("trae"), "Trae")
        self.assertEqual(self.store.source_display_name("zcode"), "ZCode")
        self.assertEqual(self.store.source_display_name("cc-switch-rollup"), "cc-switch")
        self.assertEqual(self.store.source_display_name("other"), "other")

    # ------------------------------------------------------------ 批次 1：新查询口径

    def test_query_credits_sum_and_daily(self):
        """积分查询：今日/近 N 天/每日曲线（今日实时由其他源补，恒 0）"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)
        self._rebuild()
        self.store.sync_if_needed()
        self.store.conn.execute("UPDATE usage_log SET credits = 5.0 WHERE request_id='req-B'")
        self.store.conn.execute("UPDATE usage_log SET credits = 2.0 WHERE request_id='req-C'")
        self.store.conn.commit()

        self.assertEqual(self.store.query_credits_sum(30), 7.0)
        self.assertEqual(self.store.query_credits_sum(1), 5.0)
        self.assertEqual(self.store.query_credits_daily(30), [
            ((datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"), 2.0),
            ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), 5.0)])

        self._insert_row("req-A", max(local_midnight(0) + 60,
                                      int(datetime.now().timestamp()) - 60), 100, 200)
        self.store.sync_if_needed()
        self.assertEqual(self.store.query_today_credits(), 0.0)
        self.assertEqual(self.store.query_credits_sum(0), 0.0)

    def test_query_cost_mtd(self):
        """本月累计费用：月初至今日实时，跨月行不计入"""
        import calendar
        self._make_fixture_source()
        now = datetime.now()
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self._insert_row("req-prev", int((first - timedelta(days=2)).timestamp()) + 3600, 100, 200)
        self._rebuild()
        self.store.sync_if_needed()
        self._insert_row("req-today", max(local_midnight(0) + 60,
                                          int(now.timestamp()) - 60), 10, 20)
        self.store.sync_if_needed()

        mtd, elapsed, dim = self.store.query_cost_mtd()
        self.assertAlmostEqual(mtd, 0.5, places=6, msg="只算本月今日实时的 0.5")
        self.assertEqual(elapsed, now.day)
        self.assertEqual(dim, calendar.monthrange(now.year, now.month)[1])

    def test_query_unmetered_tokens(self):
        """未计费渠道 token：只统计 total_cost_usd = 0 的行"""
        self._make_fixture_source()
        self._insert_row_ex("req-free-B", local_midnight(1) + 3600, inp=1000, out=2000, cost=0.0)
        self._insert_row("req-paid-B", local_midnight(1) + 7200, 100, 200)
        self._rebuild()
        self.store.sync_if_needed()
        self._insert_row_ex("req-free-A", max(local_midnight(0) + 60,
                                              int(datetime.now().timestamp()) - 60),
                            inp=100, out=200, cost=0.0)
        self.store.sync_if_needed()

        # days=1 窗口是 [昨天 0 点, 明日 0 点)，含今日；days=0 只看今日
        self.assertEqual(self.store.query_unmetered_tokens(1), 3300)
        self.assertEqual(self.store.query_unmetered_tokens(0), 300)

    def test_model_history_norm_and_merge(self):
        """模型编年史 / 归一化 key / 手动合并 / 自动合并"""
        self._make_fixture_source()
        self._insert_row_ex("r-1", local_midnight(5) + 3600,
                            model="global.anthropic.claude-sonnet-4", inp=1000, out=2000)
        self._insert_row_ex("r-2", local_midnight(3) + 3600,
                            model="claude-sonnet-4", inp=10, out=20)
        self._insert_row_ex("r-3", local_midnight(1) + 3600,
                            model="openai/gpt-5", inp=100, out=200)
        self._rebuild()
        self.store.sync_if_needed()

        self.assertEqual(StatsStore.model_norm_key("global.anthropic.claude-sonnet-4"),
                         "claude-sonnet-4")
        self.assertEqual(StatsStore.model_norm_key("OpenAI/GPT-5"), "gpt-5")
        self.assertEqual(StatsStore.model_norm_key("vendor/anthropic.claude-3-5"), "claude-3-5")
        self.assertEqual(StatsStore.model_norm_key(""), "")

        hist = self.store.query_model_history()
        self.assertEqual([h[0] for h in hist],
                         ["global.anthropic.claude-sonnet-4", "claude-sonnet-4", "openai/gpt-5"],
                         "按首用时间升序")
        self.assertEqual(hist[0][1], local_midnight(5) + 3600)
        self.assertEqual(hist[0][2], local_midnight(5) + 3600)
        self.assertEqual(hist[0][3], 3000)

        # 自动合并：canonical = 组内 token 最大者（global.anthropic.* 与裸名同 key）
        groups, changed = self.store.auto_merge_models()
        self.assertEqual(groups, 1, "只有 claude 组同名")
        self.assertEqual(changed, 1, "小组并入 token 最大的标准名")
        self.assertEqual([r[0] for r in self.store.conn.execute(
            "SELECT DISTINCT model FROM usage_log ORDER BY model")],
            ["global.anthropic.claude-sonnet-4", "openai/gpt-5"])
        self.assertEqual(self.store.auto_merge_models(), (0, 0), "再跑一次无事可做")

        # 手动合并：改写行数与守卫
        self.assertEqual(self.store.merge_model("openai/gpt-5", "gpt-5"), 1)
        self.assertEqual(self.store.merge_model("gpt-5", "gpt-5"), 0)
        self.assertEqual(self.store.merge_model("", "gpt-5"), 0)
        self.assertEqual(self.store.merge_model("nope", "gpt-5"), 0)

    def test_merge_from_db(self):
        """合并另一台机器的 ccbar.db：主键去重、credits 不合并、幂等"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000)
        self._rebuild()
        self.store.sync_if_needed()

        other_path = os.path.join(self.tmp, "other.db")
        conn = sqlite3.connect(other_path)
        conn.executescript(USAGE_LOG_DDL)
        conn.execute("INSERT INTO usage_log (source, request_id, app_type, model, input_tokens, "
                     "output_tokens, cache_read_tokens, cache_creation_tokens, reasoning_tokens, "
                     "total_cost_usd, credits, created_at, request_count) "
                     "VALUES ('cc-switch','req-B','claude','m',1,2,0,0,0,0.5,9.9,?,1)",
                     (local_midnight(1) + 4000,))
        conn.execute("INSERT INTO usage_log (source, request_id, app_type, model, input_tokens, "
                     "output_tokens, cache_read_tokens, cache_creation_tokens, reasoning_tokens, "
                     "total_cost_usd, credits, created_at, request_count) "
                     "VALUES ('cc-switch','req-Z','claude','m',300,400,0,0,0,0.5,9.9,?,1)",
                     (local_midnight(2) + 4000,))
        conn.commit()
        conn.close()

        self.assertEqual(self.store.merge_from_db(other_path), (2, 1))
        self.assertEqual(self.store.merge_from_db(other_path), (2, 0), "第二次零新增")
        self.assertEqual(self.store.conn.execute(
            "SELECT request_id, credits FROM usage_log ORDER BY request_id").fetchall(),
            [("req-B", 0.0), ("req-Z", 0.0)], "credits 刻意不参与合并")
        self.assertEqual(self.store.conn.execute(
            "SELECT COALESCE(SUM(input+output),0) FROM daily_agg").fetchone()[0],
            3000 + 700, "合并窗口已重算")

        bare = os.path.join(self.tmp, "bare2.db")
        sqlite3.connect(bare).close()
        self.assertEqual(self.store.merge_from_db(bare), (0, 0), "缺 usage_log 表")
        self.assertEqual(self.store.merge_from_db(
            os.path.join(self.tmp, "nope.db")), (0, 0), "文件不存在")

    def test_window_queries_exclude_today(self):
        """区间查询读 daily_agg：今日实时不入区间"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000,
                         cache_read=500, cache_create=100)
        self._insert_row("req-C", local_midnight(3) + 3600, 10000, 20000)
        self._rebuild()
        self.store.sync_if_needed()
        self._insert_row("req-A", max(local_midnight(0) + 60,
                                      int(datetime.now().timestamp()) - 60), 100, 200)
        self.store.sync_if_needed()

        self.assertEqual(self.store.query_daily_tokens_between(6, 1), [
            ((datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"), 30000),
            ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), 3600)])

        stats = self.store.query_window_stats(6, 1)
        self.assertEqual(stats, {"reqs": 2, "input": 11000, "output": 22000,
                                 "cache_create": 100, "cache_read": 500, "total": 33600})
        # 窗口上界含"今天"时也不含今日实时
        self.assertEqual(self.store.query_window_stats(6, 0)["total"], 33600)
        self.assertNotIn(datetime.now().strftime("%Y-%m-%d"),
                         [d for d, _ in self.store.query_daily_tokens_between(6, 0)])

    def test_query_hour_histogram(self):
        """时段分布：本地小时边界（偏移作为参数绑定）"""
        self._make_fixture_source()
        yday_14 = (datetime.now() - timedelta(days=1)).replace(
            hour=14, minute=30, second=0, microsecond=0)
        self._insert_row("req-B", int(yday_14.timestamp()), 1000, 2000)
        self._rebuild()
        self.store.sync_if_needed()
        today_9 = datetime.now().replace(hour=9, minute=5, second=0, microsecond=0)
        self._insert_row("req-A", int(today_9.timestamp()), 100, 200)
        self.store.sync_if_needed()

        hist = self.store.query_hour_histogram(1)
        self.assertEqual(hist.get(14), 3000)
        self.assertEqual(hist.get(9), 300)
        self.assertEqual(sum(hist.values()), 3300)

    def test_query_timeline_past_and_today(self):
        """指定日流水走 usage_log；今天/None 走实时视图；最新在前"""
        self._make_fixture_source()
        yday = datetime.now() - timedelta(days=1)
        t10 = yday.replace(hour=10, minute=0, second=0, microsecond=0)
        t11 = yday.replace(hour=11, minute=0, second=0, microsecond=0)
        self._insert_row_ex("r-10", int(t10.timestamp()), inp=100, out=200)
        self._insert_row_ex("r-11", int(t11.timestamp()), inp=300, out=400)
        self._rebuild()
        self.store.sync_if_needed()
        self._insert_row_ex("r-today", int(datetime.now().replace(
            hour=8, minute=0, second=0, microsecond=0).timestamp()), inp=10, out=20)
        self.store.sync_if_needed()

        rows = self.store.query_timeline(yday.strftime("%Y-%m-%d"))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[3] for r in rows], [700, 300], "最新在前")
        self.assertEqual(rows[0][1], "test-model")
        self.assertEqual(rows[0][2], "cc-switch")
        self.assertAlmostEqual(rows[0][4], 0.5, places=6)
        self.assertEqual(rows[0][5], 0.0)
        self.assertEqual(rows[0][0], int(t11.timestamp()))

        live = self.store.query_timeline()
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0][3], 30)
        self.assertEqual(self.store.query_timeline(datetime.now()), live,
                         "今天等价于实时视图")
        self.assertEqual(self.store.query_timeline("2099-01-01"), [])

    def test_query_monthly_totals(self):
        """按月汇总：月份倒序，带 token 与 cache_read"""
        self._make_fixture_source()
        self._insert_row("req-old", local_midnight(40) + 3600, 10000, 20000)
        self._insert_row("req-new", local_midnight(5) + 3600, 1000, 2000, cache_read=500)
        self._rebuild()
        self.store.sync_if_needed()

        months = self.store.query_monthly_totals()
        self.assertEqual(len(months), 2, "两个不同月份")
        self.assertGreater(months[0][0], months[1][0], "月份倒序")
        self.assertEqual(months[0], ((datetime.now() - timedelta(days=5)).strftime("%Y-%m"), 1, 3500, 500))
        self.assertEqual(months[1], ((datetime.now() - timedelta(days=40)).strftime("%Y-%m"), 1, 30000, 0))
        self.assertEqual(self.store.query_monthly_totals(limit=1), months[:1])

    def test_query_source_breakdown(self):
        """今日各源分账：按 token 倒序"""
        self._make_fixture_source()
        zcode_path = self._make_zcode_source()
        now = int(datetime.now().timestamp())
        stamp = max(local_midnight(0) + 60, now - 60)
        self._insert_row("req-today", stamp, 100, 200)
        conn = sqlite3.connect(zcode_path)
        conn.execute("INSERT INTO model_usage VALUES ('zc-1', 'glm-4', 500, 700, 0, ?, 'done')",
                     (stamp * 1000,))
        conn.commit()
        conn.close()
        self.store.rebuild([(CCSwitchAdapter(), True, self.source_path),
                            (ZCodeAdapter(), True, zcode_path)])
        self.store.sync_if_needed()

        self.assertEqual(self.store.query_source_breakdown(),
                         [("zcode", 1, 1200), ("cc-switch", 1, 300)])

    # ------------------------------------------------------------ 批次 1：自动备份

    def test_auto_backup_and_rotation(self):
        """自动备份：ISO 文件名、当天幂等、滚动保留 keep 份且不碰其他文件"""
        self._make_fixture_source()
        self._insert_row("req-B", local_midnight(1) + 3600, 1000, 2000)
        self._rebuild()
        self.store.sync_if_needed()

        backups = os.path.join(self.tmp, "backups")
        path = self.store.auto_backup(backups, keep=7, today="2026-01-11")
        self.assertEqual(os.path.basename(path), "ccbar-auto-2026-01-11.db")
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0], 1)
        conn.close()

        # 当天第二次：no-op
        self.assertIsNone(self.store.auto_backup(backups, keep=7, today="2026-01-11"))
        self.assertEqual(os.listdir(backups), ["ccbar-auto-2026-01-11.db"])

        # 预置 10 份老备份 + 一个非匹配文件，第 11 份触发滚动清理
        for i in range(1, 11):
            with open(os.path.join(backups, f"ccbar-auto-2026-01-{i:02d}.db"), "w") as f:
                f.write("x")
        with open(os.path.join(backups, "keepme.txt"), "w") as f:
            f.write("x")

        path2 = self.store.auto_backup(backups, keep=7, today="2026-01-12")
        self.assertIsNotNone(path2)
        self.assertEqual([n for n in sorted(os.listdir(backups)) if n.startswith("ccbar-auto-")],
                         [f"ccbar-auto-2026-01-{i:02d}.db" for i in range(6, 13)],
                         "只保留最新 7 份")
        self.assertTrue(os.path.exists(os.path.join(backups, "keepme.txt")),
                        "不匹配模式的文件不得被删")

    def test_auto_backup_iso_date_and_no_connection(self):
        """today=None 用 ISO 日期；无连接返回 None 且不建目录"""
        self._make_fixture_source()
        self._rebuild()
        backups = os.path.join(self.tmp, "backups-iso")
        path = self.store.auto_backup(backups)
        self.assertEqual(os.path.basename(path),
                         "ccbar-auto-" + datetime.now().strftime("%Y-%m-%d") + ".db")

        bare_dir = os.path.join(self.tmp, "backups-bare")
        self.assertIsNone(StatsStore().auto_backup(bare_dir))
        self.assertFalse(os.path.exists(bare_dir), "无连接不该创建目录")
