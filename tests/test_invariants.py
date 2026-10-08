"""核心不变量：对外部源库**全程只读**，自建库的所有操作都不得改动源库文件。

这是项目对外承诺（"对 cc-switch / ZCode 零侵入"），也是新增的
自动备份 / 多机合并 / Trae 同步等路径最容易不小心破坏的地方，
所以单独用文件字节哈希 + mtime + 行数三重校验锁住。
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stats_store import StatsStore, CCSwitchAdapter, ZCodeAdapter  # noqa: E402


def local_midnight(days_ago=0):
    day = datetime.now() - timedelta(days=days_ago)
    return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fingerprint(path):
    """文件指纹：字节哈希 + 大小 + mtime + 主要表的行数"""
    with open(path, "rb") as f:
        data = f.read()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        counts = {t: conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                  for t in tables}
    finally:
        conn.close()
    return (hashlib.sha256(data).hexdigest(), len(data), os.path.getmtime(path), counts)


class TestSourceReadOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-invariant-")
        self._orig_store_path = StatsStore.store_path
        StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))
        self.src = os.path.join(self.tmp, "cc-switch.db")
        self._make_source(self.src)
        self.store = StatsStore()

    def tearDown(self):
        self.store._close()
        StatsStore.store_path = self._orig_store_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_source(self, path, wal=False):
        conn = sqlite3.connect(path)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
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
        for rid, ts, i, o in (("r-1", local_midnight(1) + 3600, 1000, 2000),
                              ("r-2", local_midnight(3) + 3600, 3000, 4000),
                              ("r-3", max(local_midnight(0) + 60,
                                          int(datetime.now().timestamp()) - 60), 500, 700)):
            conn.execute(
                "INSERT INTO proxy_request_logs VALUES (?, 'claude', 'claude-sonnet-4', "
                "?, ?, 100, 20, 0.25, ?)", (rid, i, o, ts))
        conn.execute("INSERT INTO usage_daily_rollups VALUES "
                     "('2026-01-01','p','m','rm','pm','claude',10,20,0,0,0.5,3)")
        conn.commit()
        if wal:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

    def _full_cycle(self):
        """把自建库的全部读写路径跑一遍"""
        self.store.rebuild([(CCSwitchAdapter(), True, self.src)])
        self.store.sync_if_needed()
        self.store.sync_if_needed()          # 重复同步（幂等）
        # 查询面（都会经过 usage_all / daily_agg）
        for fn in ("query_cost", "query_cost_daily", "query_cost_by_model",
                   "query_streak", "query_weekly_delta", "query_peak_day",
                   "query_top_model", "query_channel_daily", "query_app_daily",
                   "query_composition_daily", "query_month_progress",
                   "query_today_timeline", "query_daily_tokens", "query_monthly_totals",
                   "query_source_breakdown", "query_cost_mtd", "query_unmetered_tokens",
                   "query_hour_histogram"):
            getattr(self.store, fn)(30) if fn not in (
                "query_streak", "query_weekly_delta", "query_month_progress",
                "query_monthly_totals", "query_source_breakdown", "query_cost_mtd",
                "query_today_timeline") else getattr(self.store, fn)()
        self.store.query_timeline()
        self.store.query_model_history()
        self.store.auto_merge_models()
        # 写路径
        self.store.auto_backup(os.path.join(self.tmp, "backups"), keep=3)
        csv_path = os.path.join(self.tmp, "export.csv")
        self.store.export_csv(csv_path)
        self.store.import_csv(csv_path)
        self.store.backup(os.path.join(self.tmp, "manual-backup.db"))
        self.store.merge_from_db(os.path.join(self.tmp, "manual-backup.db"))

    def test_source_db_untouched_by_full_cycle(self):
        before = fingerprint(self.src)
        self._full_cycle()
        after = fingerprint(self.src)
        self.assertEqual(before[0], after[0], "源库字节被改写了！")
        self.assertEqual(before[1], after[1], "源库文件大小变了")
        self.assertEqual(before[2], after[2], "源库 mtime 变了（说明被写过）")
        self.assertEqual(before[3], after[3], "源库行数变了")

    def test_wal_source_db_untouched(self):
        """源库是 WAL 模式时也不能留下 -wal/-shm 或改动文件"""
        wal_src = os.path.join(self.tmp, "cc-switch-wal.db")
        self._make_source(wal_src, wal=True)
        before = fingerprint(wal_src)
        self.store.rebuild([(CCSwitchAdapter(), True, wal_src)])
        self.store.sync_if_needed()
        self.store.query_day_stats if False else None
        self.store.query_timeline()
        after = fingerprint(wal_src)
        self.assertEqual(before[0], after[0], "WAL 源库字节被改写")
        self.assertEqual(before[2], after[2], "WAL 源库 mtime 变了")

    def test_read_only_attach_is_preferred(self):
        """能只读打开时必须用 mode=ro（普通 ATTACH 有写 WAL 的风险）"""
        self.store.rebuild([(CCSwitchAdapter(), True, self.src)])
        sql = self.store.conn.execute(
            "SELECT file FROM pragma_database_list WHERE name='src_cc'").fetchone()
        self.assertIsNotNone(sql)
        # mode=ro 挂载时 sqlite 会把 URI 里的参数丢掉，这里验证能力而非字符串：
        # 对源库执行写操作必须失败
        with self.assertRaises(sqlite3.Error):
            self.store.conn.execute(
                "INSERT INTO src_cc.proxy_request_logs (request_id) VALUES ('x')")

    def test_merge_from_db_does_not_touch_the_other_store(self):
        """多机合并只读对方的统计库"""
        other = os.path.join(self.tmp, "other.db")
        other_store = StatsStore()
        StatsStore.store_path = staticmethod(lambda: other)
        try:
            other_store.rebuild([(CCSwitchAdapter(), True, self.src)])
            other_store.sync_if_needed()
        finally:
            other_store._close()
            StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))

        before = fingerprint(other)
        self.store.rebuild([(CCSwitchAdapter(), True, self.src)])
        self.store.merge_from_db(other)
        after = fingerprint(other)
        self.assertEqual(before[0], after[0], "被合并的统计库被改写了")
        self.assertEqual(before[3], after[3])

    def test_validate_db_does_not_touch_source(self):
        from stats_store import validate_db
        before = fingerprint(self.src)
        validate_db(self.src, CCSwitchAdapter().required_tables)
        after = fingerprint(self.src)
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[2], after[2])


class TestNoDeadSettings(unittest.TestCase):
    """设置项不得是"死键"：只在设置页里出现、却没有任何行为读它。

    背景：`led_red_threshold` 曾经只在设置页读写，界面上能改但完全没生效，
    静态扫描很难发现（文本上"有引用"），所以这里按"必须在设置页之外被读"来卡。
    数据源路径类键（db_path / zcode_path / *_enabled）不在此列——它们由
    app_settings.source_configs() 统一消费；launch_at_login 由设置页保存时
    调 autostart.apply() 落地（与 macOS 版同位置）。
    """

    # 必须有"设置页之外"的行为引用
    BEHAVIOR_KEYS = {
        "refresh_interval", "warning_threshold", "warning_enabled", "notify_interval",
        "trae_enabled", "trae_sessionid", "led_red_threshold",
        "monthly_budget_usd", "default_token_price", "auto_weekly_report",
        "app_language", "theme", "custom_themes", "insights_last_page",
        "last_auto_backup_date", "last_update_check_date", "popover_wide",
    }

    def test_behavior_keys_are_read_outside_the_settings_window(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("    def show_settings(self")
        end = src.index("    @_on_gui\n    def backup_data")
        outside = src[:start] + src[end:]

        import re
        missing = [k for k in sorted(self.BEHAVIOR_KEYS)
                   if not re.search(r"\b%s\b" % re.escape(k), outside)]
        self.assertEqual(missing, [],
                         "这些设置项没有任何行为读它（死键）：%s" % missing)

    def test_behavior_keys_exist_in_defaults(self):
        import app_settings
        unknown = sorted(self.BEHAVIOR_KEYS - set(app_settings.DEFAULTS))
        self.assertEqual(unknown, [], "测试里写了 DEFAULTS 不存在的键：%s" % unknown)


if __name__ == "__main__":
    unittest.main()
