"""Trae 每日签到引擎（Windows，与 macOS 版 TraeCheckin 对齐）

2026-11 实证：签到接口（checkin_credits/status|claim）一直可用；此前 1001 +
enable:false 是 sessionid 链 JWT 鉴权等级不够被拒，客户端源 JWT 直接通过。
请求须带完整客户端形态请求头（VSCode UA + x-lscbd-aid + package-type 等）。

奖励金额取 status 的 credits + extra_credits：claim 成功响应只有
{"code":0,"message":"success"}，**不带金额**；真实"今日已签"标志是
did_checked_in（见过 checked_in=false + did_checked_in=true，此时 claim 返 9095）。
"""
import hashlib
import json
import secrets

import http_common

API_BASE = "https://api.trae.cn"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"


class CheckinStatus:
    def __init__(self, enable=False, checked_in=False, credits=0.0,
                 extra_credits=0.0, code=None, message=""):
        self.enable = enable
        self.checked_in = checked_in
        self.credits = credits
        self.extra_credits = extra_credits
        self.code = code
        self.message = message

    @property
    def reward(self):
        return self.credits + self.extra_credits

    def __repr__(self):
        return "CheckinStatus(enable=%r, checked_in=%r, credits=%r, extra=%r, code=%r)" % (
            self.enable, self.checked_in, self.credits, self.extra_credits, self.code)


class CheckinClaim:
    def __init__(self, ok, code=None, message="", credits=0.0, already=False):
        self.ok = ok
        self.code = code
        self.message = message
        self.credits = credits
        # 服务端返回「今日已经签到」：本次没有真正领取，不应再记一笔奖励
        self.already = already

    def __repr__(self):
        return "CheckinClaim(ok=%r, code=%r, message=%r, already=%r)" % (
            self.ok, self.code, self.message, self.already)


def _seeded(uid, seed, n):
    """按 uid 确定性派生字节（同账号恒定，跨启动稳定）"""
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha512(("%s:%s:%d" % (uid, seed, counter)).encode("utf-8")).digest()
        counter += 1
    return out[:n]


def headers(jwt, uid, device_id):
    """签到网关要求的完整客户端形态请求头（缺头会被 1001 + enable:false 拒绝）"""
    session = _seeded(uid, "sess", 32).hex()
    market = bytearray(_seeded(uid, "market", 16))
    market[6] = (market[6] & 0x0F) | 0x40   # UUID v4 版本位
    market[8] = (market[8] & 0x3F) | 0x80   # UUID v4 变体位
    hx = bytes(market).hex()
    market_uid = "%s-%s-%s-%s-%s" % (hx[:8], hx[8:12], hx[12:16], hx[16:20], hx[20:])
    return {
        "accept": "*/*",
        "accept-language": "zh-CN",
        "authorization": "Cloud-IDE-JWT " + jwt,
        "content-type": "application/json",
        "user-agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "x-market-client-id": "VSCode 1.107.1",
        "x-market-user-id": market_uid,
        "x-user-region": "CN",
        "x-device-id": device_id,
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn",
        "x-request-id": secrets.token_hex(16),
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": "00-%s-01" % secrets.token_hex(8),
        "vscode-sessionid": session,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
    }


def _post(path, jwt, uid, device_id, send=None, timeout=15):
    send = send or http_common.http_post
    try:
        status, _headers, text = send(
            API_BASE + path, headers=headers(jwt, uid, device_id),
            body="{}", timeout=timeout)
    except Exception:
        return None
    if status != 200 or not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _num(obj, key):
    v = obj.get(key)
    if isinstance(v, bool) or v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def status(jwt, uid, device_id, send=None):
    """预检今日是否已签；响应异常时 checked_in=False + code=None"""
    obj = _post(STATUS_PATH, jwt, uid, device_id, send=send)
    if obj is None:
        return CheckinStatus(message="无响应")
    checked = bool(obj.get("checked_in"))
    did = bool(obj.get("did_checked_in"))
    return CheckinStatus(
        enable=bool(obj.get("enable")),
        checked_in=checked or did,
        credits=_num(obj, "credits"),
        extra_credits=_num(obj, "extra_credits"),
        code=obj.get("code"),
        message=obj.get("message") or "")


def claim(jwt, uid, device_id, send=None):
    """领取签到奖励。奖励金额在 status 里，claim 成功响应本身不带金额"""
    obj = _post(CLAIM_PATH, jwt, uid, device_id, send=send)
    if obj is None:
        return CheckinClaim(False, message="无响应")
    code = obj.get("code")
    message = obj.get("message") or ""
    already = ("已经签到" in message) or ("already checked in" in message)
    auth_rejected = "not able to authenticate" in message.lower()
    ok = (code in (0, None)) and not auth_rejected
    return CheckinClaim(ok or already, code=code, message=message, already=already)
