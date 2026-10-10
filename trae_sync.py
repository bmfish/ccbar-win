"""Trae 数据源（HTTP，与 macOS 版 TraeSync.swift 同口径）

Trae 的用量不走本地 SQLite——本地 ai-agent 库是 SQLCipher 加密的，读不了，
所以走官方网页端同一套 API：

  passport sessionid（60 天）                        —— 用户唯一要提供的凭据
   └→ POST /cloudide/api/v3/trae/Login               —— 换 X-Cloudide-Session（14 天，Set-Cookie 返回）
       └→ POST /cloudide/api/v3/common/GetUserToken —— 换 Cloud-IDE-JWT（8 小时，可续签）
           ├→ POST /trae/api/v1/pay/query_user_usage_group_by_session  逐会话用量（token 四元组 + credits + 金额）
           ├→ POST /trae/api/v2/pay/ide_user_ent_usage                 积分总量/已用（官方账单口径）
           └→ POST /trae/api/v2/ug/checkin_credits/status|claim        每日签到（100 积分 + 会员加成）

鉴权头只有 Authorization: Cloud-IDE-JWT，cookie 只在换签时用。
usage_type=[7] 是 IDE 对话/Agent 消耗（1~8 里唯一有数据的类型，实测）。
口径：会话级聚合（跨零点会话计入 usage_time 那天，一行一个 session）；
amount/credits/cost 三者里 credits 是积分消耗、cost_money_float 是折算金额。
网络走标准库 urllib（同步、无第三方依赖）；post 可注入，单测因此完全不碰网络。
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Windows 控制台默认 cp1252，中文 print 会抛 UnicodeEncodeError，统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- 端点与窗口 ----

API_BASE = "https://api.trae.cn"
LOGIN_PATH = "/cloudide/api/v3/trae/Login"
TOKEN_PATH = "/cloudide/api/v3/common/GetUserToken"
USAGE_PATH = "/trae/api/v1/pay/query_user_usage_group_by_session"
ENT_USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"
CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"

# 拉取窗口：首次 90 天全量回填，之后水位 - 2 天回看
FIRST_FETCH_DAYS = 90
PAGE_LOOKBACK_DAYS = 2
# 实测 page_size 上限 20（>20 报 9004 "The submitted order parameters are incorrect"），
# 与 trae.cn 网页端的默认值一致
PAGE_SIZE = 20
MAX_PAGES = 50

# ---- 节流常量（由 StatsStore 侧使用，这里只定义）----

BG_MIN_INTERVAL = 900          # 后台 15 分钟
INTERACTIVE_MIN_INTERVAL = 60  # 点开弹窗 1 分钟
ENT_MIN_INTERVAL = 3600        # 积分账单 1 小时
CHECKIN_RETRY_INTERVAL = 3600  # 签到失败后 1 小时重试（成功/已签由水位挡住）
NIGHT_SILENT_UNTIL_HOUR = 9    # 0–9 点静默


# ---- 基础工具 ----


def _epoch(now=None):
    """now 允许 None / epoch 数字 / datetime，统一成 epoch 秒（float）"""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _int(v, default=0):
    """宽松取整：JSON 里的数字/数字串都接受，其余回落 default"""
    if v is None or isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=0.0):
    """宽松取数（金额/token 缺失即 default）"""
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _opt_num(v):
    """宽松取数但区分"缺失"：缺失/非法返回 None（金额可选字段用）"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jwt_parts(token):
    """按 '.' 切段并丢掉空段（对齐 Swift split 的默认行为）"""
    return [p for p in (token or "").split(".") if p]


def jwt_looks_valid(token):
    """像 JWT 的最低标准：3 段"""
    return len(_jwt_parts(token)) == 3


def jwt_exp(token):
    """解析 JWT payload 的 exp（epoch 秒），失败返回 0"""
    parts = _jwt_parts(token)
    if len(parts) < 2:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        obj = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(obj, dict):
        return 0
    exp = obj.get("exp")
    if exp is None or isinstance(exp, bool):
        return 0
    try:
        return int(exp)
    except (TypeError, ValueError):
        return 0


def extract_session_cookie(set_cookie_header):
    """从响应头里取 X-Cloudide-Session 的值。

    入参一般是把多条 Set-Cookie 合并后的逗号串（URLSession 合并过，urllib 也可能多条），
    也接受 http_post 返回的 headers dict。找不到返回 None。
    """
    if not isinstance(set_cookie_header, str):
        if hasattr(set_cookie_header, "items"):
            vals = [str(v) for k, v in set_cookie_header.items() if str(k).lower() == "set-cookie"]
            set_cookie_header = ", ".join(vals)
        else:
            return None
    for part in (set_cookie_header or "").split(","):
        trimmed = part.strip()
        if trimmed.startswith("X-Cloudide-Session="):
            value = trimmed[len("X-Cloudide-Session="):].split(";")[0].strip()
            return value or None
    return None


def is_night_silent(now=None):
    """0–9 点本地时间静默：凌晨不拉 API，由 9 点后第一次同步的"水位-2 天"窗口覆盖"""
    return _now_dt(now).hour < NIGHT_SILENT_UNTIL_HOUR


def _now_dt(now=None):
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(_epoch(now))


# ---- 凭据状态 ----

class TraeAuth:
    """会话凭据状态（持久化在 meta 表，重启不丢）"""

    def __init__(self, cloudide_session="", jwt="", jwt_exp=0):
        self.cloudide_session = cloudide_session or ""
        self.jwt = jwt or ""
        self.jwt_exp = _int(jwt_exp)

    @staticmethod
    def from_json(text):
        """meta 里的 JSON → TraeAuth；空/坏数据返回 None"""
        if not text:
            return None
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        return TraeAuth(cloudide_session=obj.get("cloudide_session") or "",
                        jwt=obj.get("jwt") or "",
                        jwt_exp=obj.get("jwt_exp") or 0)

    def to_json(self):
        return json.dumps({"cloudide_session": self.cloudide_session,
                           "jwt": self.jwt,
                           "jwt_exp": int(self.jwt_exp)},
                          ensure_ascii=False, separators=(",", ":"))

    def is_jwt_usable(self, now=None):
        """缓存的 JWT 还能用：非空 + 3 段 + exp > now + 120"""
        return bool(self.jwt) and self.jwt_exp > _epoch(now) + 120 and jwt_looks_valid(self.jwt)

    def __eq__(self, other):
        return isinstance(other, TraeAuth) and (self.cloudide_session == other.cloudide_session
                                                and self.jwt == other.jwt
                                                and self.jwt_exp == other.jwt_exp)

    def __repr__(self):
        return "TraeAuth(cloudide_session=%r, jwt=%r, jwt_exp=%r)" % (
            self.cloudide_session, self.jwt, self.jwt_exp)


# ---- 归一化明细行 ----

class TraeRow:
    """归一化明细行（入库前）"""

    def __init__(self, session_id, model, input, output, cache_read, cache_write,
                 cost_usd, credits, epoch):
        self.session_id = session_id
        self.model = model
        self.input = input
        self.output = output
        self.cache_read = cache_read
        self.cache_write = cache_write
        self.cost_usd = cost_usd
        self.credits = credits
        self.epoch = epoch

    @property
    def request_id(self):
        """usage_log 幂等主键：会话是聚合原子单位，续拉同一会话 UPSERT 覆盖"""
        return "trae|" + self.session_id

    def __eq__(self, other):
        return isinstance(other, TraeRow) and self.__dict__ == other.__dict__

    def __repr__(self):
        return ("TraeRow(%r, %r, %r, %r, %r, %r, %r, %r, %r)" % (
            self.session_id, self.model, self.input, self.output, self.cache_read,
            self.cache_write, self.cost_usd, self.credits, self.epoch))


def normalize(payload):
    """把 usage 响应规范化成 TraeRow 列表。

    payload 可以是整个响应 dict，也可以是 user_usage_group_by_sessions 列表。
    会话行 → 明细行：usage_group_details 只按模型细分 credits/金额、无 token 拆分，
    所以整段会话一行入库，token 归到主模型（与 Trae 网页端展示口径一致）。
    """
    if isinstance(payload, dict):
        sessions = payload.get("user_usage_group_by_sessions")
    else:
        sessions = payload
    if not isinstance(sessions, (list, tuple)):
        return []

    rows = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id")
        epoch = _int(s.get("usage_time"))
        if not isinstance(sid, str) or not sid or epoch <= 0:
            continue
        extra = s.get("extra_info")
        if not isinstance(extra, dict):
            extra = {}
        # Trae 的 input_token 是 OpenAI/智谱口径：总量，已包含 cache_read。
        # 库内统一 Anthropic 口径（input=净输入，三项相加=总 prompt），否则合计会重复计算缓存。
        gross = _int(extra.get("input_token"))
        cached = _int(extra.get("cache_read_token"))
        model = s.get("model_name")
        # credits_float 缺失回落 amount_float，两者都缺为 0
        credits = _opt_num(s.get("credits_float"))
        if credits is None:
            credits = _opt_num(s.get("amount_float"))
        rows.append(TraeRow(
            session_id=sid,
            model=model if model is not None else "unknown",
            input=max(0, gross - cached),
            output=_int(extra.get("output_token")),
            cache_read=cached,
            cache_write=_int(extra.get("cache_write_token")),
            cost_usd=_num(s.get("cost_money_float")),
            credits=credits if credits is not None else 0.0,
            epoch=epoch,
        ))
    return rows


# ---- 同步结果 ----

class TraeResult:
    """同步结果对象：status ∈ ok / not_configured / auth_expired / failed"""

    def __init__(self, status, rows=None, consumed=None, total=None, message="",
                 truncated=False):
        self.status = status
        self.rows = rows if rows is not None else []
        self.consumed = consumed
        self.total = total
        self.message = message
        self.truncated = truncated   # 分页到上限没拉完（窗口内会话过多）

    @property
    def is_ok(self):
        return self.status == "ok"

    def __repr__(self):
        return "TraeResult(%r, rows=%d, consumed=%r, total=%r, message=%r)" % (
            self.status, len(self.rows), self.consumed, self.total, self.message)


# ---- 传输（stdlib，同步）----

def http_post(url, cookie=None, auth=None, body=None, timeout=15):
    """默认同步 POST 实现：返回 (status, headers, text)。

    网络/超时等异常回落 (0, None, None)；HTTP 非 2xx 也把状态码与响应体带回来
    （Login 靠状态码判定，不能把 4xx 当断网）。
    """
    req = urllib.request.Request(url, data=(body or "").encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
            headers = dict(resp.headers.items())
            return (status, headers, raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        headers = dict(e.headers.items()) if e.headers else None
        return (e.code, headers, raw.decode("utf-8", "replace"))
    except Exception:
        return (0, None, None)


# ---- 换签链 ----

def _fetch_token(session, send):
    """X-Cloudide-Session → (token, exp)。拿不到 Token 即会话失效（含 code 1001）"""
    _, _, text = send(API_BASE + TOKEN_PATH, cookie="X-Cloudide-Session=" + session, body="{}")
    if text is None:
        return ("failed", "无响应")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return ("failed", "GetUserToken 响应解析失败")
    result = obj.get("Result") if isinstance(obj, dict) else None
    token = result.get("Token") if isinstance(result, dict) else None
    if not token or not isinstance(token, str):
        return ("auth_expired", "会话失效")
    return ("success", (token, jwt_exp(token)))


def _login(passport_cookie, send):
    """passport cookie → 新的 X-Cloudide-Session（Set-Cookie）。失败返回 None"""
    status, headers, _ = send(API_BASE + LOGIN_PATH, cookie=passport_cookie, body="{}")
    if status != 200 or not headers:
        return None
    return extract_session_cookie(headers)


def _ensure_jwt(passport_cookie, auth, send, now):
    """JWT 有效直接用；否则用 X-Cloudide-Session 换新；Session 也没有/失效就先 Login 再换。

    返回 (status, token或原因)。仅 Login 失败才算 auth_expired。
    """
    if auth.is_jwt_usable(now):
        return ("success", auth.jwt)

    if auth.cloudide_session:
        st, val = _fetch_token(auth.cloudide_session, send)
        if st == "success":
            auth.jwt, auth.jwt_exp = val
            return ("success", val[0])
        if st == "failed":
            return ("failed", val)
        # auth_expired：Session 失效，走 Login 重造

    session = _login(passport_cookie, send)
    if not session:
        return ("auth_expired", "登录已过期，请重新提供 sessionid")
    auth.cloudide_session = session
    st, val = _fetch_token(session, send)
    if st == "success":
        auth.jwt, auth.jwt_exp = val
        return ("success", val[0])
    if st == "failed":
        return ("failed", val)
    return ("auth_expired", "登录已过期，请重新提供 sessionid")


# ---- 使用量分页拉取 ----

def _fetch_sessions(jwt, from_epoch, to_epoch, send, cutoff=None):
    """分页拉取会话明细。错误码：1001 = JWT/会话失效；9004 = 参数不合法；成功响应没有 code 字段。

    服务端按 usage_time 倒序返回（最新在前）。cutoff（epoch 秒）用于提前终止：
    某页里最新一条 usage_time 都比 cutoff 旧，说明该页及后续页全是"上次已覆盖"
    的数据，不必再翻（页内数据也一并跳过，反正 UPSERT 幂等）。

    返回 (status, 原因, rows, truncated)。truncated=True 表示翻到 MAX_PAGES 上限还没拉完
    （窗口内会话过多，本批不完整）——调用方应据此不推进"上次完整同步时刻"，避免误剪。
    """
    rows = []
    page = 1
    truncated = False
    while page <= MAX_PAGES:
        body = json.dumps({"start_time": from_epoch, "end_time": to_epoch,
                           "page_size": PAGE_SIZE, "page_num": page,
                           "usage_type": [7]}, separators=(",", ":"))
        _, _, text = send(API_BASE + USAGE_PATH, auth="Cloud-IDE-JWT " + jwt, body=body)
        if text is None:
            return ("failed", "无响应", rows, truncated)
        try:
            resp = json.loads(text)
        except (ValueError, TypeError):
            return ("failed", "响应解析失败", rows, truncated)
        if not isinstance(resp, dict):
            return ("failed", "响应解析失败", rows, truncated)
        code = resp.get("code")
        if code is not None:
            if _int(code, -1) == 1001:
                return ("auth_expired", "会话失效", rows, truncated)
            return ("failed", "API code %s" % code, rows, truncated)
        sessions = resp.get("user_usage_group_by_sessions")
        if not isinstance(sessions, (list, tuple)):
            sessions = []
        # 剪枝：本页最新一条都比 cutoff 旧 → 整页（及后续更旧页）全是已覆盖数据
        if cutoff is not None and sessions:
            newest = max((_int(s.get("usage_time")) for s in sessions
                          if isinstance(s, dict)), default=0)
            if newest < cutoff:
                break
        rows.extend(normalize(sessions))
        # total 缺失按 0 处理（与 macOS 一致：此时首页即止，不再翻页）
        total = _int(resp.get("total"))
        if len(rows) >= total or not sessions:
            break
        page += 1
    if page > MAX_PAGES:
        truncated = True
        print("[ccBar] Trae 分页达到上限 %d，本窗口可能不完整" % MAX_PAGES)
    return ("success", "", rows, truncated)


# ---- 每日签到（100 积分 + 会员加成）----

class TraeCheckinResult:
    """签到结果：status ∈ claimed / already / disabled / not_configured / auth_expired / failed

    claimed 时 credits 是本次到账积分（基础 + 会员加成）；其余状态 credits 为 0。
    """

    def __init__(self, status, credits=0.0, message=""):
        self.status = status
        self.credits = credits or 0.0
        self.message = message

    @property
    def is_claimed(self):
        return self.status == "claimed"

    def __repr__(self):
        return "TraeCheckinResult(%r, credits=%r, message=%r)" % (
            self.status, self.credits, self.message)


def checkin_plan(enable, checked_in):
    """纯决策：enable=False 不参与；checked_in=True 已签；其余尝试领取"""
    if enable is False:
        return "disabled"
    if checked_in is True:
        return "already"
    return "claim"


def _checkin_post(path, jwt, body, send):
    """单次签到请求。返回 (status, payload)：success 时 payload 是响应对象，否则是原因"""
    _, _, text = send(API_BASE + path, auth="Cloud-IDE-JWT " + jwt, body=body)
    if text is None:
        return ("failed", "无响应")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return ("failed", "签到响应解析失败")
    if not isinstance(obj, dict):
        return ("failed", "签到响应解析失败")
    if _int(obj.get("code"), -1) == 1001:
        return ("auth_expired", "会话失效")
    return ("success", obj)


def _checkin_with_retry(path, body, sessionid, auth, send, now):
    """带一次强制换签重试的签到请求（status 与 claim 共用）。

    缓存的 JWT 可能被服务端提前作废（exp 未到但 1001），此时清掉重签再试一次，
    而不是把 cookie 误判成过期。返回 (status, payload)。
    """
    status, payload = _checkin_post(path, auth.jwt, body, send)
    if status != "auth_expired":
        return (status, payload)

    auth.jwt = ""
    auth.jwt_exp = 0
    st, val = _ensure_jwt(sessionid, auth, send, now)
    if st == "success":
        return _checkin_post(path, val, body, send)
    if st == "not_configured":
        return ("not_configured", val)
    if st == "auth_expired":
        return ("auth_expired", val)
    return ("failed", val)


def checkin(sessionid, auth=None, post=None, now=None):
    """每日签到：先查状态，未签则领取。

    auth 会被就地更新（换签结果），调用方负责持久化；post 可注入假 transport；
    返回 TraeCheckinResult（不抛异常）。
    """
    send = post or http_post
    auth = auth if auth is not None else TraeAuth()
    sessionid = (sessionid or "").strip()
    if not sessionid:
        return TraeCheckinResult("not_configured", message="未配置登录")

    # ---- 1. 确保 JWT 可用（无效就换签）----
    st, val = _ensure_jwt(sessionid, auth, send, now)
    if st == "not_configured":
        return TraeCheckinResult("not_configured", message="未配置登录")
    if st == "auth_expired":
        return TraeCheckinResult("auth_expired", message=val)
    if st == "failed":
        return TraeCheckinResult("failed", message=val)

    expired_msg = "登录已过期，请重新提供 sessionid"

    # ---- 2. 查签到状态（1001 → 强制换签重试一次）----
    st, obj = _checkin_with_retry(CHECKIN_STATUS_PATH, "{}", sessionid, auth, send, now)
    if st == "not_configured":
        return TraeCheckinResult("not_configured", message="未配置登录")
    if st == "auth_expired":
        return TraeCheckinResult("auth_expired", message=expired_msg)
    if st == "failed":
        return TraeCheckinResult("failed", message=obj)

    plan = checkin_plan(obj.get("enable"), obj.get("checked_in"))
    if plan == "already":
        return TraeCheckinResult("already")
    if plan == "disabled":
        return TraeCheckinResult("disabled")

    # ---- 3. 领取（官方客户端同样带 req_source）----
    st, obj = _checkin_with_retry(CHECKIN_CLAIM_PATH, '{"req_source":1}',
                                  sessionid, auth, send, now)
    if st == "not_configured":
        return TraeCheckinResult("not_configured", message="未配置登录")
    if st == "auth_expired":
        return TraeCheckinResult("auth_expired", message=expired_msg)
    if st == "failed":
        return TraeCheckinResult("failed", message=obj)

    code = obj.get("code")
    if code is not None and _int(code, -1) != 0:
        msg = "签到失败 code %s" % code
        if obj.get("message"):
            msg += "：" + str(obj["message"])
        return TraeCheckinResult("failed", message=msg)

    credits = _num(obj.get("credits")) + _num(obj.get("extra_credits"))
    return TraeCheckinResult("claimed", credits=credits)


# ---- 主入口 ----

def run(sessionid, auth=None, from_epoch=0, to_epoch=None, fetch_ent=False,
        post=None, now=None, cutoff=None):
    """完整同步一次。

    auth 会被就地更新（换签结果），调用方负责持久化；
    post 可注入假 transport（签名同 http_post），测试因此不碰网络；
    fetch_ent=False 时跳过积分账单（它 1 小时刷一次就够，减少无谓请求）；
    cutoff（epoch 秒）透传 _fetch_sessions：某页最新 usage_time 都比 cutoff 旧
    就提前终止翻页（上次已覆盖的数据不用重拉；None=不剪枝）。
    """
    send = post or http_post
    auth = auth if auth is not None else TraeAuth()
    sessionid = (sessionid or "").strip()
    if to_epoch is None:
        to_epoch = int(_epoch(now))

    if not sessionid:
        return TraeResult("not_configured", message="未配置登录")

    # ---- 1. 确保 JWT 可用（无效就换签）----
    st, val = _ensure_jwt(sessionid, auth, send, now)
    if st == "not_configured":
        return TraeResult("not_configured", message="未配置登录")
    if st == "auth_expired":
        return TraeResult("auth_expired", message=val)
    if st == "failed":
        return TraeResult("failed", message=val)
    jwt = val

    # ---- 2. 拉取会话明细（分页）----
    # 缓存的 JWT 可能被服务端提前作废（如用户在别处重新登录，exp 未到但 1001），
    # 此时强制清掉换一张再试一次，而不是把 cookie 误判成过期
    jwt2 = jwt
    st, msg, rows, truncated = _fetch_sessions(jwt, from_epoch, to_epoch, send, cutoff=cutoff)
    if st == "failed":
        return TraeResult("failed", message=msg)
    if st == "auth_expired":
        # 强制换签（清 JWT；Session 也不行时 _ensure_jwt 内部会走 Login 重造）
        auth.jwt = ""
        auth.jwt_exp = 0
        st, val = _ensure_jwt(sessionid, auth, send, now)
        if st in ("not_configured", "auth_expired"):
            return TraeResult("auth_expired", message="登录已过期，请重新提供 sessionid")
        if st == "failed":
            return TraeResult("failed", message=val)
        jwt2 = val
        st, msg, rows, truncated = _fetch_sessions(jwt2, from_epoch, to_epoch, send, cutoff=cutoff)
        if st == "auth_expired":
            return TraeResult("auth_expired", message="登录已过期，请重新提供 sessionid")
        if st == "failed":
            return TraeResult("failed", message=msg)

    # ---- 3. 积分汇总（官方账单口径；失败不致命，按小时节流由调用方控制）----
    consumed = None
    total = None
    if fetch_ent:
        _, _, text = send(API_BASE + ENT_USAGE_PATH, auth="Cloud-IDE-JWT " + jwt2,
                          body='{"require_usage":true}')
        if text is not None:
            try:
                resp = json.loads(text)
            except (ValueError, TypeError):
                resp = None
            summary = resp.get("usage_summary") if isinstance(resp, dict) else None
            if isinstance(summary, dict):
                consumed = _opt_num(summary.get("consumed_amount"))
                total = _opt_num(summary.get("total_amount"))

    return TraeResult("ok", rows=rows, consumed=consumed, total=total,
                      truncated=truncated)
