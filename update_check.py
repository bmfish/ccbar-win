"""检查更新：GitHub Releases 对比（与 macOS 版 UpdateCheck 同款口径）。

读取 latest release 的 tag_name 与当前版本做语义化比较；
有新版引导打开 Releases 页面下载。纯函数部分可单测。
"""
import json
import urllib.request

REPO = "bmfish/ccbar-win"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def is_newer_version(latest, current):
    """语义化版本比较：latest 是否比 current 新（非数字段按 0，段数不齐右补 0）"""
    def parts(v):
        out = []
        for seg in (v or "").strip().lstrip("vV").split("."):
            try:
                out.append(int(seg))
            except ValueError:
                out.append(0)
        return out

    a, b = parts(latest), parts(current)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return a > b


def fetch_latest_version(timeout=10):
    """返回最新 release 的版本号（去 v 前缀）；网络失败返回 None"""
    try:
        req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tag = (json.load(resp).get("tag_name") or "").strip().lstrip("vV")
        return tag or None
    except Exception:
        return None
