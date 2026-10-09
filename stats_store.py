"""多数据源统计库（与 macOS 版 StatsStore 设计一致）

ccbar 自建统计库（~/.ccbar/ccbar.db）为主连接：
  - usage_log   统一明细表（昨天及更早的历史，每日懒惰补账同步进来）
  - meta        各源同步水位
  - usage_all   临时视图 = 自家历史 + 各启用源"今日"实时数据
外部源库一律以只读方式 ATTACH，绝不写入。

新增数据源：实现 SourceAdapter 三个方法并注册到 SOURCE_REGISTRY 即可，
同步、视图、设置界面自动生效。
"""
import fnmatch
import os
import sqlite3
import sys
# Windows 控制台默认 cp1252，中文 print 会抛 UnicodeEncodeError（CI 实锤），统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import threading
from datetime import datetime, timedelta
from pathlib import Path

import trae_sync
from trae_sync import TraeAuth, TraeCheckinResult, TraeResult

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
    credits REAL NOT NULL DEFAULT 0,
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

    def today_fragment(self, alias, today_start):
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
               CAST(total_cost_usd AS REAL) AS total_cost_usd, 0.0 AS credits,
               created_at, 1 AS request_count
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
               reasoning_tokens, 0.0 AS total_cost_usd, 0.0 AS credits,
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
        # Trae 是 HTTP 源（不走 ATTACH）：rebuild 时把启用状态与凭据快照进来
        self.trae_enabled = False
        self.trae_sessionid = ""
        self._last_trae_sync_at = 0.0   # 上次 Trae 同步时刻（in-memory，重启无妨）
        self._last_checkin_attempt_at = 0.0   # 上次 Trae 签到尝试时刻（同上）
        # 每源连接诊断（设置页展示）：已连接 / 未启用 / 具体失败原因
        self.source_status = {}
        # 是否有任一可用数据源（SQLite ATTACH 或 Trae HTTP）。
        # 查询侧早退判据：Trae-only 安装也必须能出数（否则整个界面空白）
        self.has_active_source = False

    @staticmethod
    def store_path():
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        return str(config_dir / "ccbar.db")

    # ------------------------------------------------------------ 连接管理

    def rebuild(self, configs, trae_enabled=False, trae_sessionid=""):
        """（重）建连接并 ATTACH 各启用源。

        configs: [(adapter, enabled, path), ...]
        trae_enabled / trae_sessionid: Trae 是 HTTP 源不走 ATTACH，只登记启用状态与
        passport 凭据。Trae-only 安装（无任何 SQLite 源）同样要能查数。
        """
        with self._lock:
            self._close()
            self.trae_enabled = bool(trae_enabled)
            self.trae_sessionid = (trae_sessionid or "").strip()
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
                # v1.8 新增 credits 列（Trae 积分口径，其他源为 0）。老库 ALTER 迁移，列已存在则跳过。
                cols = [r[1] for r in self.conn.execute(
                    "PRAGMA table_info(usage_log)").fetchall()]
                if "credits" not in cols:
                    self.conn.execute(
                        "ALTER TABLE usage_log ADD COLUMN credits REAL NOT NULL DEFAULT 0")
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                # 每日聚合缓存：区间/总量查询直接读它，不再扫明细。
                # meta 标记控制升级后一次性全量回填。
                self.conn.execute(DAILY_AGG_DDL)
                flag = self.conn.execute(
                    "SELECT value FROM meta WHERE key='daily_agg_full'").fetchone()
                if not flag:
                    # daily_agg 只结算完整日（≤昨天）；今天由查询侧实时补
                    self.conn.execute("""
                        INSERT OR REPLACE INTO daily_agg
                            (date, source, reqs, input, output, cache_create, cache_read)
                        SELECT date(created_at, 'unixepoch', 'localtime'), source,
                               SUM(request_count), SUM(input_tokens), SUM(output_tokens),
                               SUM(cache_creation_tokens), SUM(cache_read_tokens)
                        FROM usage_log
                        WHERE date(created_at, 'unixepoch', 'localtime') < date('now', 'localtime')
                        GROUP BY 1, 2
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
                    self.source_status[adapter.id] = "未启用"
                    continue
                error = self._attach(adapter, path)
                if error is None:
                    self.attached.append(adapter)
                    self.source_status[adapter.id] = "已连接"
                else:
                    self.source_status[adapter.id] = error
            # 未出现在 configs 里的注册表源一律视为未启用（设置页要逐源显示状态）
            for adapter in SOURCE_REGISTRY:
                self.source_status.setdefault(adapter.id, "未启用")
            # Trae（HTTP 源）：启用但没填 sessionid 时不能算"可用"，也不能算失败
            if not self.trae_enabled:
                self.source_status["trae"] = "未启用"
            elif not self.trae_sessionid:
                self.source_status["trae"] = "未配置登录"
            else:
                self.source_status["trae"] = "连接中…"

            self.has_active_source = bool(self.attached) or self.trae_enabled

            self._rebuild_view()

    def _attach(self, adapter, path):
        """ATTACH 一个源库并校验必需表：成功返回 None，失败返回人话错误（设置页展示）"""
        if not os.path.isfile(path):
            print(f"数据源 {adapter.name} 文件不存在，跳过: {path}")
            return "文件不存在"
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
                return "ATTACH 失败（库可能被占用或损坏）"

        placeholders = ",".join("?" * len(adapter.required_tables))
        found = self.conn.execute(
            f"SELECT COUNT(*) FROM {adapter.alias}.sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            adapter.required_tables).fetchone()[0]
        if found < len(adapter.required_tables):
            print(f"数据源 {adapter.name} 缺少必需表，跳过")
            self.conn.execute(f"DETACH DATABASE {adapter.alias}")
            return "缺少必需表"
        return None

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
               total_cost_usd, credits, created_at, request_count
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
        self.source_status = {}
        self.has_active_source = False
        self.trae_enabled = False
        self.trae_sessionid = ""

    # ------------------------------------------------------------ 懒惰补账

    def sync_if_needed(self):
        """meta 记录每源已同步到的最后一个完整日，落后于昨天就补账。

        窗口回看一天防跨零点迟到行；首次水位为空即全量回填。
        INSERT OR IGNORE 幂等，不会重复计数。
        """
        with self._lock:
            if self.conn is None or not self.has_active_source:
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
        """重算 [from_day, today) 窗口的每日聚合（数据源：usage_log 明细）。

        先 DELETE 再 INSERT：明细行消失（导入去重、源库重算）时 daily_agg 的自愈
        依赖窗口整段重算，只靠 OR REPLACE 会把已无明细的 (date, source) 陈旧行留下。
        这里就地提交（自成一体的维护步骤）：留着未提交的写事务会让后续
        VACUUM INTO（手动/自动备份）报 "cannot VACUUM from within a transaction"。
        """
        self.conn.execute(
            "DELETE FROM daily_agg WHERE date >= ? AND date < ?", (from_day, today))
        self.conn.execute("""
            INSERT OR REPLACE INTO daily_agg
                (date, source, reqs, input, output, cache_create, cache_read)
            SELECT date(created_at, 'unixepoch', 'localtime'), source,
                   SUM(request_count), SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_creation_tokens), SUM(cache_read_tokens)
            FROM usage_log
            WHERE date(created_at, 'unixepoch', 'localtime') >= ?
              AND date(created_at, 'unixepoch', 'localtime') < ?
            GROUP BY 1, 2
        """, (from_day, today))
        self.conn.commit()

    def _commit_pending(self):
        """提交连接上残留的写事务（VACUUM INTO 不允许在事务内执行）"""
        if self.conn is None:
            return
        try:
            if self.conn.in_transaction:
                self.conn.commit()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ Trae（HTTP 源）同步
    # 口径与 macOS 版 StatsStore.syncTraeIfNeeded / upsertTraeRows / traeEntSummary 一致：
    # 网络在锁外跑（最长 ~15s×3 次），入库是短临界区；失败/过期也占掉节流窗口。

    @staticmethod
    def _now_epoch(now):
        """now（None=此刻 / datetime / epoch 数字）→ epoch 秒"""
        if now is None:
            return datetime.now().timestamp()
        if isinstance(now, datetime):
            return now.timestamp()
        return float(now)

    def _meta_get(self, key):
        """读 meta 值（调用方持锁；缺失/无连接返回 ""）"""
        if self.conn is None:
            return ""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else ""

    def _meta_set(self, key, value):
        """写 meta（调用方持锁；commit 由调用方统一收口）"""
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)))

    def _meta_float(self, key, default=0.0):
        try:
            return float(self._meta_get(key))
        except (TypeError, ValueError):
            return default

    def sync_trae_if_needed(self, now=None, interactive=False):
        """按需同步 Trae 用量（HTTP 源）。返回 TraeResult 或 None（被守卫拦下）。

        interactive=True 是用户点开弹窗的主动刷新，间隔更短（60s vs 900s）。
        夜间静默（0–9 点本地时间）直接返回且**不占**节流窗口：凌晨会话由 9 点后
        第一次同步的"水位 - 2 天"窗口覆盖，不丢数。
        """
        now_epoch = self._now_epoch(now)
        now_dt = datetime.fromtimestamp(now_epoch)

        # ---- 守卫 1~5（锁内取快照；网络绝不在锁内跑）----
        with self._lock:
            if self.conn is None:
                return None
            if not self.trae_enabled:
                self.source_status["trae"] = "未启用"
                return None
            if trae_sync.is_night_silent(now_dt):
                return None   # 0–9 点静默，不消耗节流
            if not self.trae_sessionid:
                self.source_status["trae"] = "未配置登录"
                return None   # 没有凭据就不发 HTTP
            interval = (trae_sync.INTERACTIVE_MIN_INTERVAL if interactive
                        else trae_sync.BG_MIN_INTERVAL)
            if now_epoch - self._last_trae_sync_at < interval:
                return None   # 节流窗口内

            # ---- 拉取窗口：水位 -2 天回看（跨零点迟到会话 + 会话跨天推进），首次 90 天全量
            water = self._meta_get("trae_synced_day")
            from_epoch = self.local_midnight(trae_sync.FIRST_FETCH_DAYS, now=now_dt)
            if water:
                try:
                    back = (datetime.strptime(water, "%Y-%m-%d")
                            - timedelta(days=trae_sync.PAGE_LOOKBACK_DAYS))
                    from_epoch = self.local_midnight(0, now=back)
                except ValueError:
                    pass   # 水位坏了当首次全量
            to_epoch = int(now_epoch)
            # 积分账单（只喂设置页）按小时节流，不必跟着每次同步拉
            fetch_ent = now_epoch - self._meta_float("trae_ent_at") >= trae_sync.ENT_MIN_INTERVAL
            auth = TraeAuth.from_json(self._meta_get("trae_auth")) or TraeAuth()

        # ---- 网络（锁外）----
        try:
            result = trae_sync.run(self.trae_sessionid, auth=auth,
                                   from_epoch=from_epoch, to_epoch=to_epoch,
                                   fetch_ent=fetch_ent, now=now_epoch)
        except Exception as e:   # 注入的 transport 等异常也不许把界面打挂
            result = TraeResult("failed", message=str(e))

        # ---- 入库 + 状态（锁内）。节流时间戳在所有出口都推进：
        # 失败/过期也占掉窗口，防弹窗反复开关把接口打爆（下次后台周期自然重试）
        with self._lock:
            self._last_trae_sync_at = now_epoch
            if self.conn is None:
                return result
            # 换签结果（run 就地更新了 auth）必须持久化，重启不丢
            self._meta_set("trae_auth", auth.to_json())
            if result.status == "ok":
                self.upsert_trae_rows(result.rows)
                # 今日行由 usage_all 实时可见，daily_agg 只结算完整日（< 今天）
                today = now_dt.strftime("%Y-%m-%d")
                self._rebuild_daily_agg_window(
                    datetime.fromtimestamp(from_epoch).strftime("%Y-%m-%d"), today)
                self._meta_set("trae_synced_day", today)
                if result.consumed is not None and result.total is not None:
                    self._meta_set("trae_ent", "%s|%s" % (result.consumed, result.total))
                    self._meta_set("trae_ent_at", int(now_epoch))
                self.source_status["trae"] = "已连接"
            elif result.status == "auth_expired":
                self.source_status["trae"] = "登录已过期，请重新提供 sessionid"
            elif result.status == "not_configured":
                self.source_status["trae"] = "未配置登录"
            else:
                print(f"Trae 同步失败: {result.message}")
                self.source_status["trae"] = "同步失败：" + (result.message or "")
            self.conn.commit()
        return result

    def trae_checkin_if_needed(self, now=None, interactive=False):
        """Trae 每日自动签到。返回 TraeCheckinResult 或 None（被守卫拦下）。

        水位 trae_checkin_day = 当天日期：成功/已签即写，当天不再发请求；
        失败/未开启按 CHECKIN_RETRY_INTERVAL（1 小时）重试。
        interactive=True 是用户点开弹窗的主动查看：夜间静默只限定时器，不拦主动操作
        （凌晨点弹窗同样能签，与 macOS 版一致）。
        """
        now_epoch = self._now_epoch(now)
        now_dt = datetime.fromtimestamp(now_epoch)
        today = now_dt.strftime("%Y-%m-%d")

        # ---- 守卫（锁内取快照；网络绝不在锁内跑）----
        with self._lock:
            if self.conn is None:
                return None
            if not self.trae_enabled:
                return None
            if not interactive and trae_sync.is_night_silent(now_dt):
                return None   # 0–9 点静默，不消耗重试窗口
            if not self.trae_sessionid:
                return None   # 没有凭据就不发 HTTP
            if self._meta_get("trae_checkin_day") == today:
                return None   # 今天已到账
            if now_epoch - self._last_checkin_attempt_at < trae_sync.CHECKIN_RETRY_INTERVAL:
                return None   # 失败重试窗口内
            # 占住尝试窗口，防定时器与弹窗同时触发打双发
            self._last_checkin_attempt_at = now_epoch
            auth = TraeAuth.from_json(self._meta_get("trae_auth")) or TraeAuth()

        # ---- 网络（锁外）----
        try:
            result = trae_sync.checkin(self.trae_sessionid, auth=auth, now=now_epoch)
        except Exception as e:   # 注入的 transport 等异常也不许把界面打挂
            result = TraeCheckinResult("failed", message=str(e))

        # ---- 状态（锁内）----
        with self._lock:
            if self.conn is None:
                return result
            # 换签结果（checkin 就地更新了 auth）必须持久化，重启不丢
            self._meta_set("trae_auth", auth.to_json())
            if result.status in ("claimed", "already"):
                self._meta_set("trae_checkin_day", today)
            elif result.status == "not_configured":
                self.source_status["trae"] = "未配置登录"
            elif result.status == "auth_expired":
                self.source_status["trae"] = "登录已过期，请重新提供 sessionid"
            # disabled / failed 不占水位，按小时重试；状态文案留给用量同步维护
            self.conn.commit()
        return result

    def upsert_trae_rows(self, rows):
        """Trae 明细入库：会话级 UPSERT（同一会话用量随对话推进增长，覆盖旧值）。

        与其他源的 INSERT OR IGNORE 不同——Trae 行是"会话聚合快照"而非不可变流水；
        request_count 是会话数口径，更新时不动。cache_write → cache_creation_tokens。
        调用方持锁（内部不再加锁，threading.Lock 非重入）；一个事务提交。
        """
        if self.conn is None or not rows:
            return 0
        sql = """
        INSERT INTO usage_log
            (source, request_id, app_type, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, reasoning_tokens,
             total_cost_usd, credits, created_at, request_count)
        VALUES ('trae', ?, 'trae', ?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
        ON CONFLICT(source, request_id) DO UPDATE SET
            model=excluded.model,
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            cache_read_tokens=excluded.cache_read_tokens,
            cache_creation_tokens=excluded.cache_creation_tokens,
            total_cost_usd=excluded.total_cost_usd,
            credits=excluded.credits,
            created_at=excluded.created_at
        """
        written = 0
        try:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                pass   # Python 驱动可能已隐式开启事务，直接复用
            for row in rows:
                self.conn.execute(sql, (row.request_id, row.model, row.input, row.output,
                                        row.cache_read, row.cache_write, row.cost_usd,
                                        row.credits, row.epoch))
                written += 1
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"Trae 行入库失败: {e}")
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            return 0
        return written

    def trae_ent_summary(self):
        """Trae 官方积分账单 meta "consumed|total" → (consumed, total)；缺失/坏数据 None"""
        with self._lock:
            raw = self._meta_get("trae_ent")
        parts = (raw or "").split("|")
        if len(parts) != 2:
            return None
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None

    # ------------------------------------------------------------ 查询

    def query_all(self, sql, params=()):
        """对 usage_all 执行查询，返回全部行；无可用数据源时返回 None

        Trae 是 HTTP 源，没有 ATTACH：判据用 has_active_source 而非 attached，
        否则 Trae-only 安装会被判成"无数据源"、整个界面空白。
        """
        with self._lock:
            if self.conn is None or not self.has_active_source:
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
    def local_midnight(days_ago=0, now=None):
        """本地时区 N 天前（0=今天）0 点的 epoch 秒

        now 可注入（None=此刻 / datetime / epoch 秒）：Trae 拉取窗口要按注入时刻算，
        测试才能脱网断言窗口边界。
        """
        if now is None:
            day = datetime.now()
        elif isinstance(now, datetime):
            day = now
        else:
            day = datetime.fromtimestamp(float(now))
        day = day - timedelta(days=days_ago)
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
        # 今日实时按源逐个补行：这里必须用 query_all（GROUP BY source 会有多行），
        # 用 query_one 只会留下第一行渠道。
        today = self.query_all("""
            SELECT source, COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0) AS t
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY source HAVING t > 0""",
            (self.local_midnight(0), self.local_midnight(-1)))
        if today:
            d = datetime.now().strftime("%Y-%m-%d")
            result += [(d, r[0], r[1]) for r in today]
        return result

    def query_daily_tokens(self, days):
        """近 N 天每日总 token（跨渠道，含今天实时），日期升序 [(date, token)]"""
        rows = self.query_all("""
            SELECT date, COALESCE(SUM(input+output+cache_create+cache_read), 0)
            FROM daily_agg WHERE date >= ? GROUP BY date ORDER BY date""",
            ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),))
        result = [(r[0], r[1]) for r in rows] if rows else []
        today = self.query_one("""SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ?""", (self.local_midnight(0),))
        if today and today[0] > 0:
            result.append((datetime.now().strftime("%Y-%m-%d"), today[0]))
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

    # ------------------------------------------------------------ 费用 / 积分 / 模型治理

    def query_cost_mtd(self):
        """本月累计费用（月初 0 点 ~ 明日 0 点）→ (费用, 已过天数, 当月总天数)"""
        import calendar
        now = datetime.now()
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.query_one("""
            SELECT COALESCE(SUM(total_cost_usd), 0) FROM usage_all
            WHERE created_at >= ? AND created_at < ?""",
            (int(first.timestamp()), self.local_midnight(-1)))
        return (row[0] if row else 0.0,
                max(now.day, 1), calendar.monthrange(now.year, now.month)[1])

    def query_unmetered_tokens(self, days):
        """近 N 天（含今天）未计费渠道（total_cost_usd 为 0/空）消耗的 token。

        供"默认单价估算"折算——cc-switch 只记部分渠道成本，ZCode 等渠道费用为 0。
        """
        row = self.query_one("""
            SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
            FROM usage_all
            WHERE created_at >= ? AND created_at < ? AND COALESCE(total_cost_usd, 0) = 0""",
            (self.local_midnight(days), self.local_midnight(-1)))
        return int(row[0]) if row else 0

    def query_today_credits(self):
        """今日积分消耗（Trae 行有值，其他源恒为 0）"""
        row = self.query_one(
            "SELECT COALESCE(SUM(credits), 0) FROM usage_all WHERE created_at >= ?",
            (self.local_midnight(0),))
        return float(row[0]) if row else 0.0

    def query_credits_sum(self, days):
        """近 N 天（含今天）积分消耗，0 天即今日"""
        row = self.query_one(
            "SELECT COALESCE(SUM(credits), 0) FROM usage_all WHERE created_at >= ?",
            (self.local_midnight(days),))
        return float(row[0]) if row else 0.0

    def query_credits_daily(self, days):
        """近 N 天每日积分曲线 [(date, credits)]，日期升序（可能含空洞）"""
        rows = self.query_all("""
            SELECT date(created_at, 'unixepoch', 'localtime'), COALESCE(SUM(credits), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY 1 ORDER BY 1""",
            (self.local_midnight(days), self.local_midnight(-1)))
        return [(r[0], float(r[1])) for r in rows] if rows else []

    def query_model_history(self):
        """模型编年史：[(model, 首用 epoch, 末用 epoch, 总 token)]，按首用时间升序"""
        with self._lock:
            if self.conn is None:
                return []
            try:
                rows = self.conn.execute("""
                    SELECT model, MIN(created_at), MAX(created_at),
                           COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) AS t
                    FROM usage_log
                    GROUP BY model ORDER BY MIN(created_at)""").fetchall()
            except sqlite3.Error as e:
                print(f"查询失败: {e}")
                return []
        return [(r[0] or "", r[1], r[2], r[3]) for r in rows]

    @staticmethod
    def model_norm_key(model):
        """归一化 key：小写 + 去掉 "vendor/" 路径前缀 + 去掉 anthropic./openai. 区域前缀。

        保守策略：只合这些明显同款；带后缀变体（-ps-gcp-dst、[1m]）视为不同模型不动。
        """
        m = (model or "").lower()
        if "/" in m:
            m = m.rsplit("/", 1)[-1]
        for vendor in ("anthropic.", "openai."):
            idx = m.find(vendor)
            if idx >= 0:
                m = m[idx + len(vendor):]
                break
        return m

    def merge_model(self, from_model, to_model):
        """手动合并：把 from 的所有明细行并入 to（usage_log 直接 UPDATE，不可撤销）"""
        with self._lock:
            changed = self._merge_model_unlocked(from_model, to_model)
            if changed:
                self.conn.commit()
            return changed

    def _merge_model_unlocked(self, from_model, to_model):
        """已持锁版本的行合并（auto_merge_models 内部用）"""
        if (self.conn is None or not from_model or not to_model
                or from_model == to_model):
            return 0
        try:
            cur = self.conn.execute(
                "UPDATE usage_log SET model = ? WHERE model = ?", (to_model, from_model))
        except sqlite3.Error as e:
            print(f"合并模型失败: {e}")
            return 0
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def auto_merge_models(self):
        """自动合并同名模型：按归一化 key 分组，组内以用量最大者为标准名。

        - Returns: (合并的组数, 改写的行数)
        """
        with self._lock:
            if self.conn is None:
                return (0, 0)
            try:
                rows = self.conn.execute("""
                    SELECT model, COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
                    FROM usage_log GROUP BY model""").fetchall()
            except sqlite3.Error as e:
                print(f"查询失败: {e}")
                return (0, 0)

            groups = {}
            for name, token in rows:
                groups.setdefault(self.model_norm_key(name), []).append((name or "", token))
            merged = changed = 0
            for members in groups.values():
                if len(members) < 2:
                    continue
                canonical = max(members, key=lambda m: m[1])[0]
                for name, _ in members:
                    if name != canonical:
                        changed += self._merge_model_unlocked(name, canonical)
                merged += 1
            if changed:
                self.conn.commit()
            return (merged, changed)

    def merge_from_db(self, path):
        """从另一台机器的 ccbar.db 合并明细（主键去重，重复行自动跳过）。

        12 列拷贝，credits 刻意不合并（与 macOS 版一致）。
        - Returns: (对方总行数, 实际新增行数)；对方缺 usage_log 表返回 (0, 0)
        """
        with self._lock:
            if self.conn is None:
                return (0, 0)
            # 先只读试开校验结构，避免 ATTACH 坏库
            try:
                probe = sqlite3.connect(_file_uri(path) + "?mode=ro", uri=True, timeout=1)
            except sqlite3.Error:
                return (0, 0)
            try:
                row = probe.execute("SELECT COUNT(*) FROM sqlite_master "
                                    "WHERE type='table' AND name='usage_log'").fetchone()
                found = row[0] if row else 0
            except sqlite3.Error:
                found = 0
            finally:
                try:
                    probe.close()
                except sqlite3.Error:
                    pass
            if not found:
                return (0, 0)

            try:
                self.conn.execute("ATTACH DATABASE ? AS merge_src", (path,))
            except sqlite3.Error as e:
                print(f"合并源库打开失败: {e}")
                return (0, 0)
            read = inserted = 0
            try:
                row = self.conn.execute("SELECT COUNT(*) FROM merge_src.usage_log").fetchone()
                read = row[0] if row else 0
                base = self.conn.total_changes
                self.conn.execute("""
                    INSERT OR IGNORE INTO usage_log
                        (source, request_id, app_type, model, input_tokens, output_tokens,
                         cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
                         created_at, request_count)
                    SELECT source, request_id, app_type, model, input_tokens, output_tokens,
                           cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd,
                           created_at, request_count
                    FROM merge_src.usage_log
                """)
                inserted = self.conn.total_changes - base
                # 重算受影响区间的聚合缓存（多包一天覆盖跨时区迟到行）
                span = self.conn.execute(
                    "SELECT MIN(created_at), MAX(created_at) FROM merge_src.usage_log").fetchone()
                if span and span[0] is not None:
                    self._rebuild_daily_agg_window(
                        (datetime.fromtimestamp(span[0]) - timedelta(days=1)).strftime("%Y-%m-%d"),
                        (datetime.fromtimestamp(span[1]) + timedelta(days=1)).strftime("%Y-%m-%d"))
                self.conn.commit()
            except (sqlite3.Error, OSError, OverflowError, ValueError) as e:
                print(f"合并失败: {e}")
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                return (read, 0)
            finally:
                try:
                    self.conn.execute("DETACH DATABASE merge_src")
                except sqlite3.Error:
                    pass
            return (read, inserted)

    # ------------------------------------------------------------ 区间 / 时段 / 流水 / 月度

    def query_daily_tokens_between(self, days_ago_from, days_ago_to):
        """任意历史窗口（days_ago_from ~ days_ago_to，均含）的日 token 序列，日期升序。

        数据源是 daily_agg，不含今日实时。
        """
        def ds(n):
            return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

        rows = self.query_all("""
            SELECT date, COALESCE(SUM(input + output + cache_create + cache_read), 0)
            FROM daily_agg WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date""",
            (ds(days_ago_from), ds(days_ago_to)))
        return [(r[0], r[1]) for r in rows] if rows else []

    def query_window_stats(self, days_ago_from, days_ago_to):
        """任意历史窗口（均含）的完整统计，不含今日实时 → dict"""
        def ds(n):
            return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

        row = self.query_one("""
            SELECT COALESCE(SUM(reqs), 0), COALESCE(SUM(input), 0), COALESCE(SUM(output), 0),
                   COALESCE(SUM(cache_create), 0), COALESCE(SUM(cache_read), 0)
            FROM daily_agg WHERE date >= ? AND date <= ?""",
            (ds(days_ago_from), ds(days_ago_to)))
        stats = {"reqs": 0, "input": 0, "output": 0, "cache_create": 0,
                 "cache_read": 0, "total": 0}
        if row:
            stats.update(reqs=row[0], input=row[1], output=row[2],
                         cache_create=row[3], cache_read=row[4])
            stats["total"] = row[1] + row[2] + row[3] + row[4]
        return stats

    def query_hour_histogram(self, days):
        """近 N 天时段分布 {小时: token}（本地时区偏移烘进参数，避免逐行 localtime）"""
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        rows = self.query_all("""
            SELECT ((created_at + ?) % 86400) / 3600,
                   COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
            FROM usage_all
            WHERE created_at >= ? AND created_at < ?
            GROUP BY 1""",
            (offset, self.local_midnight(days), self.local_midnight(-1)))
        return {int(r[0]): r[1] for r in rows} if rows else {}

    def query_timeline(self, day=None, limit=800):
        """指定日期的逐笔流水（最新在前）：(时间, 模型, 渠道, token, 费用, 积分)。

        day 为 None 或今天走实时视图；历史日期走已同步的 usage_log（本地 0 点起 24 小时）。
        """
        if day is None:
            day_str = datetime.now().strftime("%Y-%m-%d")
        elif hasattr(day, "strftime"):
            day_str = day.strftime("%Y-%m-%d")
        else:
            day_str = str(day)
        limit = max(1, limit)

        cols = """
            SELECT created_at, model, source,
                   COALESCE(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens, 0),
                   COALESCE(total_cost_usd, 0), COALESCE(credits, 0)
        """
        if day_str == datetime.now().strftime("%Y-%m-%d"):
            rows = self.query_all(cols + """
            FROM usage_all WHERE created_at >= ?
            ORDER BY created_at DESC LIMIT ?""", (self.local_midnight(0), limit))
        else:
            try:
                start = _local_epoch(day_str)
            except ValueError:
                return []
            rows = self.query_all(cols + """
            FROM usage_log WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at DESC LIMIT ?""", (start, start + 86400, limit))
        return [(r[0], r[1] or "-", r[2], r[3], r[4], r[5]) for r in rows] if rows else []

    def query_monthly_totals(self, limit=36):
        """按月汇总 [(month, reqs, token, cache_read)]，月份倒序，最多 limit 个月"""
        rows = self.query_all("""
            SELECT substr(date, 1, 7), COALESCE(SUM(reqs), 0),
                   COALESCE(SUM(input + output + cache_create + cache_read), 0),
                   COALESCE(SUM(cache_read), 0)
            FROM daily_agg GROUP BY 1 ORDER BY 1 DESC LIMIT ?""", (max(1, limit),))
        return [(r[0], r[1], r[2], r[3]) for r in rows] if rows else []

    def query_source_breakdown(self):
        """今日各数据源分账 [(source, reqs, token)]，按 token 倒序"""
        rows = self.query_all("""
            SELECT source, COALESCE(SUM(request_count), 0),
                   COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ?
            GROUP BY source ORDER BY 3 DESC""",
            (self.local_midnight(0), self.local_midnight(-1)))
        return [(r[0], r[1], r[2]) for r in rows] if rows else []

    # ------------------------------------------------------------ 导出 / 导入（幂等）

    EXPORT_COLUMNS = ["source", "request_id", "app_type", "model", "input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens",
                      "total_cost_usd", "credits", "created_at", "request_count"]
    # v1.7 及更早的导出布局（12 列，无 credits），导入时按 0 补齐
    LEGACY_EXPORT_COLUMNS = ["source", "request_id", "app_type", "model", "input_tokens",
                             "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                             "reasoning_tokens", "total_cost_usd", "created_at", "request_count"]

    def export_csv(self, path):
        """导出 usage_log 全量明细为 CSV（带 BOM，Excel 可开）。成功返回行数，库未开返回 -1"""
        import csv as _csv
        with self._lock:
            if self.conn is None:
                return -1
            rows = self.conn.execute("""
                SELECT source, request_id, app_type, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, reasoning_tokens,
                       total_cost_usd, credits, created_at, request_count
                FROM usage_log ORDER BY created_at""").fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(self.EXPORT_COLUMNS)
            w.writerows(rows)
        return len(rows)

    def import_csv(self, path):
        """幂等导入明细 CSV：主键 (source, request_id) 去重，重复/非法行跳过，
        导入后按受影响窗口重建 daily_agg。

        兼容 v1.8 的 13 列（含 credits）与旧版 12 列布局（credits 记 0）。
        - Returns: (读取行数, 新增行数, 跳过行数)；表头不符返回 (0, 0, -1)"""
        import csv as _csv
        with self._lock:
            if self.conn is None:
                return (0, 0, 0)
            try:
                with open(path, newline="", encoding="utf-8-sig") as f:
                    reader = _csv.reader(f)
                    header = next(reader, None)
                    if header not in (self.EXPORT_COLUMNS, self.LEGACY_EXPORT_COLUMNS):
                        return (0, 0, -1)
                    read = inserted = skipped = 0
                    min_day = max_day = None
                    base = self.conn.total_changes
                    for row in reader:
                        n = len(row)
                        if n not in (12, 13):
                            skipped += 1
                            continue
                        try:
                            src, rid, app, model = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
                            inp, out, cr, cc, rs = int(row[4]), int(row[5]), int(row[6]), int(row[7]), int(row[8])
                            cost = float(row[9])
                            credits = float(row[10]) if n == 13 else 0.0
                            epoch, rc = int(row[n - 2]), int(row[n - 1])
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
                                 credits, created_at, request_count)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (src, rid, app, model, inp, out, cr, cc, rs, cost, credits, epoch, rc))
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
        备份前防御性提交残留写事务：用户按"备份"不该因维护工作留下的
        未提交事务而失败（VACUUM 不允许在事务内执行）。
        """
        with self._lock:
            if self.conn is None:
                return False
            try:
                self._commit_pending()
                self.conn.execute("VACUUM INTO ?", (path,))
                return True
            except sqlite3.Error as e:
                print(f"备份失败: {e}")
                return False

    def auto_backup(self, directory, keep=7, today=None):
        """每日自动备份到 directory（VACUUM INTO，滚动保留最近 keep 份）。

        文件名一律用 ISO 日期 ccbar-auto-<YYYY-MM-DD>.db——绝不用本地化日期格式
        （macOS 版曾因本地化日期里的 "/" 变成多级路径，导致 VACUUM INTO 永久失败）。
        当天已备份过直接跳过（返回 None），成功返回备份路径。
        """
        if today is None:
            stamp = datetime.now().strftime("%Y-%m-%d")
        elif hasattr(today, "strftime"):
            stamp = today.strftime("%Y-%m-%d")
        else:
            stamp = str(today)

        with self._lock:
            if self.conn is None:
                return None
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            print(f"备份目录创建失败: {e}")
            return None

        target = os.path.join(directory, f"ccbar-auto-{stamp}.db")
        if os.path.exists(target):
            return None   # 当天已备份，幂等跳过

        with self._lock:
            if self.conn is None:
                return None
            try:
                self._commit_pending()   # 残留写事务会让 VACUUM INTO 失败
                self.conn.execute("VACUUM INTO ?", (target,))
            except sqlite3.Error as e:
                print(f"自动备份失败: {e}")
                return None

        self._prune_auto_backups(directory, keep)
        return target

    @staticmethod
    def _prune_auto_backups(directory, keep):
        """滚动清理目录内的 ccbar-auto-*.db，只保留文件名最大的 keep 份。

        只删给定目录内、匹配该模式且解析后仍在该目录内的普通文件。
        """
        try:
            names = sorted(n for n in os.listdir(directory)
                           if fnmatch.fnmatch(n, "ccbar-auto-*.db")
                           and os.path.isfile(os.path.join(directory, n)))
        except OSError:
            return
        base = os.path.realpath(directory)
        for name in (names[:-keep] if keep > 0 else names):
            target = os.path.realpath(os.path.join(directory, name))
            if os.path.dirname(target) != base:
                continue   # 符号链接等越界目标不删
            try:
                os.remove(target)
            except OSError:
                pass

    def source_display_name(self, source):
        """source 标识 → 界面显示名（历史聚合行归入 cc-switch）"""
        if source == "zcode":
            return "ZCode"
        if source == "trae":
            return "Trae"
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
