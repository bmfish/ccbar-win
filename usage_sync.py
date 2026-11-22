"""多设备用量云同步（与 macOS 版 UsageSync / cc-service usage_event 对齐）。

- 上传：只读本机 origin=local 的明细，按数据源水位 + 3 天重叠窗口增量上报；
  每 24 小时把窗口扩到 30 天对账。整批 2xx 后才推进水位。
- 下载：按服务端 seq 游标拉取其他设备的行，写入本机 usage_log（origin=cloud）；
  同主键的本机行优先，绝不被覆盖；cloud 行绝不上传，避免回声。
- 登录：用户名 + 密码换会话 Token；密码只在登录请求里出现，绝不落盘。
"""
import json
import threading
from datetime import datetime

import http_common
from l10n import L

BATCH = 500
OVERLAP_SECONDS = 3 * 86400
RECONCILE_SECONDS = 30 * 86400
RECONCILE_INTERVAL = 86400
MAX_PAGES = 400
DEFAULT_BASE_URL = "http://139.196.108.26/api"


def normalize_base_url(url):
    """服务地址归一：补 http:// 与 /api 后缀，去掉尾部斜杠。"""
    value = (url or "").strip()
    if not value:
        value = DEFAULT_BASE_URL
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    value = value.rstrip("/")
    if not value.endswith("/api"):
        value += "/api"
    return value


class SyncOutcome:
    """一次同步的结果（供设置页展示）。"""

    def __init__(self, uploaded=0, pulled=0, error="", skipped=False):
        self.uploaded = int(uploaded)
        self.pulled = int(pulled)
        self.error = error or ""
        self.skipped = skipped

    @property
    def ok(self):
        return not self.error and not self.skipped

    def message(self):
        if self.error:
            return self.error
        if self.skipped:
            return L("同步跳过")
        return L("已同步：上传 %d 条 · 拉取 %d 条") % (self.uploaded, self.pulled)


class _Abort(Exception):
    pass


class UsageSync:
    """云同步执行器。线程安全：同一时刻只跑一轮，其余调用直接跳过。"""

    def __init__(self, store, settings, post=None, get=None):
        self.store = store
        self.settings = settings
        self._post = post or http_common.http_post
        self._get = get or http_common.http_get
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------ 登录

    def login(self, username, password, base_url=None):
        """用户名密码登录 → (token, 错误文案)。成功时 token 非空。"""
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            return None, "请填写用户名和密码"
        base = normalize_base_url(base_url or self.settings.get("usage_sync_url", ""))
        status, _, text = self._post(
            base + "/auth/login",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body=json.dumps({"username": username, "password": password},
                            ensure_ascii=False))
        if status == 0:
            return None, "网络不可用，请稍后重试"
        if status == 401:
            return None, "用户名或密码错误"
        if not 200 <= status < 300:
            return None, "登录失败（HTTP %s）" % status
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return None, "服务端返回格式异常"
        token = (data or {}).get("token") or ""
        if not token:
            return None, "登录失败：服务端未返回 Token"
        return token, ""

    def login_and_save(self, username, password, base_url=None):
        """登录成功后写入用户名 / Token / 地址（不保存密码）。"""
        token, error = self.login(username, password, base_url)
        if not token:
            return None, error
        base = normalize_base_url(base_url or self.settings.get("usage_sync_url", ""))
        self.settings.apply({
            "usage_sync_url": base,
            "usage_sync_username": (username or "").strip(),
            "usage_sync_token": token,
            "usage_sync_enabled": True,
        })
        return token, ""

    # ------------------------------------------------------------ 调度

    def run(self, force=False, now=None):
        """执行一轮同步。自动调用跟随 Trae 拉取节奏，由 sync_trae 在拉取后触发；
        force=True 用于设置页手动同步。"""
        if not force and not bool(self.settings.get("usage_sync_enabled", False)):
            return SyncOutcome(skipped=True)
        now_epoch = float(now) if now is not None else datetime.now().timestamp()

        if not self._run_lock.acquire(blocking=False):
            return SyncOutcome(skipped=True)

        try:
            return self._run_locked(now_epoch)
        finally:
            self._run_lock.release()

    def _run_locked(self, now_epoch):
        username = (self.settings.get("usage_sync_username", "") or "").strip()
        token = (self.settings.get("usage_sync_token", "") or "").strip()
        if not username or not token:
            msg = "请先登录云同步账号"
            self.store.set_usage_sync_status(msg, msg)
            return SyncOutcome(error=msg)

        base = normalize_base_url(self.settings.get("usage_sync_url", ""))
        try:
            # 启动时补算上次崩溃遗留的历史聚合标记
            self.store.flush_usage_cloud_agg()

            identity = self.store.usage_sync_meta("cloud_usage_identity", "")
            if identity and identity != username:
                # 换账号：清掉上一个账号拉回的数据、游标和水位，本机数据重传
                self.store.reset_usage_cloud_state()
            self.store.set_usage_sync_meta("cloud_usage_identity", username)

            device_id = self.store.usage_device_id()
            uploaded = self._upload(base, token, device_id, now_epoch)
            pulled = self._download(base, token)
            outcome = SyncOutcome(uploaded=uploaded, pulled=pulled)
            self.store.set_usage_sync_status(outcome.message(), "")
            return outcome
        except _Abort as e:
            message = str(e)
            self.store.set_usage_sync_status("同步失败：" + message, message)
            return SyncOutcome(error=message)
        except Exception as e:  # 网络 / SQLite / JSON 异常都不许打挂托盘
            message = "同步失败：%s" % e
            self.store.set_usage_sync_status(message, str(e))
            return SyncOutcome(error=message)
        finally:
            try:
                self.store.flush_usage_cloud_agg()
            except Exception:
                pass

    # ------------------------------------------------------------ 上传

    def _upload(self, base, token, device_id, now_epoch):
        sources = self.store.usage_local_sources()
        if not sources:
            return 0

        reconcile_at = self._meta_float("cloud_usage_reconcile_at")
        reconcile = now_epoch - reconcile_at >= RECONCILE_INTERVAL
        uploaded = 0

        for item in sources:
            source = item["source"]
            watermark = self._meta_int("cloud_usage_watermark_" + source)
            after = None
            if watermark > 0:
                overlap = RECONCILE_SECONDS if reconcile else OVERLAP_SECONDS
                after = (max(watermark - overlap, 0), "")

            pages = 0
            while True:
                rows = self.store.usage_local_batch(source, after, BATCH)
                if not rows:
                    break
                events = [self._event_payload(row) for row in rows]
                result = self._call(base + "/usage/events", token, {
                    "deviceId": device_id,
                    "events": events,
                })
                if int(result.get("received", -1)) != len(events):
                    raise _Abort("服务端接收数量不一致")

                uploaded += len(events)
                last = rows[-1]
                last_created = int(last["created_at"])
                if last_created > self._meta_int("cloud_usage_watermark_" + source):
                    self.store.set_usage_sync_meta(
                        "cloud_usage_watermark_" + source, str(last_created))

                after = (last_created, last["request_id"])
                pages += 1
                if len(rows) < BATCH:
                    break
                if pages >= MAX_PAGES:
                    raise _Abort("上传分页过多，已中止")

        if reconcile:
            self.store.set_usage_sync_meta(
                "cloud_usage_reconcile_at", str(int(now_epoch)))
        return uploaded

    @staticmethod
    def _event_payload(row):
        return {
            "source": row["source"],
            "requestId": row["request_id"],
            "appType": row.get("app_type", ""),
            "model": row.get("model", ""),
            "inputTokens": row.get("input_tokens", 0),
            "outputTokens": row.get("output_tokens", 0),
            "cacheReadTokens": row.get("cache_read_tokens", 0),
            "cacheCreationTokens": row.get("cache_creation_tokens", 0),
            "reasoningTokens": row.get("reasoning_tokens", 0),
            "totalCostUsd": row.get("total_cost_usd", 0),
            "credits": row.get("credits", 0),
            "requestCount": row.get("request_count", 1),
            "account": row.get("account", ""),
            "createdAt": row.get("created_at", 0),
        }

    # ------------------------------------------------------------ 下载

    def _download(self, base, token):
        seq = self._meta_int("cloud_usage_seq")
        pulled = 0
        pages = 0
        while True:
            data = self._call(
                "%s/usage/events?after=%d&limit=%d" % (base, seq, BATCH),
                token, None)
            events = data.get("events") or []
            next_seq = int(data.get("nextSeq") or seq)
            if events:
                self.store.apply_usage_cloud_events(
                    [self._event_from_payload(e) for e in events])
                pulled += len(events)
            if next_seq <= seq:
                break
            seq = next_seq
            self.store.set_usage_sync_meta("cloud_usage_seq", str(seq))
            pages += 1
            if len(events) < BATCH:
                break
            if pages >= MAX_PAGES:
                raise _Abort("下载分页过多，已中止")
        return pulled

    @staticmethod
    def _event_from_payload(event):
        return {
            "source": event.get("source", ""),
            "request_id": event.get("requestId", ""),
            "app_type": event.get("appType", ""),
            "model": event.get("model", ""),
            "input_tokens": event.get("inputTokens", 0),
            "output_tokens": event.get("outputTokens", 0),
            "cache_read_tokens": event.get("cacheReadTokens", 0),
            "cache_creation_tokens": event.get("cacheCreationTokens", 0),
            "reasoning_tokens": event.get("reasoningTokens", 0),
            "total_cost_usd": event.get("totalCostUsd", 0),
            "credits": event.get("credits", 0),
            "created_at": event.get("createdAt", 0),
            "request_count": event.get("requestCount", 1),
            "account": event.get("account", ""),
        }

    # ------------------------------------------------------------ HTTP / meta

    def _call(self, url, token, payload):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            headers["Content-Type"] = "application/json"
            status, _, text = self._post(
                url, headers=headers,
                body=json.dumps(payload, ensure_ascii=False))
        else:
            status, _, text = self._get(url, headers=headers)

        if status == 401:
            self.settings.set("usage_sync_token", "")
            self.settings.save()
            raise _Abort("登录已过期，请重新登录")
        if status == 0:
            raise _Abort("网络不可用，请稍后重试")
        if not 200 <= status < 300:
            raise _Abort("同步失败（HTTP %s）" % status)
        try:
            return json.loads(text or "{}")
        except ValueError:
            raise _Abort("服务端返回格式异常")

    def _meta_int(self, key):
        try:
            return int(float(self.store.usage_sync_meta(key, "0") or 0))
        except (TypeError, ValueError):
            return 0

    def _meta_float(self, key):
        try:
            return float(self.store.usage_sync_meta(key, "0") or 0)
        except (TypeError, ValueError):
            return 0.0
