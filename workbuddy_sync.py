"""WorkBuddy（codebuddy.cn）用量数据源 —— HTTP，与 Trae 同架构但**完全独立**。

与 Trae 的三点差别，都是接口口径决定的：

1. **只有积分，没有 token**。响应里每条请求只给 `credit`（积分），
   `input`/`inputTrunc` 是页面上"输入"列的 prompt 片段（实测恒为 "-"），不是 token 数。
   所以本源的统计只走积分 / 请求数口径，绝不参与 token、费用、渠道等页面。
2. **完全不落 usage_log**。ccBar 的 `usage_all` 视图第一段就是 usage_log 全表，
   WorkBuddy 的需求是"只出现在洞察中心的独立页签"，因此明细进独立的
   `workbuddy_usage` 表（见 stats_store），从存储层就与其他页面隔离。
3. **按天切片拉取**。接口是「区间 + 分页」，一天 27 条、网页端 pageSize=10，
   直接拉 90 天会放大成 250+ 个请求；逐天切片后每天 1~2 个请求，首次回填也只需 30 个。

鉴权：整条 Cookie（`session` 必需，`session_2` 一并带上更稳）。网关在凭据失效时
返回 401 + HTML，因此非 JSON 响应 + 401 判为 auth_expired。
续期：若响应头回写了新的 `session`（服务端滑动续期的常见做法），
`run()` 会把新值拼回整条 Cookie 交给调用方持久化——服务端没有回写也不损失什么。

网络走标准库 urllib（同步、无第三方依赖）；send 可注入，单测因此完全不碰网络。
"""
import json

import http_common
from http_common import (_int, _num, _opt_num, day_slices, fmt_datetime,
                         now_epoch, parse_time_epoch)

# ---- 端点 ----

API_BASE = "https://www.codebuddy.cn"
USAGE_PATH = "/billing/meter/get-user-request-usage"

# ---- 拉取窗口与分页 ----

FIRST_FETCH_DAYS = 30       # 首次回填天数（逐天切片，30 天 = 30 个请求）
PAGE_LOOKBACK_DAYS = 2      # 平时回看天数（跨零点迟到请求 + 服务端事后补记）
PAGE_SIZE = 100             # 首选页大小（服务端若不接受则回落 PAGE_SIZE_FALLBACK）
PAGE_SIZE_FALLBACK = 10     # 网页端默认值，实测可用
PAGE_SIZE_REJECT_CODE = 9004  # 服务端拒绝页大小时返回的业务码（实测过）
MAX_PAGES_PER_DAY = 20      # 每天翻页上限（兜底；撞上即视为本批截断）
REQUEST_TIMEOUT = 20

# 与 Trae 共用同一套节流默认与边界（定义在 http_common，避免两处漂移）
BG_MIN_INTERVAL = http_common.DEFAULT_BG_INTERVAL
INTERACTIVE_MIN_INTERVAL = http_common.INTERACTIVE_MIN_INTERVAL
BG_INTERVAL_MIN = http_common.BG_INTERVAL_MIN
BG_INTERVAL_MAX = http_common.BG_INTERVAL_MAX
NIGHT_SILENT_UNTIL_HOUR = http_common.NIGHT_SILENT_UNTIL_HOUR

clamp_bg_interval = http_common.clamp_bg_interval
is_night_silent = http_common.is_night_silent

# 只保留真正需要的两枚 Cookie，避免把浏览器里其他无关 cookie 抄进设置
KEEP_COOKIES = ("session", "session_2")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": API_BASE,
    "Referer": API_BASE + "/profile/plans-usage",
    "x-client-platform": "web",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"),
}


# ---- 凭据 ----

def normalize_cookie(text):
    """用户粘贴的内容（整条 Cookie / 整条 curl / 单个值）→ 规范化 Cookie 串"""
    return http_common.extract_cookie_from_text(text, keep_only=KEEP_COOKIES)


def cookie_expiry(cookie):
    """从 session 值里解析有效期 epoch（形如 `<id>|<epoch>|<签名>`），解析不出返回 0"""
    value = http_common.parse_cookie_header(cookie).get("session", "")
    for part in value.split("|"):
        stamp = _int(part.strip(), 0)
        if 1_000_000_000 < stamp < 99_999_999_999:
            return stamp
    return 0


def cookie_is_expired(cookie, now=None):
    """凭据是否已过期（解析不出有效期时按未过期处理）"""
    expiry = cookie_expiry(cookie)
    return bool(expiry) and expiry <= now_epoch(now)


# ---- 归一化明细行 ----

class WorkBuddyRow:
    """一条请求（不可变流水，requestId 即幂等主键）"""

    def __init__(self, request_id, client, model, credit, epoch):
        self.request_id = request_id
        self.client = client
        self.model = model
        self.credit = credit
        self.epoch = epoch

    @property
    def key(self):
        return "workbuddy|" + self.request_id

    def __eq__(self, other):
        return isinstance(other, WorkBuddyRow) and self.__dict__ == other.__dict__

    def __repr__(self):
        return "WorkBuddyRow(%r, %r, %r, %r, %r)" % (
            self.request_id, self.client, self.model, self.credit, self.epoch)


def normalize(payload):
    """响应 → WorkBuddyRow 列表。

    结构：{"code":0,"msg":"OK","data":{"total":27,"data":[{...}]}}
    payload 也可以是那个列表本身（便于单测与后续接口微调）。
    缺 requestId / 时间不可解析的行直接跳过（无法幂等、也无法归属日期）。
    """
    if isinstance(payload, dict):
        data = payload.get("data")
        items = data.get("data") if isinstance(data, dict) else data
    else:
        items = payload
    if not isinstance(items, (list, tuple)):
        return []

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        request_id = item.get("requestId")
        epoch = parse_time_epoch(item.get("requestTime"))
        if not isinstance(request_id, str) or not request_id or epoch <= 0:
            continue
        client = item.get("client")
        model = item.get("model")
        credit = _opt_num(item.get("credit"))
        rows.append(WorkBuddyRow(
            request_id=request_id,
            client=client if isinstance(client, str) and client else "未知客户端",
            model=model if isinstance(model, str) and model else "unknown",
            credit=credit if credit is not None else 0.0,
            epoch=epoch,
        ))
    return rows


# ---- 同步结果 ----

class WorkBuddyResult:
    """status ∈ ok / not_configured / auth_expired / failed"""

    def __init__(self, status, rows=None, requests=0, days=0, new_cookie="",
                 truncated=False, message=""):
        self.status = status
        self.rows = rows if rows is not None else []
        # 去重后的明细行数由 len(rows) 表达；requests 是实际发出的 HTTP 次数（便于诊断）
        self.requests = requests
        self.days = days
        self.new_cookie = new_cookie
        self.truncated = truncated
        self.message = message

    @property
    def is_ok(self):
        return self.status == "ok"

    def __repr__(self):
        return ("WorkBuddyResult(%r, rows=%d, requests=%d, days=%d, new_cookie=%r, "
                "truncated=%r, message=%r)" % (
                    self.status, len(self.rows), self.requests, self.days,
                    bool(self.new_cookie), self.truncated, self.message))


# ---- 单次同步 ----

def _post(send, cookie, body):
    headers = dict(HEADERS)
    headers["Cookie"] = cookie
    return send(API_BASE + USAGE_PATH, headers=headers, body=body)


def _fetch_page(send, cookie, start, end, page, page_size):
    """拉一页 → (status, msg, items, total, new_cookie, code)"""
    body = json.dumps({"startTime": fmt_datetime(start),
                       "endTime": fmt_datetime(end - 1),
                       "pageNum": page,
                       "pageSize": page_size},
                      separators=(",", ":"))
    try:
        status, headers, text = _post(send, cookie, body)
    except Exception as e:   # 注入的 transport / 网络层异常都算一次失败
        return ("failed", str(e), [], 0, "", -1)
    if text is None:
        return ("failed", "无响应", [], 0, "", -1)
    try:
        resp = json.loads(text)
    except (ValueError, TypeError):
        # 网关（APISIX）在凭据失效时返回 401 + HTML，不是 JSON
        if status == 401:
            return ("auth_expired", "登录已过期，请重新粘贴 Cookie", [], 0, "", -1)
        return ("failed", "HTTP %s 响应无法解析" % (status or "?"), [], 0, "", -1)

    updates = http_common.extract_set_cookie(headers)
    new_cookie = http_common.cookie_header(
        http_common.merge_cookies({}, updates, keep_only=KEEP_COOKIES))
    if not isinstance(resp, dict):
        return ("failed", "响应结构异常", [], 0, new_cookie, -1)

    code = _int(resp.get("code"), -1)
    if code != 0:
        message = str(resp.get("msg") or "").strip()
        return ("failed", "API code %s%s" % (code, "：" + message if message else ""),
                [], 0, new_cookie, code)

    data = resp.get("data")
    items = data.get("data") if isinstance(data, dict) else None
    total = _int(data.get("total")) if isinstance(data, dict) else 0
    return ("success", "", items if isinstance(items, (list, tuple)) else [],
            total, new_cookie, 0)


def run(cookie, from_epoch, to_epoch, send=None, now=None):
    """完整同步一次（逐天切片 + 分页 + 去重）。

    cookie 是规范化后的整条 Cookie 串；send 可注入（签名同 http_common.http_post）；
    返回的 new_cookie 非空表示服务端回写了新凭据，调用方需要持久化。
    """
    send = send or http_common.http_post
    cookie = normalize_cookie(cookie) or (cookie or "").strip()
    if not cookie:
        return WorkBuddyResult("not_configured", message="未配置登录")
    if "session=" not in cookie:
        return WorkBuddyResult("not_configured",
                               message="Cookie 里没有 session，请重新复制")

    slices = day_slices(from_epoch, to_epoch)
    collected = {}
    requests = 0
    truncated = False
    latest_cookie = ""

    for start, end in slices:
        page = 1
        page_size = PAGE_SIZE
        seen = 0
        while page <= MAX_PAGES_PER_DAY:
            status, msg, items, total, new_cookie, code = _fetch_page(
                send, cookie, start, end, page, page_size)
            requests += 1
            if new_cookie:
                latest_cookie = new_cookie
            if status == "auth_expired":
                return WorkBuddyResult("auth_expired", rows=list(collected.values()),
                                       requests=requests, days=len(slices),
                                       new_cookie=latest_cookie, message=msg)
            if status == "failed":
                # 首次用的请求页大小是推断值（PAGE_SIZE_REJECT_CODE 就是实测到的拒绝码），
                # 首页一旦报业务错就用网页端的默认页大小重试一次；只重试一次，
                # 第二次仍失败就原样上报，别把错因吞掉
                if page == 1 and page_size != PAGE_SIZE_FALLBACK:
                    page_size = PAGE_SIZE_FALLBACK
                    continue
                return WorkBuddyResult("failed", rows=list(collected.values()),
                                       requests=requests, days=len(slices),
                                       new_cookie=latest_cookie, message=msg)
            for row in normalize(items):
                collected[row.key] = row
            seen += len(items)
            # 终止条件只看 total 与空页：服务端若私自把 page_size 压小，
            # 靠 total 仍能翻完（短页不代表最后一页，不能据此提前收工）
            if not items or (total and seen >= total):
                break
            page += 1
        if page > MAX_PAGES_PER_DAY:
            truncated = True

    # 服务端回写的新凭据要用最新的整条 Cookie 覆盖旧值（其余键保持）
    merged_cookie = ""
    if latest_cookie:
        merged_cookie = http_common.cookie_header(http_common.merge_cookies(
            http_common.parse_cookie_header(cookie),
            http_common.parse_cookie_header(latest_cookie)))

    return WorkBuddyResult("ok", rows=list(collected.values()), requests=requests,
                           days=len(slices), new_cookie=merged_cookie,
                           truncated=truncated)


def credit_total(rows):
    """明细行积分合计（给调用方对账 / 测试用）"""
    return sum(_num(r.credit) for r in rows)
