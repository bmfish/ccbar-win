"""Trae 多账号（双槽 + per-uid 游标）单测：uid 解码 / 指纹 / 余额合计 / 永不删行。

全程不碰网络；口径与 macOS 版 MultiAccountTests 对齐。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats_store as stats_mod  # noqa: E402
import trae_sync as ts  # noqa: E402
import trae_client_credentials  # noqa: E402
from stats_store import StatsStore  # noqa: E402
from trae_sync import TraeRow  # noqa: E402


_ORIG_STORAGE_OVERRIDE = None


def setUpModule():
    """隔离本机真实 Trae 客户端存储（开发机有登录态时不能影响槽位断言）"""
    global _ORIG_STORAGE_OVERRIDE
    _ORIG_STORAGE_OVERRIDE = trae_client_credentials.storage_override
    trae_client_credentials.storage_override = []


def tearDownModule():
    trae_client_credentials.storage_override = _ORIG_STORAGE_OVERRIDE


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

    def test_quota_snapshot_uses_per_uid_timestamp_and_name(self):
        self.store._meta_set("trae_uid", "u1")
        self.store._meta_set("trae_ent_u1", "10|100")
        self.store._meta_set("trae_ent_at_u1", 1700000123)
        self.store._meta_set("trae_name_u1", "小明")
        self.store._meta_set("trae_uid_2", "u2")
        self.store._meta_set("trae_ent_u2", "20|200")
        self.store._meta_set("trae_ent_at_2", 1700000456)   # 旧槽位键，须能回落
        snapshots = {s["account"]: s for s in self.store.trae_quota_snapshots()}
        self.assertEqual(snapshots["u1"]["updated_at"], 1700000123)
        self.assertEqual(snapshots["u1"]["name"], "小明")
        self.assertEqual(snapshots["u2"]["updated_at"], 1700000456)
        self.assertEqual(snapshots["u2"]["name"], "")

    def test_reset_quota_cache_clears_ent_name_and_throttle(self):
        self.store._meta_set("trae_ent_u1", "1|2")
        self.store._meta_set("trae_ent_at_u1", 123)
        self.store._meta_set("trae_name_u1", "小明")
        self.store.conn.commit()
        self.assertGreaterEqual(self.store.reset_trae_quota_cache(), 3)
        self.assertEqual(self.store._meta_get("trae_ent_u1"), "")
        self.assertEqual(self.store._meta_get("trae_ent_at_u1"), "")
        self.assertEqual(self.store._meta_get("trae_name_u1"), "")
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


class ClientUidAttributionTests(unittest.TestCase):
    """客户端凭据通道的 uid 归属：账单必须写在 client uid 下，不能被旧 sessionid JWT 串号"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-attr-")
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

    def test_ent_written_under_client_uid(self):
        now = int(time.time())
        client_uid = "942709926015804"
        stale_uid = "3172552512377002"
        acc = trae_client_credentials.ClientTraeAccount(
            client_uid, make_jwt(client_uid), now + 86400,
            device_id="1", source_app="Trae CN")
        # 槽 1 里缓存的旧 sessionid JWT 属于另一个账号：不能拿它当 uid
        self.store._meta_set(
            "trae_auth",
            ts.TraeAuth(jwt=make_jwt(stale_uid), jwt_exp=now + 86400).to_json())
        self.store.conn.commit()
        result = ts.TraeResult("ok", rows=[], consumed=100.0, total=900.0)
        with mock.patch.object(stats_mod.trae_client_credentials, "discover",
                               return_value=[acc]), \
                mock.patch.object(stats_mod, "_fetch_trae_window",
                                  return_value=result):
            self.store.sync_trae_if_needed(interactive=True)
        self.assertEqual(self.store._meta_get("trae_uid"), client_uid)
        self.assertEqual(self.store._meta_get("trae_ent_%s" % client_uid),
                         "100.0|900.0")
        self.assertEqual(self.store._meta_get("trae_ent_%s" % stale_uid), "",
                         "账单不得写到旧 sessionid 账号名下")

    def _sync_slot_with_account(self, slot, acc, result):
        """直连单槽同步（绕过凭据发现），便于构造槽位冲突场景"""
        now = int(time.time())
        with mock.patch.object(stats_mod, "_fetch_trae_window",
                               return_value=result):
            self.store._sync_trae_slot(slot, "", now,
                                       datetime.fromtimestamp(now), True,
                                       client_account=acc)

    def test_same_uid_not_registered_in_two_slots(self):
        now = int(time.time())
        uid = "942709926015804"
        self.store._meta_set("trae_uid", uid)
        self.store._meta_set("trae_uid_cred", "client:" + uid)
        self.store.conn.commit()
        acc = trae_client_credentials.ClientTraeAccount(
            uid, make_jwt(uid), now + 86400, device_id="1", source_app="Trae CN 2")
        self._sync_slot_with_account(
            2, acc, ts.TraeResult("ok", rows=[], consumed=10.0, total=100.0))
        self.assertEqual(self.store._meta_get("trae_uid_2"), "",
                         "同一 uid 不得登记到第二个槽位")

    def test_slot_conflict_keeps_other_slot_ent_intact(self):
        now = int(time.time())
        uid_a = "942709926015804"
        uid_b = "3172552512377002"
        self.store._meta_set("trae_uid", uid_a)
        self.store._meta_set("trae_uid_2", uid_b)
        self.store._meta_set("trae_ent_%s" % uid_b, "1.0|2.0")
        self.store.conn.commit()
        # 第二客户端也登录了 A（与槽 1 同账号）：槽 2 不能改登记，也不能写账单
        acc = trae_client_credentials.ClientTraeAccount(
            uid_a, make_jwt(uid_a), now + 86400, device_id="1", source_app="Trae CN 2")
        self._sync_slot_with_account(
            2, acc, ts.TraeResult("ok", rows=[], consumed=100.0, total=900.0))
        self.assertEqual(self.store._meta_get("trae_uid_2"), uid_b,
                         "槽 2 登记不得被改成与槽 1 相同")
        self.assertEqual(self.store._meta_get("trae_ent_%s" % uid_b), "1.0|2.0",
                         "账单归属不一致时必须跳过写入")

    def test_screen_name_saved_under_uid(self):
        now = int(time.time())
        uid = "942709926015804"
        acc = trae_client_credentials.ClientTraeAccount(
            uid, make_jwt(uid), now + 86400, device_id="1", source_app="Trae CN")
        with mock.patch.object(stats_mod.trae_sync, "fetch_screen_name",
                               return_value="笑语家的小鱼儿"):
            self._sync_slot_with_account(
                1, acc, ts.TraeResult("ok", rows=[], consumed=1.0, total=2.0))
        self.assertEqual(self.store._meta_get("trae_name_%s" % uid),
                         "笑语家的小鱼儿")


if __name__ == "__main__":
    unittest.main()
