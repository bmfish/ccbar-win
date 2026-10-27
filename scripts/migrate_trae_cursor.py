#!/usr/bin/env python3
"""一次性迁移：把旧的全局 Trae 同步游标挂到指定账号 uid 名下（多账号 per-uid 游标方案）。

背景：ccBar-win 多账号版起游标（水位/剪枝基准/余额）按 uid 记账：
  trae_synced_day_<uid> / trae_synced_at_<uid> / trae_ent_<uid>
本脚本把旧全局键的值搬给指定 uid，使该账号无缝续用原游标（不做 90 天全量重拉），
并写入 trae_uid / trae_uid_cred（凭据指纹）让 App 启动后直接认领。

用法（先退出 ccBar 再执行）:
  python scripts/migrate_trae_cursor.py <uid>          # uid 从 JWT claims.data.id 取
  python scripts/migrate_trae_cursor.py <uid> --dry    # 只打印将要做的变更

幂等：目标键已存在时不覆盖；trae_uid 已认领时不重写。可安全重复执行。
"""
import json
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.expanduser("~"), ".ccbar", "ccbar.db")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".ccbar", "settings.json")


def parse_credential(text):
    """镜像 trae_sync.parse_credential：返回 (passport Cookie, X-Cloudide-Session)。"""
    raw = (text or "").strip()
    if not raw:
        return ("", "")
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    session_id = None
    cloudide = None
    for part in raw.replace("\r", "\n").replace("\n", ";").split(";"):
        pair = part.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip().lower()
        value = value.strip().strip('"')
        if not value:
            continue
        if name == "sessionid":
            session_id = value
        elif name == "x-cloudide-session":
            cloudide = value
    if session_id is None and cloudide is None:
        return (raw, "") if "=" in raw else ("sessionid=" + raw, "")
    return (("sessionid=" + session_id) if session_id else "", cloudide or "")


def credential_identity(raw):
    """镜像 trae_sync.credential_identity：passport 优先，退而 X-Cloudide-Session。"""
    passport, cloudide = parse_credential(raw)
    return passport if passport else cloudide


def load_trae_credential():
    """从 settings.json 读 Trae 主凭据（uid_cred 指纹用）。"""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            conf = json.load(f)
        return conf.get("trae_sessionid") or ""
    except Exception as e:
        print("! 读取 %s 失败: %s" % (SETTINGS_PATH, e))
        return ""


def get_meta(cur, key):
    row = cur.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def set_meta(cur, key, value):
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print(__doc__)
        print("错误：请传入数字 uid（JWT claims.data.id，如 942709926015804）")
        sys.exit(1)
    uid = sys.argv[1]
    dry = "--dry" in sys.argv
    if not os.path.exists(DB_PATH):
        print("错误：找不到统计库 %s" % DB_PATH)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    changes = []

    # 1) 旧全局游标 → per-uid（目标已存在则不覆盖，保证幂等）
    for base in ("trae_synced_day", "trae_synced_at", "trae_ent"):
        legacy_value = get_meta(cur, base)
        target = "%s_%s" % (base, uid)
        current = get_meta(cur, target)
        if legacy_value and not current:
            changes.append("%s = %s" % (target, str(legacy_value)[:60]))
            if not dry:
                set_meta(cur, target, legacy_value)

    # 2) uid 认领 + 凭据指纹（App 首轮同步即可续用游标）
    if not get_meta(cur, "trae_uid"):
        identity = credential_identity(load_trae_credential())
        changes.append("trae_uid = %s" % uid)
        changes.append("trae_uid_cred = %s" % (identity[:60] or "(空，App 首次同步后自动补)"))
        if not dry:
            set_meta(cur, "trae_uid", uid)
            set_meta(cur, "trae_uid_cred", identity)

    # 3) 删除旧全局余额键（余额改为 per-uid 合计，残留会同账号双算）
    if get_meta(cur, "trae_ent"):
        changes.append("DELETE trae_ent（已搬至 per-uid 键）")
        if not dry:
            cur.execute("DELETE FROM meta WHERE key = 'trae_ent'")

    if dry:
        print("dry-run 变更预览：")
    else:
        con.commit()
        con.close()
        print("迁移完成：" if changes else "无需变更（已迁移过）")
    for c in changes:
        print("  -", c)
    if not dry and changes:
        print("现在可以启动 ccBar 了。")


if __name__ == "__main__":
    main()
