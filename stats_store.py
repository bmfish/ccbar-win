"""多数据源统计库（与 macOS 版 StatsStore 设计一致）

ccbar 自建统计库（~/.ccbar/ccbar.db）为主连接：
  - usage_log   统一明细表（昨天及更早的历史，每日懒惰补账同步进来）
  - meta        各源同步水位
  - usage_all   临时视图 = 自家历史 + 各启用源"今日"实时数据
外部源库一律以只读方式 ATTACH，绝不写入。

新增数据源：实现 SourceAdapter 三个方法并注册到 SOURCE_REGISTRY 即可，
同步、视图、设置界面自动生效。
"""
import os
import sqlite3
# Windows 控制台默认 cp1252，中文 print 会抛 UnicodeEncodeError（CI 实锤），统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import threading
from datetime import datetime, timedelta
from pathlib import Path

DAILY_AGG_DDL = """
CREATE TABLE IF NOT EXISTS daily_agg (
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    reqs INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_create INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, source)
)
"""

USAGE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS usage_log (
    source TEXT NOT NULL,
    request_id TEXT NOT NULL,
    app_type TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, request_id)
)
"""


def _file_uri(path):
    """路径转 file: URI（ATTACH 用），兼容 Windows 盘符与特殊字符"""
    p = path.replace("\\", "/")
    p = p.replace("?", "%3F").replace("#", "%23")
    return "file:///" + p.lstrip("/")


def _local_epoch(day_str):
    """日期字符串（yyyy-MM-dd）→ 本地零点 epoch。

    1970 之前的日期 Windows 的本地时间函数不支持（timestamp() 抛 OSError），
    此时返回 0——等价于全量回填的起点，1970 前不会有任何数据。
    """
    d = datetime.strptime(day_str, "%Y-%m-%d")
    try:
        return int(d.timestamp())
    except (OSError, OverflowError, ValueError):
        return 0


class SourceAdapter:
    """数据源适配器基类"""

    id = ""
    name = ""
    default_path = ""
    alias = ""
    required_tables = []

    def attach_sql(self, file_url):
        return f"ATTACH DATABASE 'file:{file_url}' AS {self.alias}"

    def sync_sqls(self, alias, from_day, today):
        """历史补账 SQL（INSERT OR IGNORE 幂等，按本地日期字符串过滤）"""
        raise NotImplementedError

    def today_fragment(self, alias):
        """usage_all 视图中该源"今日"数据的 UNION ALL 段"""
        raise NotImplementedError


class CCSwitchAdapter(SourceAdapter):
    id = "ccswitch"
    name = "cc-switch"
    default_path = os.path.expanduser("~/.cc-switch/cc-switch.db")
    alias = "src_cc"
    required_tables = ["proxy_request_logs"]

    def attach_sql(self, file_url):
        return f"ATTACH DATABASE 'file:{file_url}?mode=ro' AS {self.alias}"

    def sync_sqls(self, alias, from_day, today):
        # 明细：只同步"昨天及更早"，今日走实时视图。
        # 边界由 Python 侧算好的本地零点 epoch 传入（区间条件走索引，避免逐行 date()）
        from_epoch = _local_epoch(from_day)
        today_epoch = _local_epoch(today)
        detail = f"""
        INSERT OR IGNORE INTO usage_log
            (source, request_id, app_type, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
             created_at, request_count)
        SELECT
            'cc-switch', request_id, app_type, model, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, 0,
            CAST(total_cost_usd AS REAL), created_at, 1
        FROM {alias}.proxy_request_logs
        WHERE created_at >= {from_epoch} AND created_at < {today_epoch}
        """
        # 历史聚合（早于本地已有明细最早一天的）展开成"每天每模型一行"的伪明细，
        # created_at 取"该日期的本地正午"：先按 UTC 零点取 epoch，再补偿本地时区偏移 +12h，
        # 任意时区 date() 都落在原日期（旧写法 UTC 正午在 UTC±13/14 会错一天）
        rollups = f"""
        INSERT OR IGNORE INTO usage_log
            (source, request_id, app_type, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
             created_at, request_count)
        SELECT
            'cc-switch-rollup',
            'rollup|' || date || '|' || provider_id || '|' || model || '|' || request_model || '|' || pricing_model,
            app_type, model, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, 0,
            CAST(total_cost_usd AS REAL),
            CAST(strftime('%s', date || ' 00:00:00') - (strftime('%s', 'now', 'localtime') - strftime('%s', 'now')) + 43200 AS INTEGER),
            request_count
        FROM {alias}.usage_daily_rollups
        WHERE date < (SELECT date(MIN(created_at), 'unixepoch', 'localtime')
                      FROM usage_log WHERE source = 'cc-switch')
        """
        return [detail, rollups]

    def today_fragment(self, alias, today_start):
        # 边界由 StatsStore 用本地零点 epoch 烘入，跨零点由 sync_if_needed 重建视图。
        # 不用 strftime('now','localtime') 之类 SQL 时区运算——那会偏移一个时区。
        return f"""
        SELECT 'cc-switch' AS source, app_type, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, 0 AS reasoning_tokens,
               CAST(total_cost_usd AS REAL) AS total_cost_usd, created_at, 1 AS request_count
        FROM {alias}.proxy_request_logs
        WHERE created_at >= {today_start}
        """


class ZCodeAdapter(SourceAdapter):
    id = "zcode"
    name = "ZCode"
    default_path = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    alias = "src_zc"
    required_tables = ["model_usage"]

    def attach_sql(self, file_url):
        return f"ATTACH DATABASE 'file:{file_url}?mode=ro' AS {self.alias}"

    def sync_sqls(self, alias, from_day, today):
        # started_at 是毫秒，折成秒。
        # 口径：按 ZCode 官方统计（computed_total_tokens = input+output），
        # 缓存命中部分不计入用量，缓存列记 0（原始值仍在 ZCode 自己的库里）。
        from_epoch = _local_epoch(from_day)
        today_epoch = _local_epoch(today)
        detail = f"""
        INSERT OR IGNORE INTO usage_log
            (source, request_id, app_type, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
             created_at, request_count)
        SELECT
            'zcode', id, 'zcode', model_id, input_tokens, output_tokens,
            0, 0, reasoning_tokens, 0,
            started_at / 1000, 1
        FROM {alias}.model_usage
        WHERE status != 'running'
          AND started_at >= {from_epoch} * 1000 AND started_at < {today_epoch} * 1000
        """
        return [detail]

    def today_fragment(self, alias, today_start):
        return f"""
        SELECT 'zcode' AS source, 'zcode' AS app_type, model_id AS model,
               input_tokens, output_tokens,
               0 AS cache_read_tokens, 0 AS cache_creation_tokens,
               reasoning_tokens, 0.0 AS total_cost_usd,
               started_at / 1000 AS created_at, 1 AS request_count
        FROM {alias}.model_usage
        WHERE status != 'running'
          AND started_at >= {today_start} * 1000
        """


# 适配器注册表（新数据源在这里加一行）
SOURCE_REGISTRY = [CCSwitchAdapter(), ZCodeAdapter()]


def adapter_for(source_id):
    for a in SOURCE_REGISTRY:
        if a.id == source_id:
            return a
    return None


class StatsStore:
    """自建统计库：历史补账 + 今日实时视图，线程安全"""

    def __init__(self):
        self.conn = None
        self.attached = []      # 已 ATTACH 且表齐全的适配器
        self._view_day = ""     # usage_all 视图构建日期（跨零点重建用）
        self._lock = threading.Lock()

    @staticmethod
    def store_path():
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        return str(config_dir / "ccbar.db")

    # ------------------------------------------------------------ 连接管理

    def rebuild(self, configs):
        """（重）建连接并 ATTACH 各启用源。

        configs: [(adapter, enabled, path), ...]
        """
        with self._lock:
            self._close()
            try:
                # uri=True 使 ATTACH 支持 file:...?mode=ro 只读挂载
                self.conn = sqlite3.connect(self.store_path(), uri=True,
                                            check_same_thread=False)
            except sqlite3.Error as e:
                print(f"无法打开统计库: {e}")
                self.conn = None
                return

            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                # 源库正被宿主应用写入时，只读 ATTACH 查询可能撞 SQLITE_BUSY，等 2 秒而不是直接失败
                self.conn.execute("PRAGMA busy_timeout=2000")
                self.conn.execute(USAGE_LOG_DDL)
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at)")
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                # 每日聚合缓存：区间/总量查询直接读它，不再扫明细。
                # meta 标记控制升级后一次性全量回填。
                self.conn.execute(DAILY_AGG_DDL)
                flag = self.conn.execute(
                    "SELECT value FROM meta WHERE key='daily_agg_full'").fetchone()
                if not flag:
                    self.conn.execute("""
                        INSERT OR REPLACE INTO daily_agg
                            (date, source, reqs, input, output, cache_create, cache_read)
                        SELECT date(created_at, 'unixepoch', 'localtime'), source,
                               SUM(request_count), SUM(input_tokens), SUM(output_tokens),
                               SUM(cache_creation_tokens), SUM(cache_read_tokens)
                        FROM usage_log GROUP BY 1, 2
                    """)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES ('daily_agg_full', '1')")
                self.conn.commit()
            except sqlite3.Error as e:
                print(f"初始化统计库失败: {e}")
                return

            self.attached = []
            for adapter, enabled, path in configs:
                if not enabled:
                    continue
                if self._attach(adapter, path):
                    self.attached.append(adapter)

            self._rebuild_view()

    def _attach(self, adapter, path):
        if not os.path.isfile(path):
            print(f"数据源 {adapter.name} 文件不存在，跳过: {path}")
            return False
        # 优先 mode=ro；源库是 WAL 模式且 -wal 文件不在（源应用已关闭）时
        # 只读打开会失败，此时回退普通 ATTACH——本应用保证绝不写源库。
        uri = _file_uri(path)
        try:
            self.conn.execute(f"ATTACH DATABASE '{uri}?mode=ro' AS {adapter.alias}")
        except sqlite3.Error:
            try:
                self.conn.execute(f"ATTACH DATABASE '{uri}' AS {adapter.alias}")
            except sqlite3.Error as e:
                print(f"数据源 {adapter.name} ATTACH 失败，跳过: {e}")
                return False

        placeholders = ",".join("?" * len(adapter.required_tables))
        found = self.conn.execute(
            f"SELECT COUNT(*) FROM {adapter.alias}.sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            adapter.required_tables).fetchone()[0]
        if found < len(adapter.required_tables):
            print(f"数据源 {adapter.name} 缺少必需表，跳过")
            self.conn.execute(f"DETACH DATABASE {adapter.alias}")
            return False
        return True

    def _rebuild_view(self):
        """usage_all = 自家历史 + 各源今日实时。设置变化（启停/换路径）后重建。

        "今日"分支的边界用本地零点 epoch 烘入；跨零点后由 sync_if_needed 检测并重建
        （SQL 侧 strftime 的 localtime 组合会偏移一个时区，禁止使用）。
        """
        self._view_day = datetime.now().strftime("%Y-%m-%d")
        today_start = int(datetime.now().replace(hour=0, minute=0, second=0,
                                                 microsecond=0).timestamp())
        self.conn.execute("DROP VIEW IF EXISTS temp.usage_all")
        parts = ["""
        SELECT source, app_type, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, reasoning_tokens,
               total_cost_usd, created_at, request_count
        FROM usage_log
        """]
        for adapter in self.attached:
            parts.append(adapter.today_fragment(adapter.alias, today_start))
        self.conn.execute("CREATE TEMP VIEW usage_all AS " + " UNION ALL ".join(parts))
        self.conn.commit()

    def _refresh_view_for_new_day(self):
        """跨零点后重建视图（今日分支边界烘在视图里，不能过夜）。"""
        if getattr(self, "_view_day", "") != datetime.now().strftime("%Y-%m-%d"):
            self._rebuild_view()

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        self.conn = None
        self.attached = []

    # ------------------------------------------------------------ 懒惰补账

    def sync_if_needed(self):
        """meta 记录每源已同步到的最后一个完整日，落后于昨天就补账。

        窗口回看一天防跨零点迟到行；首次水位为空即全量回填。
        INSERT OR IGNORE 幂等，不会重复计数。
        """
        with self._lock:
            if self.conn is None or not self.attached:
                return

            self._refresh_view_for_new_day()

            today = datetime.now().strftime("%Y-%m-%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            for adapter in self.attached:
                key = f"synced_day_{adapter.id}"
                row = self.conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                water = row[0] if row else ""
                if water >= yesterday:
                    continue  # 已是最新

                from_day = "1970-01-01"
                if water:
                    try:
                        from_day = (datetime.strptime(water, "%Y-%m-%d")
                                    - timedelta(days=1)).strftime("%Y-%m-%d")
                    except ValueError:
                        pass

                try:
                    for sql in adapter.sync_sqls(adapter.alias, from_day, today):
                        self.conn.execute(sql)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (key, yesterday))
                    self._rebuild_daily_agg_window(from_day, today)
                    self.conn.commit()
                except sqlite3.Error as e:
                    print(f"{adapter.name} 同步失败: {e}")
                    return

    def _rebuild_daily_agg_window(self, from_day, today):
        """重算 [from_day, today) 窗口的每日聚合（数据源：usage_log 明细，OR REPLACE 自愈）。"""
        self.conn.execute(f"""
            INSERT OR REPLACE INTO daily_agg
                (date, source, reqs, input, output, cache_create, cache_read)
            SELECT date(created_at, 'unixepoch', 'localtime'), source,
                   SUM(request_count), SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_creation_tokens), SUM(cache_read_tokens)
            FROM usage_log
            WHERE date(created_at, 'unixepoch', 'localtime') >= '{from_day}'
              AND date(created_at, 'unixepoch', 'localtime') < '{today}'
            GROUP BY 1, 2
        """)

    # ------------------------------------------------------------ 查询

    def query_all(self, sql, params=()):
        """对 usage_all 执行查询，返回全部行；无可用数据源时返回 None"""
        with self._lock:
            if self.conn is None or not self.attached:
                return None
            try:
                return self.conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                print(f"查询失败: {e}")
                return None

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def local_midnight(days_ago=0):
        """本地时区 N 天前（0=今天）0 点的 epoch 秒"""
        day = datetime.now() - timedelta(days=days_ago)
        return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    # ------------------------------------------------------------ 洞察中心查询
    # 与 macOS 版同款口径：费用类走 usage_all（epoch 区间走索引），
    # 历史 token 类优先 daily_agg，今日实时统一补查。

    def query_cost(self, days):
        """近 N 天（含今天）总费用（USD）"""
        row = self.query_one("""
            SELECT COALESCE(SUM(total_cost_usd), 0) FROM usage_all
            WHERE created_at >= ? AND created_at < ?""",
            (self.local_midnight(days), self.local_midnight(-1)))
        return row[0] if row else 0.0

    def query_cost_daily(self, days):
        """近 N 天每日费用 [(date, cost)]，日期升序（可能含空洞）"""
        rows = self.query_all("""
            SELECT date(created_at, 'unixepoch', 'localtime'), COALESCE(SUM(total_cost_usd), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY 1 ORDER BY 1""",
            (self.local_midnight(days), self.local_midnight(-1)))
        return [(r[0], r[1]) for r in rows] if rows else []

    def query_cost_by_model(self, days, limit=8):
        """近 N 天按模型费用排行 [(model, cost, token)]"""
        rows = self.query_all("""
            SELECT model, COALESCE(SUM(total_cost_usd), 0),
                   COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY model ORDER BY 2 DESC LIMIT ?""",
            (self.local_midnight(days), self.local_midnight(-1), limit))
        return [(r[0], r[1], r[2]) for r in rows] if rows else []

    def query_streak(self):
        """连续使用天数（今天没用就从昨天起算）"""
        rows = self.query_all("""
            SELECT DISTINCT date FROM daily_agg
            WHERE input + output + cache_create + cache_read > 0 ORDER BY date DESC LIMIT 400""")
        if not rows:
            return 0
        dates = [r[0] for r in rows]
        day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if dates[0] != day.strftime("%Y-%m-%d"):
            day -= timedelta(days=1)
        streak = 0
        for d in dates:
            if d == day.strftime("%Y-%m-%d"):
                streak += 1
                day -= timedelta(days=1)
            elif d > day.strftime("%Y-%m-%d"):
                continue  # 游离的未来日期，跳过不打断
            else:
                break
        return streak

    def query_weekly_delta(self):
        """周环比：(本周含今日, 上一个 7 天)"""
        def ds(n):
            return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

        row = self.query_one("""SELECT
            COALESCE(SUM(CASE WHEN date >= ? AND date <= ? THEN input+output+cache_create+cache_read END), 0),
            COALESCE(SUM(CASE WHEN date >= ? AND date <= ? THEN input+output+cache_create+cache_read END), 0)
            FROM daily_agg""", (ds(6), ds(1), ds(13), ds(7)))
        this_week = row[0] if row else 0
        today = self.query_one("""SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ?""", (self.local_midnight(0),))
        return (this_week + (today[0] if today else 0), row[1] if row else 0)

    def query_peak_day(self, days):
        """近 N 天单日峰值（今日实时也参与竞争）→ (date, token) | None"""
        def ds(n):
            return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

        rows = self.query_all("""
            SELECT date, COALESCE(SUM(input+output+cache_create+cache_read), 0) AS t
            FROM daily_agg WHERE date >= ? GROUP BY date ORDER BY t DESC LIMIT 1""", (ds(days),))
        peak = (rows[0][0], rows[0][1]) if rows else None
        today = self.query_one("""SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ?""", (self.local_midnight(0),))
        if today and today[0] > (peak[1] if peak else 0):
            peak = (ds(0), today[0])
        return peak

    def query_top_model(self, days):
        """近 N 天使用量最大的模型 → (model, token) | None"""
        rows = self.query_all("""
            SELECT model, COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0) AS t
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY model ORDER BY t DESC LIMIT 1""",
            (self.local_midnight(days), self.local_midnight(-1)))
        return (rows[0][0], rows[0][1]) if rows else None

    def query_channel_daily(self, days):
        """近 N 天渠道每日 token（daily_agg 自带 source 维度），今日实时按源补一行"""
        rows = self.query_all("""
            SELECT date, source, COALESCE(SUM(input+output+cache_create+cache_read), 0)
            FROM daily_agg WHERE date >= ? GROUP BY date, source ORDER BY date""",
            ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),))
        result = [(r[0], r[1], r[2]) for r in rows] if rows else []
        today = self.query_one("""
            SELECT source, COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ? GROUP BY source""",
            (self.local_midnight(0), self.local_midnight(-1)))
        if today and today[1] > 0:
            result.append((datetime.now().strftime("%Y-%m-%d"), today[0], today[1]))
        return result

    def query_app_daily(self, days):
        """近 N 天应用（app_type）每日 token，今日实时细分补一行"""
        rows = self.query_all("""
            SELECT date(created_at, 'unixepoch', 'localtime'),
                   COALESCE(NULLIF(app_type, ''), 'unknown'),
                   COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_log WHERE created_at >= ? AND created_at < ?
            GROUP BY 1, 2""",
            (self.local_midnight(days), self.local_midnight(-1)))
        result = [(r[0], r[1], r[2]) for r in rows] if rows else []
        today = self.query_all("""
            SELECT COALESCE(NULLIF(app_type, ''), 'unknown') AS app,
                   COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0) AS t
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY app HAVING t > 0""",
            (self.local_midnight(0), self.local_midnight(-1)))
        if today:
            t = datetime.now().strftime("%Y-%m-%d")
            result += [(t, r[0], r[1]) for r in today]
        return result

    def query_composition_daily(self, days):
        """近 N 天每日 token 构成（输入/输出/缓存读/缓存创建），含今日，日期升序"""
        rows = self.query_all("""
            SELECT date, COALESCE(SUM(input),0), COALESCE(SUM(output),0),
                   COALESCE(SUM(cache_read),0), COALESCE(SUM(cache_create),0)
            FROM daily_agg WHERE date >= ? GROUP BY date ORDER BY date""",
            ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),))
        result = [(r[0], r[1], r[2], r[3], r[4]) for r in rows] if rows else []
        today = self.query_one("""
            SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens),0)
            FROM usage_all WHERE created_at >= ?""", (self.local_midnight(0),))
        if today and sum(today) > 0:
            result.append((datetime.now().strftime("%Y-%m-%d"), today[0], today[1], today[2], today[3]))
        return result

    def query_month_progress(self):
        """本月进度：(月初至昨日+今日实时, 已过天数, 当月总天数)"""
        now = datetime.now()
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last_day = next_month - timedelta(days=1)
        row = self.query_one("""
            SELECT COALESCE(SUM(input+output+cache_create+cache_read), 0)
            FROM daily_agg WHERE date >= ? AND date <= ?""",
            (first.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")))
        today = self.query_one("""SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ?""", (self.local_midnight(0),))
        mtd = (row[0] if row else 0) + (today[0] if today else 0)
        import calendar
        return (mtd, max(now.day, 1), calendar.monthrange(now.year, now.month)[1])

    def query_today_timeline(self, limit=500):
        """今日请求流水（最新在前）：(时间, 模型, 渠道, token, 费用)"""
        rows = self.query_all("""
            SELECT created_at, model, source,
                   COALESCE(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens, 0),
                   COALESCE(total_cost_usd, 0)
            FROM usage_all WHERE created_at >= ?
            ORDER BY created_at DESC LIMIT ?""", (self.local_midnight(0), limit))
        return [(r[0], r[1] or "-", r[2], r[3], r[4]) for r in rows] if rows else []

    # ------------------------------------------------------------ 导出 / 导入（幂等）

    EXPORT_COLUMNS = ["source", "request_id", "app_type", "model", "input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens",
                      "total_cost_usd", "created_at", "request_count"]

    def export_csv(self, path):
        """导出 usage_log 全量明细为 CSV（带 BOM，Excel 可开）。成功返回行数，库未开返回 -1"""
        import csv as _csv
        with self._lock:
            if self.conn is None:
                return -1
            rows = self.conn.execute("""
                SELECT source, request_id, app_type, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, reasoning_tokens,
                       total_cost_usd, created_at, request_count
                FROM usage_log ORDER BY created_at""").fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(self.EXPORT_COLUMNS)
            w.writerows(rows)
        return len(rows)

    def import_csv(self, path):
        """幂等导入明细 CSV：主键 (source, request_id) 去重，重复/非法行跳过，
        导入后按受影响窗口重建 daily_agg。
        - Returns: (读取行数, 新增行数, 跳过行数)；表头不符返回 (0, 0, -1)"""
        import csv as _csv
        with self._lock:
            if self.conn is None:
                return (0, 0, 0)
            try:
                with open(path, newline="", encoding="utf-8-sig") as f:
                    reader = _csv.reader(f)
                    if next(reader, None) != self.EXPORT_COLUMNS:
                        return (0, 0, -1)
                    read = inserted = skipped = 0
                    min_day = max_day = None
                    base = self.conn.total_changes
                    for row in reader:
                        if len(row) != 12:
                            skipped += 1
                            continue
                        try:
                            src, rid, app, model = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
                            inp, out, cr, cc, rs = int(row[4]), int(row[5]), int(row[6]), int(row[7]), int(row[8])
                            cost, epoch, rc = float(row[9]), int(row[10]), int(row[11])
                            if not src or not rid:
                                raise ValueError
                        except ValueError:
                            skipped += 1
                            continue
                        read += 1
                        self.conn.execute("""
                            INSERT OR IGNORE INTO usage_log
                                (source, request_id, app_type, model, input_tokens, output_tokens,
                                 cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
                                 created_at, request_count)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (src, rid, app, model, inp, out, cr, cc, rs, cost, epoch, rc))
                        day = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
                        min_day = day if min_day is None else min(min_day, day)
                        max_day = day if max_day is None else max(max_day, day)
                    inserted = self.conn.total_changes - base
                    skipped += read - inserted
                    self.conn.commit()
            except sqlite3.Error as e:
                print(f"导入失败: {e}")
                return (0, 0, 0)

        if min_day is not None:
            # 窗口 [from_day, today)：from 回看一天防迟到行，to 多包一天覆盖导入尾日
            margin = (datetime.strptime(min_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            end = (datetime.strptime(max_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            with self._lock:
                self._rebuild_daily_agg_window(margin, end)
        return (read, inserted, skipped)

    def backup(self, path):
        """VACUUM INTO 一键备份统计库（用量历史是长期资产）。

        与 macOS 版语义一致：目标必须是未存在的空路径，成功返回 True。
        """
        with self._lock:
            if self.conn is None:
                return False
            try:
                self.conn.execute("VACUUM INTO ?", (path,))
                return True
            except sqlite3.Error as e:
                print(f"备份失败: {e}")
                return False

    def source_display_name(self, source):
        """source 标识 → 界面显示名（历史聚合行归入 cc-switch）"""
        if source == "zcode":
            return "ZCode"
        if source in ("cc-switch", "cc-switch-rollup"):
            return "cc-switch"
        return source


def validate_db(path, required_tables):
    """只读试开一次源库并检查必需表。

    返回 None 表示通过；否则返回人话错误（与 macOS 版设置页同款文案）。
    """
    if not os.path.isfile(path):
        return "文件不存在"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return "打不开（被占用或损坏）"
    try:
        placeholders = ",".join("?" * len(required_tables))
        found = conn.execute(
            f"SELECT COUNT(*) FROM sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            required_tables).fetchone()[0]
        if found < len(required_tables):
            return "缺少必需表"
        return None
    except sqlite3.Error:
        return "校验失败"
    finally:
        conn.close()
