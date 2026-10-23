"""HTTP 数据源的公共部件（WorkBuddy / Trae 这类「不走本地库」的源共用）。

只放与具体源无关的东西：节流常量与规整、夜间静默、宽松取值、时间解析、按天切片、
Cookie 解析、以及标准库 POST 传输。源自己的端点 / 分页 / 归一化仍留在各自模块。

依赖：仅标准库（无第三方），可单独 import，不产生循环依赖。
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# ---- 节流（两个 HTTP 源共用同一套默认与边界）----

DEFAULT_BG_INTERVAL = 600      # 后台默认 10 分钟
INTERACTIVE_MIN_INTERVAL = 60  # 用户点开界面时 1 分钟
BG_INTERVAL_MIN = 60           # 后台间隔可配下限（秒）
BG_INTERVAL_MAX = 7200         # 后台间隔可配上限（秒）
NIGHT_SILENT_UNTIL_HOUR = 9    # 0–9 点后台静默

DEFAULT_TIMEOUT = 20           # 单次请求超时（秒）
RETRY_ATTEMPTS = 2             # 网络错误 / 429 / 5xx 的重试次数
RETRY_BASE_DELAY = 0.6         # 首次重试等待（秒），之后翻倍
MAX_RETRY_DELAY = 5.0          # 单次重试等待上限（Retry-After 也夹到这里）
RETRY_STATUSES = (429, 500, 502, 503, 504)


# ============================================================
# 宽松取值 / 时间工具
# ============================================================

def _int(v, default=0):
    """宽松取整：数字 / 数字串都接受，其余回落 default"""
    if v is None or isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=0.0):
    """宽松取数（金额 / 积分缺失即 default）"""
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _opt_num(v):
    """宽松取数但区分「缺失」：缺失 / 非法返回 None"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def now_epoch(now=None):
    """now（None=此刻 / datetime / epoch 数字）→ epoch 秒"""
    if now is None:
        return datetime.now().timestamp()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _now_dt(now=None):
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(float(now))


def parse_time_epoch(value):
    """各种时间写法 → epoch 秒；认不出来返回 0。

    支持 "2026-10-16 16:35:00" / "2026-10-16T16:35:00" / "2026-10-16" /
    epoch 秒（10 位）与毫秒（13 位）的数字或数字串。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return _from_number(value)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return _from_number(int(text))
    normalized = text.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(normalized, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _from_number(value):
    """数字时间戳：13 位按毫秒，10 位按秒（负数 / 0 一律当无效）"""
    number = float(value)
    if number <= 0:
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)


def day_slices(from_epoch, to_epoch):
    """[from, to) 按本地自然日切段 → [(start_epoch, end_epoch_exclusive)]

    逐天拉取是为了避免「一大段区间 × 分页」把请求数放大到不可控。
    """
    if to_epoch <= from_epoch:
        return []
    slices = []
    cursor = datetime.fromtimestamp(from_epoch).replace(hour=0, minute=0,
                                                       second=0, microsecond=0)
    last = datetime.fromtimestamp(to_epoch)
    while cursor < last:
        nxt = cursor + timedelta(days=1)
        start = max(int(cursor.timestamp()), int(from_epoch))
        end = min(int(nxt.timestamp()), int(to_epoch))
        if end > start:
            slices.append((start, end))
        cursor = nxt
    return slices


def fmt_datetime(epoch):
    """epoch → "YYYY-MM-DD HH:MM:SS"（接口入参格式）"""
    return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 节流 / 静默
# ============================================================

def clamp_bg_interval(value, default=None):
    """后台同步间隔规整（秒）：非法 / 缺失回落 default，否则夹进
    [BG_INTERVAL_MIN, BG_INTERVAL_MAX]，坏设置不会把托盘节流打乱。"""
    fallback = DEFAULT_BG_INTERVAL if default is None else default
    seconds = _int(value, None)
    if seconds is None:
        return fallback
    return max(BG_INTERVAL_MIN, min(BG_INTERVAL_MAX, seconds))


def is_night_silent(now=None):
    """0–9 点本地时间静默：凌晨不拉 API，由 9 点后第一次同步的回看窗口覆盖"""
    return _now_dt(now).hour < NIGHT_SILENT_UNTIL_HOUR


# ============================================================
# Cookie 解析（用户粘贴 / 响应回写都走这里）
# ============================================================

def parse_cookie_header(text):
    """Cookie 串 → {name: value}。容忍空段、多余分号与尾部换行。"""
    cookies = {}
    for part in (text or "").replace("\r", "\n").replace("\n", ";").split(";"):
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        cookies[name] = value.strip()
    return cookies


def cookie_header(cookies):
    """{name: value} → "a=b; c=d"（按名字排序，便于比较与幂等）"""
    return "; ".join("%s=%s" % (k, v) for k, v in sorted((cookies or {}).items()))


def merge_cookies(existing, updates, keep_only=None):
    """把新拿到的 cookie 并进旧的（同名覆盖）；keep_only 有值时只保留这些名字。"""
    merged = dict(existing or {})
    merged.update({k: v for k, v in (updates or {}).items() if v})
    if keep_only:
        merged = {k: v for k, v in merged.items() if k in keep_only}
    return merged


def extract_cookie_from_text(text, keep_only=None):
    """用户粘贴的内容 → 规范化 Cookie 串。

    支持三种粘贴方式（都从浏览器复制得来）：
      1. 整条 Cookie 串：        "session=xxx; session_2=yyy"
      2. 整条 curl 命令：        curl '...' -b 'session=xxx' / -H 'cookie: session=xxx'
      3. 只有单个值：            "session=xxx" 或直接是值（不推荐，按 session 处理）
    取不到任何键值对时返回 ""。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # curl 形式：优先取 -b / --cookie / -H 'cookie: ...'
    if raw.lower().startswith("curl") or " -b " in raw or "-h 'cookie" in raw.lower():
        for marker in ("-b ", "--cookie ", "-b'", "--cookie=", "cookie: "):
            idx = raw.lower().find(marker)
            if idx >= 0:
                rest = raw[idx + len(marker):].lstrip()
                if rest[:1] in ("'", '"'):
                    quote = rest[0]
                    end = rest.find(quote, 1)
                    if end > 0:
                        raw = rest[1:end]
                        break
                else:
                    raw = rest.split()[0] if rest.split() else ""
                    break
    cookies = parse_cookie_header(raw)
    if not cookies and "=" not in raw:
        cookies = {"session": raw}   # 只粘了值：按 session 处理
    if keep_only:
        cookies = {k: v for k, v in cookies.items() if k in keep_only}
    return cookie_header(cookies)


def extract_set_cookie(headers):
    """响应头 → {name: value}（兼容 urllib 的 dict 与多条 Set-Cookie 合并串）"""
    if not headers:
        return {}
    values = []
    if hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == "set-cookie":
                values.extend(str(value).split("\n"))
    else:
        values = str(headers).split("\n")
    cookies = {}
    for item in values:
        for chunk in str(item).split(","):
            name, sep, rest = chunk.strip().partition("=")
            if not sep:
                continue
            name = name.strip()
            if not name or name.lower() in ("path", "domain", "expires", "max-age",
                                            "samesite", "secure", "httponly"):
                continue
            cookies[name] = rest.split(";")[0].strip()
    return cookies


# ============================================================
# 传输（stdlib，同步；可注入假实现用于单测）
# ============================================================

def _header_value(headers, name):
    """响应头大小写不敏感取值（urllib 返回的键大小写不固定）"""
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def _retry_delay(attempt, headers):
    """重试等待：优先服务端 Retry-After（秒），否则指数退避，统一夹在 5 秒内"""
    try:
        retry_after = float(_header_value(headers, "Retry-After"))
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None:
        return min(max(retry_after, 0.0), MAX_RETRY_DELAY)
    return min(RETRY_BASE_DELAY * (2 ** attempt), MAX_RETRY_DELAY)


def _http_post_once(url, headers, body, timeout):
    req = urllib.request.Request(url, data=(body or "").encode("utf-8"), method="POST")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
            return (status, dict(resp.headers.items()), raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        headers_out = dict(e.headers.items()) if e.headers else None
        return (e.code, headers_out, raw.decode("utf-8", "replace"))
    except Exception:
        return (0, None, None)


def http_post(url, headers=None, body=None, timeout=DEFAULT_TIMEOUT,
              retries=RETRY_ATTEMPTS):
    """POST → (status, headers_dict, text)。

    网络 / 超时异常回落 (0, None, None)；HTTP 非 2xx 也把状态码与响应体带回来
    （网关 401 返回 HTML 时，调用方靠状态码判定凭据过期，不能当断网）。
    网络错误、429 与 5xx 自动重试（指数退避，尊重 Retry-After），避免一次抖动
    就浪费掉一整轮同步节流窗口。
    """
    result = (0, None, None)
    for attempt in range(retries + 1):
        result = _http_post_once(url, headers, body, timeout)
        status, resp_headers, _text = result
        if status != 0 and status not in RETRY_STATUSES:
            return result
        if attempt >= retries:
            return result
        time.sleep(_retry_delay(attempt, resp_headers))
    return result
