"""签到引擎：请求头形态、灰度字段解析、已签到兼容"""
import json
import unittest

import trae_checkin


def _send_returning(payload, status=200):
    text = json.dumps(payload) if not isinstance(payload, str) else payload
    calls = []

    def send(url, headers=None, body=None, timeout=15):
        calls.append((url, headers, body))
        return status, {}, text
    send.calls = calls
    return send


class TestHeaders(unittest.TestCase):
    def test_full_client_shape(self):
        h = trae_checkin.headers("tok", "942709926015804", "450130483890571")
        self.assertEqual(h["authorization"], "Cloud-IDE-JWT tok")
        self.assertEqual(h["x-device-id"], "450130483890571")
        self.assertEqual(h["user-agent"], "VSCode 1.107.1 (TRAE SOLO CN)")
        self.assertEqual(h["x-lscbd-aid"], "787976")
        self.assertEqual(h["x-lscbd-platform"], "windows")
        self.assertEqual(h["package-type"], "stable_cn")
        self.assertEqual(h["x-lgw-req-sdk-type"], "3")
        self.assertEqual(h["app-version"], "0.1.45")
        self.assertEqual(h["x-user-region"], "CN")

    def test_market_uid_deterministic_and_uuid4(self):
        a = trae_checkin.headers("t", "942709926015804", "d")
        b = trae_checkin.headers("t", "942709926015804", "d")
        self.assertEqual(a["x-market-user-id"], b["x-market-user-id"])
        groups = a["x-market-user-id"].split("-")
        self.assertEqual([len(g) for g in groups], [8, 4, 4, 4, 12])
        self.assertEqual(groups[2][0], "4")
        self.assertIn(groups[3][0], "89ab")
        other = trae_checkin.headers("t", "3172552512377002", "d")
        self.assertNotEqual(a["x-market-user-id"], other["x-market-user-id"])
        self.assertEqual(len(a["vscode-sessionid"]), 64)


class TestStatus(unittest.TestCase):
    def test_parse_rewards_and_checked(self):
        st = trae_checkin.status(
            "jwt", "1", "d",
            send=_send_returning({"checked_in": True, "did_checked_in": True,
                                  "credits": 200, "extra_credits": 100,
                                  "enable": True, "code": 0, "message": "success"}))
        self.assertTrue(st.checked_in)
        self.assertTrue(st.enable)
        self.assertEqual(st.reward, 300)

    def test_did_checked_in_is_authoritative(self):
        st = trae_checkin.status(
            "jwt", "1", "d",
            send=_send_returning({"checked_in": False, "did_checked_in": True,
                                  "credits": 200, "extra_credits": 0,
                                  "enable": True, "code": 0}))
        self.assertTrue(st.checked_in, "did_checked_in=true 就是今日已签")

    def test_no_response(self):
        st = trae_checkin.status("jwt", "1", "d", send=lambda *a, **k: (500, {}, ""))
        self.assertFalse(st.checked_in)
        self.assertEqual(st.message, "无响应")


class TestClaim(unittest.TestCase):
    def test_success_has_no_reward_in_body(self):
        cl = trae_checkin.claim(
            "jwt", "1", "d", send=_send_returning({"code": 0, "message": "success"}))
        self.assertTrue(cl.ok)
        self.assertEqual(cl.credits, 0, "claim 响应不带金额，奖励由 status 提供")

    def test_already_checked_is_ok(self):
        cl = trae_checkin.claim(
            "jwt", "1", "d",
            send=_send_returning({"code": 9095, "message": "当前设备今日已经签到，请明日再来哦～"}))
        self.assertTrue(cl.ok)

    def test_auth_rejected_is_not_ok(self):
        cl = trae_checkin.claim(
            "jwt", "1", "d",
            send=_send_returning({"code": 1001, "message": "not able to authenticate"}))
        self.assertFalse(cl.ok)


if __name__ == "__main__":
    unittest.main()
