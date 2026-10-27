"""Trae 多账号（双槽 + per-uid 游标）单测：uid 解码 / 指纹 / 余额合计 / 永不删行。

全程不碰网络；口径与 macOS 版 MultiAccountTests 对齐。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trae_sync as ts  # noqa: E402
from stats_store import StatsStore  # noqa: E402
from trae_sync import TraeRow  # noqa: E402


def make_jwt(uid, numeric=False):
    """手工拼一个 3 段 JWT（payload 为 base64url 无 padding）"""
    body = {"id": uid if numeric else str(uid), "source": "session", "type": "user"}
    payload = base64.urlsafe_b64encode(
        json.dumps({"data": body, "exp": 1_799_999_999, "iat": 1_799_990_000}).encode()
    ).decode().rstrip("=")
    return "header." + payload + ".sig"


class JwtUidTests(unittest.TestCase):
    def test_decodes_string_id(self):
        self.assertEqual(ts.jwt_uid(make_jwt("942709926015804")), "942709926015804")

    def test_normalizes_numeric_id(self):
        self.assertEqual(ts.jwt_uid(make_jwt(123456, numeric=True)), "123456")

    def test_rejects_garbage(self):
        self.assertEqual(ts.jwt_uid(""), "")
        self.assertEqual(ts.jwt_uid("not-a-jwt"), "")
        self.assertEqual(ts.jwt_uid("a.###.c"), "")
        self.assertEqual(ts.jwt_uid("a." + base64.urlsafe_b64encode(b"{}").decode() + ".c"), "")


class CredentialIdentityTests(unittest.TestCase):
    def test_bare_equals_prefixed_equals_full_cookie(self):
        bare = ts.credential_identity("c57b3c1dbc326bfa")
        prefixed = ts.credential_identity("sessionid=c57b3c1dbc326bfa")
        full = ts.credential_identity(
            "is_staff_user=false; sessionid=c57b3c1dbc326bfa; uid_tt=abc; "
            "X-Cloudide-Session=xyz=.18e2")
        self.assertEqual(bare, prefixed)
        self.assertEqual(bare, full)

    def test_differs_across_accounts(self):
        self.assertNotEqual(ts.credential_identity("sessionid=aaa"),
                            ts.credential_identity("sessionid=bbb"))

    def test_falls_back_to_cloudide_session(self):
        self.assertEqual(ts.credential_identity("X-Cloudide-Session=abc=.18e2"), "abc=.18e2")


class MultiAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-multi-")
        self._orig_store_path = StatsStore.store_path
        StatsStore.store_path = staticmethod(
            lambda: os.path.join(self.tmp, "ccbar.db"))
        self.store = StatsStore()
        self.store.rebuild(configs=[],
                           trae_enabled=True, trae_sessionid="sessionid=old")

    def tearDown(self):
        self.store._close()
        StatsStore.store_path = self._orig_store_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, session_id="s1", credits=2.5):
        row = TraeRow(session_id=session_id, model="m", input=10, output=5,
                      cache_read=0, cache_write=0, cost_usd=0.1, credits=credits,
                      epoch=0)
        self.store.upsert_trae_rows([row])

    def test_ent_summary_sums_multiple_uid_keys(self):
        self.store._meta_set("trae_ent_111", "100|1000")
        self.store._meta_set("trae_ent_222", "50.5|500.5")
        self.store._meta_set("trae_ent_at", 1793110905)      # 节流时间戳，必须跳过
        self.store._meta_set("trae_ent_at_2", 1793110906)
        summary = self.store.trae_ent_summary()
        self.assertAlmostEqual(summary[0], 150.5)
        self.assertAlmostEqual(summary[1], 1500.5)

    def test_ent_summary_prefers_per_uid_over_legacy(self):
        # 迁移脚本未跑时可能同时存在旧全局键与 per-uid 键（同账号），此时忽略旧键防双算
        self.store._meta_set("trae_ent", "6441.38|9450.0")
        self.store._meta_set("trae_ent_942709926015804", "10|100")
        summary = self.store.trae_ent_summary()
        self.assertAlmostEqual(summary[0], 10)
        self.assertAlmostEqual(summary[1], 100)

    def test_ent_summary_falls_back_to_legacy_key(self):
        self.store._meta_set("trae_ent", "6441.38|9450.0")
        summary = self.store.trae_ent_summary()
        self.assertAlmostEqual(summary[0], 6441.38)
        self.assertAlmostEqual(summary[1], 9450.0)

    def test_ent_summary_none_when_empty(self):
        self.assertIsNone(self.store.trae_ent_summary())

    def test_credential_change_keeps_rows_and_clears_auth(self):
        self._insert(credits=2.5)
        self.store.rebuild(configs=[],
                           trae_enabled=True, trae_sessionid="sessionid=new")
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE source='trae'").fetchone()[0]
        self.assertEqual(count, 1, "换凭据后旧账号明细应保留（永不删行）")
        self.assertEqual(self.store._meta_get("trae_auth"), "",
                         "换凭据应清 JWT 缓存，防止旧 JWT 拉旧账号")

    def test_credential_format_change_keeps_auth(self):
        # 同一凭据换个粘贴格式不应清 JWT 缓存（避免多余换签请求）
        self.store._meta_set("trae_auth", "keep-me")
        self.store.conn.commit()
        self.store.rebuild(configs=[],
                           trae_enabled=True, trae_sessionid="old")
        self.assertEqual(self.store._meta_get("trae_auth"), "keep-me")


if __name__ == "__main__":
    unittest.main()
