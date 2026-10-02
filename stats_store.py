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
        from_epoch = int(datetime.strptime(from_day, "%Y-%m-%d").timestamp())
        today_epoch = int(datetime.strptime(today, "%Y-%m-%d").timestamp())
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
        from_epoch = int(datetime.strptime(from_day, "%Y-%m-%d").timestamp())
        today_epoch = int(datetime.strptime(today, "%Y-%m-%d").timestamp())
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

    def source_display_name(self, source):
        """source 标识 → 界面显示名（历史聚合行归入 cc-switch）"""
        if source == "zcode":
            return "ZCode"
        if source in ("cc-switch", "cc-switch-rollup"):
            return "cc-switch"
        return source
