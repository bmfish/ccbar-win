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
        # 明细：只同步"昨天及更早"，今日走实时视图
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
        WHERE date(created_at, 'unixepoch', 'localtime') < '{today}'
          AND date(created_at, 'unixepoch', 'localtime') >= '{from_day}'
        """
        # 历史聚合（早于本地已有明细最早一天的）展开成"每天每模型一行"的伪明细，
        # created_at 取当天正午，保证 date() 落在原日期
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
            CAST(strftime('%s', date || ' 12:00:00') AS INTEGER),
            request_count
        FROM {alias}.usage_daily_rollups
        WHERE date < (SELECT date(MIN(created_at), 'unixepoch', 'localtime')
                      FROM usage_log WHERE source = 'cc-switch')
        """
        return [detail, rollups]

    def today_fragment(self, alias):
        return f"""
        SELECT 'cc-switch' AS source, app_type, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, 0 AS reasoning_tokens,
               CAST(total_cost_usd AS REAL) AS total_cost_usd, created_at, 1 AS request_count
        FROM {alias}.proxy_request_logs
        WHERE date(created_at, 'unixepoch', 'localtime') = date('now', 'localtime')
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
          AND date(started_at / 1000, 'unixepoch', 'localtime') < '{today}'
          AND date(started_at / 1000, 'unixepoch', 'localtime') >= '{from_day}'
        """
        return [detail]

    def today_fragment(self, alias):
        return f"""
        SELECT 'zcode' AS source, 'zcode' AS app_type, model_id AS model,
               input_tokens, output_tokens,
               0 AS cache_read_tokens, 0 AS cache_creation_tokens,
               reasoning_tokens, 0.0 AS total_cost_usd,
               started_at / 1000 AS created_at, 1 AS request_count
        FROM {alias}.model_usage
        WHERE status != 'running'
          AND date(started_at / 1000, 'unixepoch', 'localtime') = date('now', 'localtime')
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
                self.conn.execute(USAGE_LOG_DDL)
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at)")
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
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
        try:
            self.conn.execute(f"ATTACH DATABASE '{_file_uri(path)}?mode=ro' AS {adapter.alias}")
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
        """usage_all = 自家历史 + 各源今日实时。设置变化（启停/换路径）后重建。"""
        self.conn.execute("DROP VIEW IF EXISTS temp.usage_all")
        parts = ["""
        SELECT source, app_type, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, reasoning_tokens,
               total_cost_usd, created_at, request_count
        FROM usage_log
        """]
        for adapter in self.attached:
            parts.append(adapter.today_fragment(adapter.alias))
        self.conn.execute("CREATE TEMP VIEW usage_all AS " + " UNION ALL ".join(parts))
        self.conn.commit()

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
                    self.conn.commit()
                except sqlite3.Error as e:
                    print(f"{adapter.name} 同步失败: {e}")
                    return

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
