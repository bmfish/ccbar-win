"""Trae 客户端凭据通道（零配置自动发现，Windows）

背景（与 macOS 版 TraeClientCredentials 对齐）：sessionid → Login 链造出的 JWT
鉴权等级不够，会被签到网关拒绝；只有 Trae 客户端自己续签的 JWT 能通过全部业务接口。
客户端把当前账号的 JWT / refreshToken 存在 storage.json 的 tc 信封里，设备 EC P-256
私钥也在同一个文件——本模块只读本机客户端存储，不落日志、不外发。

    %APPDATA%/Trae CN/User/globalStorage/storage.json
      ├─ iCubeAuthInfo://icube.cloudide   → {token, refreshToken, expiredAt, refreshExpiredAt}
      ├─ iCubeAuthInfo://icube-dc:<devId> → {privateKeyPEM}（RT 续签 DeviceProof 签名用）
      └─ telemetry.machineId

tc 信封是客户端 JS 层加密（公开 pepper + AES-128-CBC + SHA512 完整性），
跨平台格式一致，因此 Windows 上可直接按同一套算法解开。
"""
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime

import http_common

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _CRYPTO_AVAILABLE = True
except Exception:      # pragma: no cover - 未安装 cryptography 时自动发现静默跳过
    _CRYPTO_AVAILABLE = False

# 客户端 byteCrypto 的公开常量表（与 mac 版逐字节一致）
WOE_T = [82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
         124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
         84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
         8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37]
VOE_T = [31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
         96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
         160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
         23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125]
ENVELOPE_HEADER = bytes([116, 99, 5, 16, 0, 0])
SHA512_LEN = 64
PEPPER = bytes(a ^ b for a, b in zip(WOE_T, VOE_T))

EXCHANGE_URL = "https://api.trae.com.cn/trae/api/v3/oauth/ExchangeToken"
EXCHANGE_SIGN_PATH = "/trae/api/v3/oauth/ExchangeToken"
CLIENT_ID = "ono9krqynydwx5"

# 测试注入：覆盖真实客户端存储路径（mac 版 storageOverride 同款）
storage_override = None


class ClientTraeAccount:
    """一个客户端账号的全部可用原料"""

    def __init__(self, uid, jwt, jwt_exp, refresh_token="", refresh_exp=0,
                 device_id="", device_key_pem="", source_app=""):
        self.uid = uid
        self.jwt = jwt
        self.jwt_exp = int(jwt_exp or 0)
        self.refresh_token = refresh_token or ""
        self.refresh_exp = int(refresh_exp or 0)
        self.device_id = device_id or ""
        self.device_key_pem = device_key_pem or ""
        self.source_app = source_app or ""

    def __repr__(self):
        return "ClientTraeAccount(uid=%r, jwt_exp=%r, app=%r)" % (
            self.uid, self.jwt_exp, self.source_app)


def storage_urls():
    """客户端存储路径：标准双客户端 + 主目录下的便携/第二客户端数据目录。"""
    if storage_override is not None:
        return list(storage_override)
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    paths = [os.path.join(appdata, name, "User", "globalStorage", "storage.json")
             for name in ("Trae CN", "Trae CN 2")]
    # 第二客户端可能用 --user-data-dir 把数据放在用户主目录（如 ~/TraeCN2），
    # 这类安装不在 %APPDATA% 下，只扫一层目录即可覆盖，避免全盘搜索。
    home = os.path.expanduser("~")
    try:
        names = os.listdir(home)
    except OSError:
        names = []
    for name in names:
        if not name.lower().startswith("trae"):
            continue
        path = os.path.join(home, name, "User", "globalStorage", "storage.json")
        if os.path.isfile(path) and path not in paths:
            paths.append(path)
    return paths


def _now_epoch(now=None):
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _credential_epoch(value):
    """客户端时间字段 → epoch 秒。

    客户端版本之间格式不一致：mac 版存毫秒数，Windows 新版存 ISO 8601 字符串
    （如 2027-05-05T09:00:41.313Z）。解析失败一律返回 0，不能因为一个可选
    字段就让整个凭据发现（进而应用启动）崩掉。
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        seconds = float(value)
        return int(seconds // 1000) if seconds > 1e12 else int(seconds)
    text = str(value).strip()
    if not text:
        return 0
    try:
        seconds = float(text)
    except ValueError:
        try:
            return int(datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text).timestamp())
        except ValueError:
            return 0
    return int(seconds // 1000) if seconds > 1e12 else int(seconds)


def discover(now=None):
    """扫描双客户端并按 uid 去重（同账号保留 JWT 更新的一份）"""
    if not _CRYPTO_AVAILABLE:
        return []
    by_uid = {}
    for path in storage_urls():
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        cloudide = _cloudide_entry(obj)
        if not cloudide:
            continue
        jwt = cloudide.get("token") or ""
        parsed = jwt_payload(jwt)
        if not parsed or not parsed[0]:
            continue
        device = _device_entry(obj)
        app_name = os.path.basename(os.path.dirname(os.path.dirname(
            os.path.dirname(path))))
        uid, exp = parsed
        try:
            account = ClientTraeAccount(
                uid=uid, jwt=jwt, jwt_exp=exp,
                refresh_token=cloudide.get("refreshToken") or "",
                refresh_exp=_credential_epoch(cloudide.get("refreshExpiredAt")),
                device_id=(device or {}).get("device_id", ""),
                device_key_pem=(device or {}).get("private_key_pem", ""),
                source_app=app_name)
        except (TypeError, ValueError):
            continue
        old = by_uid.get(uid)
        if old is None or old.jwt_exp <= account.jwt_exp:
            by_uid[uid] = account
    return [by_uid[k] for k in sorted(by_uid)]


def _cloudide_entry(obj):
    """icube.cloudide 键解密（活跃账号 JWT/RT）"""
    raw = obj.get("iCubeAuthInfo://icube.cloudide")
    if not isinstance(raw, str):
        return None
    try:
        entry = json.loads(tc_decrypt(raw))
    except Exception:
        return None
    return entry if isinstance(entry, dict) and entry.get("token") else None


def _device_entry(obj):
    """icube-dc 键解密（设备 EC 私钥；多把时取第一把能解析的）"""
    for key, value in obj.items():
        if not key.startswith("iCubeAuthInfo://icube-dc:"):
            continue
        device_id = key[len("iCubeAuthInfo://icube-dc:"):]
        if not device_id.isdigit() or not isinstance(value, str):
            continue
        try:
            entry = json.loads(tc_decrypt(value))
        except Exception:
            continue
        pem = entry.get("privateKeyPEM") if isinstance(entry, dict) else None
        if isinstance(pem, str) and "BEGIN" in pem:
            return {"device_id": device_id, "private_key_pem": pem}
    return None


# ------------------------------------------------------------ JWT

def jwt_payload(token):
    """JWT payload → (uid, exp)；解析失败返回 None（签名不校，业务码为准）"""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    b64 = parts[1].replace("-", "+").replace("_", "/")
    b64 += "=" * (-len(b64) % 4)
    try:
        obj = json.loads(base64.b64decode(b64))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    uid = data.get("id") or obj.get("uid") or ""
    exp = obj.get("exp") or 0
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        exp = 0
    return (str(uid), exp)


def jwt_usable(account, now=None):
    """客户端 JWT 仍可用：非空 + exp > now + 120"""
    return bool(account.jwt) and account.jwt_exp > _now_epoch(now) + 120


# ------------------------------------------------------------ RT 续签

def proof_message(sign_path, client_id, token, ts, nonce):
    """DeviceProof 签名原文（对齐客户端 main.js 逆向：5 个 \\n 连接）"""
    return "POST\n%s\n%s\n%s\n%s\n%s" % (sign_path, client_id, token, ts, nonce)


def nonce():
    return secrets.token_hex(16)


def refresh(account, now=None, post=None):
    """用 RT + 设备私钥换新 JWT；不可行/失败返回 None（调用方回落 sessionid 或等客户端）"""
    if not _CRYPTO_AVAILABLE:
        return None
    if not (account.refresh_token and account.device_key_pem and account.device_id):
        return None
    try:
        key = serialization.load_pem_private_key(
            account.device_key_pem.encode("utf-8"), password=None)
    except Exception:
        return None
    ts = int(_now_epoch(now))
    nonce_text = nonce()
    msg = proof_message(EXCHANGE_SIGN_PATH, CLIENT_ID, account.refresh_token, ts, nonce_text)
    try:
        sig = key.sign(msg.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except Exception:
        return None
    sig_b64 = base64.b64encode(sig).decode("ascii")
    body = json.dumps({
        "ClientID": CLIENT_ID,
        "RefreshToken": account.refresh_token,
        "DeviceID": account.device_id,
        "PlatformCode": "IDE_PC",
        "DeviceProof": {"Signature": sig_b64, "Timestamp": ts, "Nonce": nonce_text},
    }, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "x-device-id": account.device_id,
        "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "x-platform-code": "IDE_PC",
        "x-cloudide-token": "",
    }
    send = post or (lambda url, headers, body: http_common.http_post(
        url, headers=headers, body=body, timeout=20))
    try:
        status, _resp_headers, text = send(EXCHANGE_URL, headers, body)
    except Exception:
        return None
    if status != 200 or not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    result = obj.get("Result") if isinstance(obj, dict) else None
    if not isinstance(result, dict):
        return None
    jwt = result.get("Token")
    parsed = jwt_payload(jwt)
    if not parsed:
        return None
    return {"jwt": jwt, "jwt_exp": parsed[1],
            "refresh_token": result.get("RefreshToken") or None}


# ------------------------------------------------------------ tc 信封解密

def sha512(data):
    return hashlib.sha512(data).digest()


def tc_decrypt(b64_text):
    """tc 信封 → 明文。magic/长度/SHA512 完整性任一不符即抛 ValueError"""
    if not _CRYPTO_AVAILABLE:
        raise ValueError("cryptography 未安装")
    raw = base64.b64decode((b64_text or "").strip())
    if len(raw) <= len(ENVELOPE_HEADER) + 32 + SHA512_LEN:
        raise ValueError("信封长度异常")
    if raw[:6] != ENVELOPE_HEADER:
        raise ValueError("magic 不匹配")
    random_bytes = raw[6:38]
    cipher_bytes = raw[38:]
    derived = sha512(sha512(random_bytes) + PEPPER)
    key, iv = derived[:16], derived[16:32]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    if not padded:
        raise ValueError("明文为空")
    pad = padded[-1]
    if pad < 1 or pad > 16 or len(padded) < pad + SHA512_LEN:
        raise ValueError("padding 异常")
    plain = padded[:-pad]
    tag, body = plain[:SHA512_LEN], plain[SHA512_LEN:]
    if sha512(body) != tag:
        raise ValueError("完整性校验失败")
    return body.decode("utf-8")
