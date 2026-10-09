"""trae_sync 单测：归一化 / JWT / Set-Cookie / 换签链 / 分页 / 积分账单。

全程注入假 transport，不碰网络；口径与 macOS 版 TraeSyncTests 对齐。
"""
import base64
import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trae_sync as ts  # noqa: E402
from trae_sync import TraeAuth, TraeRow, jwt_exp, jwt_looks_valid, normalize  # noqa: E402

NOW = 1_700_000_000


# ---- 测试脚手架 ----


def make_jwt(exp, sig="sig"):
    """手工拼一个 3 段 JWT（payload 为 base64url 无 padding）"""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "header." + payload + "." + sig


def ok_response(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj))


def usage_page(sessions, total=None, code=None):
    obj = {"user_usage_group_by_sessions": sessions}
    if code is not None:
        obj["code"] = code
    if total is not None:
        obj["total"] = total
    return obj


def sess(sid, usage_time=1_791_460_506, model="GLM-5.3-Flash", input_token=100,
         output_token=10, cache_read=0, cache_write=0, credits=1.0, amount=1.0,
         cost=0.01):
    s = {"session_id": sid, "model_name": model, "usage_time": usage_time,
         "credits_float": credits, "amount_float": amount, "cost_money_float": cost,
         "extra_info": {"input_token": input_token, "output_token": output_token,
                        "cache_read_token": cache_read, "cache_write_token": cache_write}}
    return s


class FakePost:
    """按路径排队返回响应的假 transport；记录每次请求，测试不碰网络。

    同一路径只配一条响应时该响应会一直复用（分页/重试用例靠它）。
    """

    def __init__(self):
        self.calls = []
        self.queues = {}

    def add(self, path, *responses):
        self.queues.setdefault(path, []).extend(responses)
        return self

    def __call__(self, url, cookie=None, auth=None, body=None, timeout=15):
        path = url[len(ts.API_BASE):] if url.startswith(ts.API_BASE) else url
        call = {"url": url, "path": path, "cookie": cookie, "auth": auth,
                "body": body, "json": json.loads(body) if body else None}
        self.calls.append(call)
        queue = self.queues.get(path)
        if not queue:
            raise AssertionError("未预期的请求: %s" % url)
        resp = queue[0] if len(queue) == 1 else queue.pop(0)
        return resp(call) if callable(resp) else resp

    def paths(self):
        return [c["path"] for c in self.calls]


def signed_in(**kw):
    """一个已缓存可用 JWT + X-Cloudide-Session 的凭据"""
    exp = kw.pop("exp", NOW + 3600)
    jwt = kw.pop("jwt", make_jwt(exp))
    return TraeAuth(cloudide_session=kw.pop("cloudide_session", "cached-sess"),
                    jwt=jwt, jwt_exp=exp)


# ---- normalize：净输入换算与字段口径 ----


class TestNormalize(unittest.TestCase):
    def test_maps_tokens_credits_and_request_id(self):
        # 与 macOS 版 testNormalizeMapsTokensAndCredits 同一份数据
        payload = {"user_usage_group_by_sessions": [{
            "session_id": "abc123", "model_name": "GLM-5.3-Flash", "usage_time": 1791460506,
            "amount_float": 0.9128, "credits_float": 0.9128, "cost_money_float": 0.02282,
            "extra_info": {"input_token": 142064, "output_token": 3710,
                           "cache_read_token": 137536, "cache_write_token": 0}}]}
        rows = normalize(payload)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # Trae 的 input_token 是总量口径（含缓存），入库折算为净输入
        self.assertEqual(r.input, 142064 - 137536)
        self.assertEqual(r.output, 3710)
        self.assertEqual(r.cache_read, 137536)
        self.assertEqual(r.cache_write, 0)
        self.assertAlmostEqual(r.credits, 0.9128)
        self.assertAlmostEqual(r.cost_usd, 0.02282)
        self.assertEqual(r.model, "GLM-5.3-Flash")
        self.assertEqual(r.epoch, 1791460506)
        self.assertEqual(r.request_id, "trae|abc123")

    def test_net_input_clamped_when_cached_exceeds_gross(self):
        rows = normalize([sess("s", input_token=100, cache_read=300, cache_write=5)])
        self.assertEqual(rows[0].input, 0)
        self.assertEqual(rows[0].cache_read, 300)
        self.assertEqual(rows[0].cache_write, 5)

    def test_skips_missing_session_id_and_bad_time(self):
        rows = normalize([
            {"usage_time": 10},                            # 无 session_id
            {"session_id": "", "usage_time": 10},          # 空 session_id
            {"session_id": "a"},                           # 无 usage_time
            {"session_id": "a", "usage_time": 0},          # 零点/缺失
            {"session_id": "a", "usage_time": -5},         # 负数
            {"session_id": "keep", "usage_time": 7},       # 唯一合法行
        ])
        self.assertEqual([r.session_id for r in rows], ["keep"])

    def test_credits_fallback_to_amount_then_zero(self):
        # credits_float 缺失 → amount_float
        rows = normalize([{"session_id": "a", "usage_time": 10, "amount_float": 1.5}])
        self.assertAlmostEqual(rows[0].credits, 1.5)
        # 两者都缺 → 0
        rows = normalize([{"session_id": "a", "usage_time": 10}])
        self.assertEqual(rows[0].credits, 0)
        # credits_float 显式为 0 时不得回落 amount（区分 None 与 0）
        rows = normalize([{"session_id": "a", "usage_time": 10,
                           "credits_float": 0, "amount_float": 9.0}])
        self.assertEqual(rows[0].credits, 0)

    def test_defaults_model_and_cost(self):
        rows = normalize([{"session_id": "a", "usage_time": 10}])
        self.assertEqual(rows[0].model, "unknown")
        self.assertEqual(rows[0].cost_usd, 0)
        self.assertEqual(rows[0].output, 0)
        self.assertEqual(rows[0].cache_read, 0)

    def test_one_row_per_session_ignores_group_details(self):
        s = sess("abc", input_token=10, output_token=2)
        s["usage_group_details"] = [
            {"model_display_name": "m1", "credits_float": 0.5, "cost_money_float": 0.01},
            {"model_display_name": "m2", "credits_float": 0.5, "cost_money_float": 0.01},
        ]
        rows = normalize({"user_usage_group_by_sessions": [s]})
        self.assertEqual(len(rows), 1, "usage_group_details 不拆行")

    def test_multiple_sessions_and_tolerant_input(self):
        rows = normalize([sess("s1"), sess("s2"), sess("s3")])
        self.assertEqual([r.session_id for r in rows], ["s1", "s2", "s3"])
        self.assertEqual(normalize(None), [])
        self.assertEqual(normalize({}), [])
        self.assertEqual(normalize("nonsense"), [])

    def test_row_equality_repr(self):
        a = TraeRow("s", "m", 1, 2, 3, 4, 0.5, 1.5, 99)
        b = TraeRow("s", "m", 1, 2, 3, 4, 0.5, 1.5, 99)
        self.assertEqual(a, b)
        self.assertIn("trae|s", repr(a) + a.request_id)


# ---- JWT ----


class TestJwt(unittest.TestCase):
    def test_jwt_exp_parsing_without_padding(self):
        # {"exp":1700000000} 的 base64url（去掉 padding）
        payload = base64.urlsafe_b64encode(b'{"exp":1700000000}').decode().rstrip("=")
        self.assertEqual(jwt_exp("header." + payload + ".signature"), 1_700_000_000)
        self.assertEqual(jwt_exp(make_jwt(1_791_489_157)), 1_791_489_157)

    def test_jwt_exp_garbage_returns_zero(self):
        self.assertEqual(jwt_exp("not-a-jwt"), 0)
        self.assertEqual(jwt_exp(""), 0)
        self.assertEqual(jwt_exp(None), 0)
        self.assertEqual(jwt_exp("header.!!!.sig"), 0)
        b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        self.assertEqual(jwt_exp("h." + b64({"sub": "x"}) + ".s"), 0, "无 exp 字段")
        self.assertEqual(jwt_exp("h." + b64(["exp"]) + ".s"), 0, "payload 非对象")

    def test_jwt_looks_valid_needs_three_segments(self):
        self.assertTrue(jwt_looks_valid(make_jwt(NOW)))
        self.assertFalse(jwt_looks_valid("header.payload"))
        self.assertFalse(jwt_looks_valid(""))
        self.assertFalse(jwt_looks_valid(None))
        self.assertFalse(jwt_looks_valid("h.p.s.x"))
        # 空段被丢弃（对齐 Swift split 的默认行为）
        self.assertTrue(jwt_looks_valid("h.p.s."))


# ---- Set-Cookie 提取 ----


class TestExtractSessionCookie(unittest.TestCase):
    def test_comma_joined_header_from_urlsession(self):
        merged = ("sid_guard=x%7C1; Path=/, "
                  "X-Cloudide-Session=KC_lA83=.18dc8d30b30; Path=/; HttpOnly, "
                  "ttwid=1%7Cabc")
        self.assertEqual(ts.extract_session_cookie(merged), "KC_lA83=.18dc8d30b30")

    def test_extra_attributes_and_expires_comma(self):
        merged = ("lang=zh; Expires=Wed, 21 Oct 2026 07:28:00 GMT, "
                  "X-Cloudide-Session=abc.def.123; Path=/; HttpOnly; SameSite=Lax, "
                  "other=1")
        self.assertEqual(ts.extract_session_cookie(merged), "abc.def.123")

    def test_session_cookie_last_without_attributes(self):
        self.assertEqual(ts.extract_session_cookie("a=1, X-Cloudide-Session=xyz"),
                         "xyz")

    def test_missing_returns_none(self):
        self.assertIsNone(ts.extract_session_cookie("other=1; Path=/"))
        self.assertIsNone(ts.extract_session_cookie(""))
        self.assertIsNone(ts.extract_session_cookie(None))
        self.assertIsNone(ts.extract_session_cookie("X-Cloudide-Session=; Path=/"))

    def test_accepts_headers_dict(self):
        self.assertEqual(
            ts.extract_session_cookie({"Set-Cookie": "a=1, X-Cloudide-Session=from-dict; Path=/"}),
            "from-dict")
        self.assertEqual(
            ts.extract_session_cookie({"set-cookie": "X-Cloudide-Session=lower; Path=/"}),
            "lower")
        self.assertIsNone(ts.extract_session_cookie({"Content-Type": "application/json"}))


# ---- 凭据序列化与可用性 ----


class TestTraeAuth(unittest.TestCase):
    def test_json_round_trip(self):
        auth = TraeAuth(cloudide_session="sess=.123", jwt=make_jwt(1_791_489_157),
                        jwt_exp=1_791_489_157)
        back = TraeAuth.from_json(auth.to_json())
        self.assertEqual(back, auth)
        self.assertEqual(back.cloudide_session, "sess=.123")
        self.assertEqual(back.jwt_exp, 1_791_489_157)

    def test_from_json_tolerates_bad_input(self):
        self.assertIsNone(TraeAuth.from_json(""))
        self.assertIsNone(TraeAuth.from_json(None))
        self.assertIsNone(TraeAuth.from_json("{bad json"))
        self.assertIsNone(TraeAuth.from_json("[]"))
        # 空对象 → 空凭据而非 None
        self.assertEqual(TraeAuth.from_json("{}"), TraeAuth())

    def test_is_jwt_usable_exp_boundary(self):
        # exp == now + 120 不算可用（要求严格大于）
        self.assertFalse(TraeAuth(jwt=make_jwt(NOW + 120), jwt_exp=NOW + 120)
                         .is_jwt_usable(NOW))
        self.assertTrue(TraeAuth(jwt=make_jwt(NOW + 121), jwt_exp=NOW + 121)
                        .is_jwt_usable(NOW))
        self.assertFalse(TraeAuth().is_jwt_usable(NOW), "空 JWT 不可用")
        self.assertFalse(TraeAuth(jwt="h.p", jwt_exp=NOW + 99999).is_jwt_usable(NOW),
                         "两段伪 JWT 不可用")
        self.assertFalse(TraeAuth(jwt=make_jwt(NOW + 99999), jwt_exp=NOW - 1)
                         .is_jwt_usable(NOW), "缓存的 exp 过期即不可用")

    def test_result_is_ok(self):
        r = ts.TraeResult("ok", rows=[], consumed=1.0, total=2.0)
        self.assertTrue(r.is_ok)
        self.assertFalse(ts.TraeResult("failed", message="x").is_ok)


# ---- run：换签链与分页 ----


class TestRun(unittest.TestCase):
    def test_not_configured_makes_no_http_call(self):
        for sid in ("", None, "   "):
            fake = FakePost()
            res = ts.run(sid, post=fake, now=NOW)
            self.assertEqual(res.status, "not_configured")
            self.assertEqual(fake.calls, [], "空凭据不得发起任何请求")

    def test_auth_expired_only_when_login_fails(self):
        # 无缓存 Session/JWT → 必须先 Login；Login 非 200
        fake = FakePost().add(ts.LOGIN_PATH, (403, {}, "forbidden"))
        auth = TraeAuth()
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)
        self.assertEqual(res.status, "auth_expired")
        self.assertEqual(fake.paths(), [ts.LOGIN_PATH])
        self.assertEqual(fake.calls[0]["cookie"], "sessionid=x")

    def test_auth_expired_when_login_has_no_session_cookie(self):
        fake = FakePost().add(ts.LOGIN_PATH, ok_response({"ok": True},
                                                         headers={"Set-Cookie": "other=1"}))
        res = ts.run("sessionid=x", post=fake, now=NOW)
        self.assertEqual(res.status, "auth_expired")
        self.assertEqual(fake.paths(), [ts.LOGIN_PATH])

    def test_full_chain_login_token_usage(self):
        exp = NOW + 8 * 3600
        token = make_jwt(exp)
        fake = (FakePost()
                .add(ts.LOGIN_PATH, (200, {"Set-Cookie": "X-Cloudide-Session=s1; Path=/; HttpOnly"}, ""))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": token}}))
                .add(ts.USAGE_PATH, ok_response(usage_page([sess("s1")], total=1))))
        auth = TraeAuth()
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)

        self.assertEqual(res.status, "ok")
        self.assertEqual([r.session_id for r in res.rows], ["s1"])
        self.assertEqual(fake.paths(), [ts.LOGIN_PATH, ts.TOKEN_PATH, ts.USAGE_PATH])
        self.assertEqual(fake.calls[1]["cookie"], "X-Cloudide-Session=s1")
        self.assertEqual(fake.calls[2]["auth"], "Cloud-IDE-JWT " + token)
        # 换签结果回写 auth
        self.assertEqual(auth.cloudide_session, "s1")
        self.assertEqual(auth.jwt, token)
        self.assertEqual(auth.jwt_exp, exp)

    def test_cached_jwt_reused_without_resign(self):
        fake = FakePost().add(ts.USAGE_PATH, ok_response(usage_page([sess("s1")], total=1)))
        auth = signed_in()
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)
        self.assertEqual(res.status, "ok")
        self.assertEqual(fake.paths(), [ts.USAGE_PATH], "可用 JWT 不应再换签")

    def test_expired_jwt_resigns_with_cached_session(self):
        token = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": token}}))
                .add(ts.USAGE_PATH, ok_response(usage_page([sess("s1")], total=1))))
        auth = TraeAuth(cloudide_session="cached", jwt="expired.token.x", jwt_exp=NOW - 1)
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)
        self.assertEqual(res.status, "ok")
        self.assertEqual(fake.paths(), [ts.TOKEN_PATH, ts.USAGE_PATH])
        self.assertNotIn(ts.LOGIN_PATH, fake.paths(), "Session 还活着就不该重新 Login")
        self.assertEqual(auth.jwt, token)

    def test_usage_1001_forces_resign_and_retries_once(self):
        """v1.8.1 回归：usage 返回 1001 时强制重签再试，不得误报登录过期"""
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.USAGE_PATH, ok_response({"code": 1001}),
                     ok_response(usage_page([sess("s1")], total=1)))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": fresh}})))
        auth = signed_in(cloudide_session="cached-sess")
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)

        self.assertEqual(res.status, "ok", "一次 1001 不该算登录过期")
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(fake.paths(), [ts.USAGE_PATH, ts.TOKEN_PATH, ts.USAGE_PATH])
        self.assertNotIn(ts.LOGIN_PATH, fake.paths())
        self.assertEqual(auth.jwt, fresh)
        self.assertEqual(auth.cloudide_session, "cached-sess")

    def test_usage_1001_triggers_full_login_when_session_dead(self):
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.USAGE_PATH, ok_response({"code": 1001}),
                     ok_response(usage_page([sess("s1")], total=1)))
                .add(ts.TOKEN_PATH, ok_response({"Result": {}}),      # 旧 Session 失效
                     ok_response({"Result": {"Token": fresh}}))
                .add(ts.LOGIN_PATH, (200, {"Set-Cookie": "X-Cloudide-Session=new-sess; Path=/"}, "")))
        auth = signed_in(cloudide_session="dead-sess")
        res = ts.run("sessionid=x", auth=auth, post=fake, now=NOW)

        self.assertEqual(res.status, "ok")
        self.assertEqual(fake.paths(),
                         [ts.USAGE_PATH, ts.TOKEN_PATH, ts.LOGIN_PATH, ts.TOKEN_PATH, ts.USAGE_PATH])
        self.assertEqual(auth.cloudide_session, "new-sess")
        self.assertEqual(auth.jwt, fresh)

    def test_usage_1001_twice_is_auth_expired(self):
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.USAGE_PATH, ok_response({"code": 1001}))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": fresh}})))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "auth_expired")
        self.assertEqual(res.rows, [])

    def test_other_api_code_fails_with_code_in_message(self):
        fake = FakePost().add(ts.USAGE_PATH, ok_response({"code": 9004}))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "failed")
        self.assertIn("9004", res.message)
        self.assertEqual(res.rows, [])

    def test_transport_error_is_failed(self):
        fake = FakePost().add(ts.USAGE_PATH, (0, None, None))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.rows, [])

    def test_usage_body_shape_and_window(self):
        fake = FakePost().add(ts.USAGE_PATH, ok_response(usage_page([], total=0)))
        ts.run("sessionid=x", auth=signed_in(), from_epoch=100, post=fake, now=NOW)
        body = fake.calls[0]["json"]
        self.assertEqual(body["start_time"], 100)
        self.assertEqual(body["end_time"], NOW)
        self.assertEqual(body["page_size"], ts.PAGE_SIZE)
        self.assertEqual(body["page_num"], 1)
        self.assertEqual(body["usage_type"], [7])

    def test_pagination_two_pages_stops_on_empty_page(self):
        fake = (FakePost()
                .add(ts.USAGE_PATH,
                     ok_response(usage_page([sess("s1"), sess("s2")], total=99)),
                     ok_response(usage_page([], total=99))))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "ok")
        self.assertEqual([r.session_id for r in res.rows], ["s1", "s2"])
        self.assertEqual([c["json"]["page_num"] for c in fake.calls], [1, 2])

    def test_pagination_stops_at_total(self):
        fake = FakePost().add(ts.USAGE_PATH, ok_response(usage_page([sess("s1"), sess("s2")], total=2)))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(len(res.rows), 2)
        self.assertEqual(len(fake.calls), 1, "已达 total 就不该再翻页")

    def test_max_pages_returns_what_it_collected(self):
        def grow(call):
            n = call["json"]["page_num"]
            return ok_response(usage_page([sess("s%d" % n, usage_time=n)], total=9999))

        fake = FakePost().add(ts.USAGE_PATH, grow)
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "ok")
        self.assertEqual(len(res.rows), ts.MAX_PAGES)
        self.assertEqual(len(fake.calls), ts.MAX_PAGES)
        self.assertEqual(fake.calls[-1]["json"]["page_num"], ts.MAX_PAGES)

    def test_entitlements_only_fetched_when_requested(self):
        session_page = ok_response(usage_page([sess("s1")], total=1))

        fake = FakePost().add(ts.USAGE_PATH, session_page)
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW, fetch_ent=False)
        self.assertEqual(res.status, "ok")
        self.assertNotIn(ts.ENT_USAGE_PATH, fake.paths())
        self.assertIsNone(res.consumed)
        self.assertIsNone(res.total)

        fake = (FakePost()
                .add(ts.USAGE_PATH, session_page)
                .add(ts.ENT_USAGE_PATH, ok_response(
                    {"code": 0, "usage_summary": {"consumed_amount": 12.5, "total_amount": 100.0}})))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW, fetch_ent=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(fake.paths()[-1], ts.ENT_USAGE_PATH)
        self.assertEqual(fake.calls[-1]["json"], {"require_usage": True})
        self.assertAlmostEqual(res.consumed, 12.5)
        self.assertAlmostEqual(res.total, 100.0)

    def test_entitlement_failure_is_not_fatal(self):
        fake = (FakePost()
                .add(ts.USAGE_PATH, ok_response(usage_page([sess("s1")], total=1)))
                .add(ts.ENT_USAGE_PATH, (0, None, None)))
        res = ts.run("sessionid=x", auth=signed_in(), post=fake, now=NOW, fetch_ent=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(len(res.rows), 1)
        self.assertIsNone(res.consumed)
        self.assertIsNone(res.total)

    def test_default_transport_is_used_when_post_not_injected(self):
        self.assertTrue(callable(ts.http_post))
        self.assertEqual(ts.http_post.__defaults__[-1], 15)


# ---- 每日签到 ----


class TestCheckinPlan(unittest.TestCase):
    def test_pure_decision_table(self):
        self.assertEqual(ts.checkin_plan(False, False), "disabled")
        self.assertEqual(ts.checkin_plan(False, True), "disabled", "未开启优先于已签")
        self.assertEqual(ts.checkin_plan(True, True), "already")
        self.assertEqual(ts.checkin_plan(True, False), "claim")
        # 字段缺失（None）按"能签"处理，与 macOS 版 Bool? 语义一致
        self.assertEqual(ts.checkin_plan(None, None), "claim")
        self.assertEqual(ts.checkin_plan(None, True), "already")


class TestCheckin(unittest.TestCase):
    def test_not_configured_makes_no_http_call(self):
        for sid in ("", None, "   "):
            fake = FakePost()
            res = ts.checkin(sid, post=fake, now=NOW)
            self.assertEqual(res.status, "not_configured")
            self.assertFalse(res.is_claimed)
            self.assertEqual(fake.calls, [], "空凭据不得发起任何请求")

    def test_claim_success_sums_base_and_bonus_credits(self):
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"code": 0, "enable": True,
                                                          "checked_in": False}))
                .add(ts.CHECKIN_CLAIM_PATH, ok_response({"code": 0, "credits": 100.0,
                                                         "extra_credits": 50.0})))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "claimed")
        self.assertAlmostEqual(res.credits, 150.0)
        self.assertTrue(res.is_claimed)
        self.assertEqual(fake.paths(), [ts.CHECKIN_STATUS_PATH, ts.CHECKIN_CLAIM_PATH])
        self.assertEqual(fake.calls[0]["json"], {}, "status 请求体为空对象")
        self.assertEqual(fake.calls[1]["json"], {"req_source": 1}, "claim 带 req_source")
        self.assertEqual(fake.calls[0]["auth"], "Cloud-IDE-JWT " + signed_in().jwt)

    def test_claim_credits_default_to_zero_when_missing(self):
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"checked_in": False}))
                .add(ts.CHECKIN_CLAIM_PATH, ok_response({"code": 0})))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "claimed")
        self.assertEqual(res.credits, 0.0)

    def test_already_checked_in_skips_claim(self):
        fake = FakePost().add(ts.CHECKIN_STATUS_PATH,
                              ok_response({"enable": True, "checked_in": True}))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "already")
        self.assertEqual(fake.paths(), [ts.CHECKIN_STATUS_PATH], "已签不该再领")
        # enable 缺失但 checked_in=true 同样算已签
        fake2 = FakePost().add(ts.CHECKIN_STATUS_PATH, ok_response({"checked_in": True}))
        self.assertEqual(ts.checkin("sessionid=x", auth=signed_in(),
                                    post=fake2, now=NOW).status, "already")

    def test_disabled_account_skips_claim(self):
        fake = FakePost().add(ts.CHECKIN_STATUS_PATH,
                              ok_response({"enable": False, "checked_in": False}))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "disabled")
        self.assertEqual(fake.paths(), [ts.CHECKIN_STATUS_PATH])

    def test_claim_api_code_fails_with_message(self):
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"checked_in": False}))
                .add(ts.CHECKIN_CLAIM_PATH, ok_response({"code": 9004, "message": "参数不对"})))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "failed")
        self.assertIn("9004", res.message)
        self.assertIn("参数不对", res.message)
        self.assertEqual(res.credits, 0.0)

    def test_status_1001_forces_resign_and_retries_once(self):
        """缓存的 JWT 被提前作废（exp 未到但 1001）：强制换签重试，不得误报登录过期"""
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"code": 1001}),
                     ok_response({"enable": True, "checked_in": False}))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": fresh}}))
                .add(ts.CHECKIN_CLAIM_PATH, ok_response({"code": 0, "credits": 100.0})))
        auth = signed_in(cloudide_session="cached-sess")
        res = ts.checkin("sessionid=x", auth=auth, post=fake, now=NOW)

        self.assertEqual(res.status, "claimed")
        self.assertAlmostEqual(res.credits, 100.0)
        self.assertEqual(fake.paths(), [ts.CHECKIN_STATUS_PATH, ts.TOKEN_PATH,
                                        ts.CHECKIN_STATUS_PATH, ts.CHECKIN_CLAIM_PATH])
        self.assertNotIn(ts.LOGIN_PATH, fake.paths(), "Session 还活着就不该重新 Login")
        self.assertEqual(auth.jwt, fresh)
        self.assertEqual(auth.cloudide_session, "cached-sess")

    def test_claim_1001_also_resigns(self):
        """claim 阶段 1001 同样走一次换签重试"""
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"checked_in": False}))
                .add(ts.CHECKIN_CLAIM_PATH, ok_response({"code": 1001}),
                     ok_response({"code": 0, "credits": 100.0}))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": fresh}})))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "claimed")
        self.assertEqual(fake.paths(), [ts.CHECKIN_STATUS_PATH, ts.CHECKIN_CLAIM_PATH,
                                        ts.TOKEN_PATH, ts.CHECKIN_CLAIM_PATH])

    def test_1001_twice_is_auth_expired(self):
        fresh = make_jwt(NOW + 8 * 3600)
        fake = (FakePost()
                .add(ts.CHECKIN_STATUS_PATH, ok_response({"code": 1001}))
                .add(ts.TOKEN_PATH, ok_response({"Result": {"Token": fresh}})))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "auth_expired")
        self.assertIn("登录已过期", res.message)

    def test_login_failure_is_auth_expired(self):
        fake = FakePost().add(ts.LOGIN_PATH, (403, {}, "forbidden"))
        res = ts.checkin("sessionid=x", auth=TraeAuth(), post=fake, now=NOW)
        self.assertEqual(res.status, "auth_expired")
        self.assertEqual(fake.paths(), [ts.LOGIN_PATH])

    def test_transport_and_parse_errors_are_failed(self):
        fake = FakePost().add(ts.CHECKIN_STATUS_PATH, (0, None, None))
        res = ts.checkin("sessionid=x", auth=signed_in(), post=fake, now=NOW)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.message, "无响应")

        fake = FakePost().add(ts.CHECKIN_STATUS_PATH, ("200", {}, "not json"))
        self.assertEqual(ts.checkin("sessionid=x", auth=signed_in(),
                                    post=fake, now=NOW).status, "failed")

    def test_result_repr_and_is_claimed(self):
        r = ts.TraeCheckinResult("claimed", credits=100.0)
        self.assertTrue(r.is_claimed)
        self.assertIn("claimed", repr(r))
        self.assertFalse(ts.TraeCheckinResult("already").is_claimed)


# ---- 夜间静默 ----


class TestNightSilent(unittest.TestCase):
    def test_hours_0_to_8_are_silent_and_9_to_23_are_not(self):
        for hour in range(24):
            dt = datetime(2026, 1, 15, hour, 0)
            self.assertEqual(ts.is_night_silent(dt), hour < 9,
                             "hour=%d 的静默判定不符（0–8 静默）" % hour)

    def test_accepts_epoch_and_none(self):
        dt = datetime(2026, 1, 15, 3, 0)
        self.assertTrue(ts.is_night_silent(dt.timestamp()))
        self.assertFalse(ts.is_night_silent(datetime(2026, 1, 15, 12, 0).timestamp()))
        self.assertIn(ts.is_night_silent(), (True, False))

    def test_constants_match_macos(self):
        self.assertEqual((ts.FIRST_FETCH_DAYS, ts.PAGE_LOOKBACK_DAYS), (90, 2))
        self.assertEqual((ts.PAGE_SIZE, ts.MAX_PAGES), (20, 50))
        self.assertEqual((ts.BG_MIN_INTERVAL, ts.INTERACTIVE_MIN_INTERVAL), (900, 60))
        self.assertEqual((ts.ENT_MIN_INTERVAL, ts.NIGHT_SILENT_UNTIL_HOUR), (3600, 9))
        self.assertEqual(ts.CHECKIN_RETRY_INTERVAL, 3600)
        self.assertEqual(ts.CHECKIN_STATUS_PATH, "/trae/api/v2/ug/checkin_credits/status")
        self.assertEqual(ts.CHECKIN_CLAIM_PATH, "/trae/api/v2/ug/checkin_credits/claim")
        self.assertEqual(ts.API_BASE, "https://api.trae.cn")


if __name__ == "__main__":
    unittest.main()
