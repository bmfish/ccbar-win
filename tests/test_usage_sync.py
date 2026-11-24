"""多设备用量云同步测试（上传水位 / 下载去重 / 换账号清理）。

不访问真实网络：注入假的 HTTP 收发函数，验证与 cc-service 的协议字段和
origin=local/cloud 的边界。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_settings import Settings  # noqa: E402
from stats_store import StatsStore  # noqa: E402
from usage_sync import UsageSync  # noqa: E402

COLUMNS = (
    "source, request_id, app_type, model, input_tokens, output_tokens, "
    "cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_cost_usd, "
    "credits, created_at, request_count, account, origin, updated_at")


class FakeServer:
    def __init__(self):
        self.uploaded = []
        self.quotas = []
        self.download_pages = []
        self.get_count = 0
        self.last_headers = None

    def post(self, url, headers=None, body=None, **_kwargs):
        payload = json.loads(body or "{}")
        if url.endswith("/auth/login"):
            return 200, {}, json.dumps({"token": "test-token"})
        self.last_headers = headers
        if url.endswith("/usage/quota"):
            quotas = payload.get("quotas") or []
            self.quotas.extend(quotas)
            return 200, {}, json.dumps({"received": len(quotas), "saved": len(quotas)})
        events = payload.get("events") or []
        self.uploaded.extend(events)
        return 200, {}, json.dumps({"received": len(events), "saved": len(events)})

    def get(self, url, headers=None, **_kwargs):
        self.get_count += 1
        self.last_headers = headers
        if self.download_pages:
            page = self.download_pages.pop(0)
        else:
            page = {"events": [], "nextSeq": 0}
        return 200, {}, json.dumps(page)


class TestUsageSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-usage-sync-")
        self._orig_path = StatsStore.store_path
        StatsStore.store_path = staticmethod(
            lambda: os.path.join(self.tmp, "ccbar.db"))
        self.store = StatsStore()
        self.store.rebuild([])
        self.settings = Settings(os.path.join(self.tmp, "settings.json"))
        self.settings.apply({
            "usage_sync_enabled": True,
            "usage_sync_url": "http://example.com/api",
            "usage_sync_username": "alice",
            "usage_sync_token": "token-a",
        })
        self.server = FakeServer()
        self.sync = UsageSync(self.store, self.settings,
                              post=self.server.post, get=self.server.get)

    def tearDown(self):
        self.store._close()
        StatsStore.store_path = self._orig_path
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, request_id, created_at, origin="local", source="trae",
                tokens=100, account="u1", credits=1.5):
        self.store.conn.execute(
            "INSERT INTO usage_log (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" % COLUMNS,
            (source, request_id, "trae", "model-a", tokens, tokens,
             0, 0, 0, 0.01, credits, created_at, 1, account, origin, created_at))
        self.store.conn.commit()

    def test_upload_only_local_and_advance_watermark(self):
        old = int(time.time()) - 10 * 86400
        recent = int(time.time()) - 3600
        self._insert("old-local", old)
        self._insert("recent-local", recent)
        self._insert("recent-cloud", recent, origin="cloud")

        result = self.sync.run(force=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.uploaded, 2)
        self.assertEqual([e["requestId"] for e in self.server.uploaded],
                         ["old-local", "recent-local"])
        self.assertEqual(self.server.uploaded[0]["credits"], 1.5)
        self.assertEqual(self.server.last_headers["Authorization"], "Bearer token-a")
        self.assertEqual(
            int(self.store.usage_sync_meta("cloud_usage_watermark_trae")), recent)

        # 第二轮：内容没变，不会再上传；cloud 行始终不上传
        self.sync.run(force=True)
        self.assertEqual(self.server.uploaded[2:], [])

    def test_changed_row_reuploads_by_updated_at(self):
        recent = int(time.time()) - 3600
        self._insert("row", recent)
        self.sync.run(force=True)
        self.assertEqual(len(self.server.uploaded), 1)

        self.store.conn.execute(
            "UPDATE usage_log SET input_tokens=?, updated_at=? WHERE request_id=?",
            (200, int(time.time()), "row"))
        self.store.conn.commit()

        self.sync.run(force=True)
        self.assertEqual(len(self.server.uploaded), 2)
        self.assertEqual(self.server.uploaded[-1]["requestId"], "row")
        self.assertEqual(self.server.uploaded[-1]["inputTokens"], 200)

    def test_uploads_trae_quota_snapshots(self):
        self.store._meta_set("trae_uid", "u1")
        self.store._meta_set("trae_ent_u1", "100|500")
        self.store._meta_set("trae_ent_at", "1700000000")
        self.store._meta_set("trae_uid_2", "u2")
        self.store._meta_set("trae_ent_u2", "50|300")
        self.store._meta_set("trae_ent_at_2", "1700000100")
        self._insert("row", int(time.time()) - 60, account="u1")

        result = self.sync.run(force=True)
        self.assertTrue(result.ok, result.error)
        quotas = {q["account"]: q for q in self.server.quotas}
        self.assertEqual(quotas["u1"]["consumed"], 100.0)
        self.assertEqual(quotas["u1"]["total"], 500.0)
        self.assertEqual(quotas["u1"]["updatedAt"], 1700000000)
        self.assertEqual(quotas["u2"]["slot"], 2)
        self.assertEqual(quotas["u2"]["updatedAt"], 1700000100)

    def test_download_cloud_rows_and_respect_local_priority(self):
        now = int(time.time())
        self._insert("shared", now, tokens=10)
        self.server.download_pages = [
            {"events": [
                {"source": "trae", "requestId": "shared", "appType": "trae",
                 "model": "remote", "inputTokens": 999, "outputTokens": 999,
                 "createdAt": now, "credits": 9, "requestCount": 1, "account": "u1"},
                {"source": "zcode", "requestId": "remote-only", "appType": "zcode",
                 "model": "m", "inputTokens": 5, "outputTokens": 6,
                 "createdAt": now, "credits": 2, "requestCount": 1, "account": ""},
            ], "nextSeq": 7},
        ]

        result = self.sync.run(force=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.pulled, 2)
        rows = {r[0]: r for r in self.store.conn.execute(
            "SELECT request_id, input_tokens, model, origin FROM usage_log").fetchall()}
        self.assertEqual(rows["shared"][1], 10, "本机行必须优先，不能被云端覆盖")
        self.assertEqual(rows["shared"][3], "local")
        self.assertEqual(rows["remote-only"][1], 5)
        self.assertEqual(rows["remote-only"][3], "cloud")
        self.assertEqual(int(self.store.usage_sync_meta("cloud_usage_seq")), 7)

        # 游标已推进：再次同步只发一次空 GET，不会重复拉取
        self.sync.run(force=True)
        self.assertEqual(self.server.get_count, 2)

    def test_switch_user_clears_cloud_rows_and_cursor(self):
        self._insert("cloud-row", int(time.time()), origin="cloud")
        self.store.set_usage_sync_meta("cloud_usage_identity", "alice")
        self.store.set_usage_sync_meta("cloud_usage_seq", "12")
        self.settings.apply({"usage_sync_username": "bob"})

        result = self.sync.run(force=True)
        self.assertTrue(result.ok, result.error)
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE origin='cloud'").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.store.usage_sync_meta("cloud_usage_identity"), "bob")
        self.assertEqual(int(self.store.usage_sync_meta("cloud_usage_seq") or 0), 0)

    def test_login_saves_token_but_never_password(self):
        token, error = self.sync.login_and_save(
            "alice", "secret-password", "http://example.com")
        self.assertEqual(error, "")
        self.assertEqual(token, "test-token")
        self.assertEqual(self.settings.get("usage_sync_token"), "test-token")
        self.assertEqual(self.settings.get("usage_sync_username"), "alice")
        self.assertEqual(self.settings.get("usage_sync_url"), "http://example.com/api")
        self.assertNotIn("secret-password",
                         json.dumps(self.settings.as_dict(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
