"""workbuddy_sync 单测（与 test_trae_sync 同款：注入假 transport，完全不碰网络）"""
import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_common  # noqa: E402
import workbuddy_sync as wb  # noqa: E402


def _response(items, total=None, code=0, msg="OK"):
    """构造接口响应体（结构照抄真实返回）"""
    return json.dumps({
        "code": code,
        "msg": msg,
        "requestId": "srv-trace-id",
        "data": {"total": len(items) if total is None else total, "data": items},
    })


def _item(request_id="r1", credit=3.54, model="deepseek-v4.1-flash",
          client="CodeBuddyIDE", time_text="2026-10-16 16:35:00"):
    return {"requestId": request_id, "credit": credit, "model": model,
            "client": client, "requestTime": time_text,
            "inputTrunc": "-", "input": "-", "agentPurpose": ""}


COOKIE = "session=9Qw9C1|1792744353|sig; session_2=abc|def"


class FakeSend:
    """记录每次请求，按队列吐响应"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, body=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {},
                           "body": json.loads(body or "{}")})
        if not self.responses:
            return (200, {}, _response([]))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _ok(items, total=None, headers=None):
    return (200, headers or {}, _response(items, total=total))


# ---- 归一化 ----


class TestNormalize(unittest.TestCase):
    def test_full_response(self):
        rows = wb.normalize(json.loads(_response([_item()])))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.request_id, "r1")
        self.assertEqual(row.client, "CodeBuddyIDE")
        self.assertEqual(row.model, "deepseek-v4.1-flash")
        self.assertAlmostEqual(row.credit, 3.54, places=6)
        self.assertEqual(row.epoch,
                         int(datetime(2026, 10, 16, 16, 35).timestamp()))
        self.assertEqual(row.key, "workbuddy|r1")

    def test_accepts_bare_list(self):
        """payload 直接是列表也认（接口微调时的兜底）"""
        self.assertEqual(len(wb.normalize([_item()])), 1)

    def test_skips_unusable_rows(self):
        payload = json.loads(_response([
            _item(request_id=""),                       # 无 requestId
            _item(request_id="r2", time_text="-"),      # 时间不可解析
            _item(request_id="r3", time_text=""),       # 空时间
            {"requestId": "r4", "requestTime": "2026-10-16 10:00:00"},  # 缺 credit
        ]))
        rows = wb.normalize(payload)
        self.assertEqual([r.request_id for r in rows], ["r4"])
        self.assertEqual(rows[0].credit, 0.0)

    def test_fallbacks_for_client_and_model(self):
        rows = wb.normalize([{"requestId": "r", "requestTime": "2026-10-16 10:00:00",
                              "credit": "1.5", "model": "", "client": None}])
        self.assertEqual(rows[0].client, "未知客户端")
        self.assertEqual(rows[0].model, "unknown")
        self.assertAlmostEqual(rows[0].credit, 1.5)

    def test_garbage_payloads(self):
        for payload in (None, {}, {"data": None}, {"data": {"data": "x"}}, "junk"):
            self.assertEqual(wb.normalize(payload), [])


class TestTimeParsing(unittest.TestCase):
    def test_formats(self):
        base = int(datetime(2026, 10, 16, 16, 35).timestamp())
        for text in ("2026-10-16 16:35:00", "2026-10-16T16:35:00",
                     "2026-10-16 16:35", "2026/10/16 16:35:00"):
            self.assertEqual(http_common.parse_time_epoch(text), base, text)

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(http_common.parse_time_epoch(1792140000), 1792140000)
        self.assertEqual(http_common.parse_time_epoch(1792140000000), 1792140000)
        self.assertEqual(http_common.parse_time_epoch("1792140000"), 1792140000)

    def test_invalid(self):
        for value in (None, "", "-", "abc", 0, -5, True):
            self.assertEqual(http_common.parse_time_epoch(value), 0, repr(value))


# ---- Cookie ----


class TestCookie(unittest.TestCase):
    def test_normalize_raw_cookie_keeps_session(self):
        raw = ("_gcl_au=1.1.2; session=sess-value; session_2=second; "
               "i18next=zh-CN; trafficParams=xyz")
        self.assertEqual(wb.normalize_cookie(raw), "session=sess-value; session_2=second")

    def test_normalize_from_curl(self):
        curl = ("curl --url 'https://www.codebuddy.cn/x' -b 'a=1; session=sv; "
                "session_2=s2; other=drop' -H 'dnt: 1'")
        self.assertEqual(wb.normalize_cookie(curl), "session=sv; session_2=s2")

    def test_normalize_from_curl_cookie_header(self):
        curl = ("curl 'https://www.codebuddy.cn/x' -H 'cookie: session=cv; z=9' "
                "-H 'accept: application/json'")
        self.assertEqual(wb.normalize_cookie(curl), "session=cv")

    def test_normalize_bare_value(self):
        """只粘了值（没有 name=）时按 session 处理"""
        self.assertEqual(wb.normalize_cookie("plain-value"), "session=plain-value")

    def test_normalize_empty(self):
        self.assertEqual(wb.normalize_cookie(""), "")
        self.assertEqual(wb.normalize_cookie(None), "")

    def test_expiry_parsed_from_session(self):
        self.assertEqual(wb.cookie_expiry(COOKIE), 1792744353)

    def test_expiry_unknown(self):
        self.assertEqual(wb.cookie_expiry("session=abc"), 0)
        self.assertEqual(wb.cookie_expiry(""), 0)

    def test_is_expired(self):
        self.assertTrue(wb.cookie_is_expired("session=x|1000000001|sig",
                                             now=1000000002))
        self.assertFalse(wb.cookie_is_expired("session=x|1792744353|sig",
                                              now=1792138000))
        self.assertFalse(wb.cookie_is_expired("session=abc", now=9999999999),
                         "解析不出有效期时按未过期处理")


# ---- run()：窗口 / 分页 / 错误 ----


class TestRun(unittest.TestCase):
    def test_not_configured(self):
        self.assertEqual(wb.run("", 0, 1).status, "not_configured")
        self.assertEqual(wb.run("other=1", 0, 1).status, "not_configured",
                         "没有 session 不该发请求")

    def test_no_request_for_empty_window(self):
        fake = FakeSend([])
        result = wb.run(COOKIE, 100, 100, send=fake)
        self.assertTrue(result.is_ok)
        self.assertEqual(result.requests, 0)
        self.assertEqual(fake.calls, [])

    def test_one_request_per_day(self):
        """逐天切片：两天各一次请求，入参时间格式与区间边界都要对"""
        day1 = int(datetime(2026, 10, 15).timestamp())
        day2 = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item(request_id="a", time_text="2026-10-15 09:00:00")]),
                         _ok([_item(request_id="b", time_text="2026-10-16 09:00:00")])])
        result = wb.run(COOKIE, day1, day2 + 60, send=fake)

        self.assertTrue(result.is_ok)
        self.assertEqual(result.days, 2)
        self.assertEqual(result.requests, 2)
        self.assertEqual([r.request_id for r in result.rows], ["a", "b"])
        self.assertEqual(fake.calls[0]["body"]["startTime"], "2026-10-15 00:00:00")
        self.assertEqual(fake.calls[0]["body"]["endTime"], "2026-10-15 23:59:59")
        self.assertEqual(fake.calls[1]["body"]["startTime"], "2026-10-16 00:00:00")
        self.assertEqual(fake.calls[0]["body"]["pageNum"], 1)
        self.assertEqual(fake.calls[0]["body"]["pageSize"], wb.PAGE_SIZE)
        self.assertEqual(fake.calls[0]["url"], wb.API_BASE + wb.USAGE_PATH)
        self.assertIn("Cookie", fake.calls[0]["headers"])

    def test_pagination_and_dedup(self):
        """total 大于单页 → 继续翻页；跨页重复的 requestId 只留一份"""
        day = int(datetime(2026, 10, 16).timestamp())
        page1 = _item(request_id="a")
        page2 = _item(request_id="b")
        dup = _item(request_id="a", credit=9.9)
        fake = FakeSend([_ok([page1, page2], total=3), _ok([dup], total=3)])
        result = wb.run(COOKIE, day, day + 60, send=fake)

        self.assertEqual(result.requests, 2)
        self.assertEqual(sorted(r.request_id for r in result.rows), ["a", "b"])
        covered = [r for r in result.rows if r.request_id == "a"][0]
        self.assertAlmostEqual(covered.credit, 9.9, places=6, msg="后一页覆盖前一页")

    def test_pagination_stops_on_empty_page(self):
        """终止只看 total 与空页：服务端私自压小 page_size 时短页不代表收工"""
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item(request_id="a")], total=100),
                         _ok([], total=100)])
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertEqual(result.requests, 2, "空页才收工")
        self.assertEqual([r.request_id for r in result.rows], ["a"])
        self.assertFalse(result.truncated)

    def test_pagination_stops_when_total_reached(self):
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item(request_id="a"), _item(request_id="b")], total=2)])
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertEqual(result.requests, 1, "seen 达到 total 即止")

    def test_page_size_fallback(self):
        """首选页大小被服务端拒（code != 0）时，回落网页端默认值重试同一天首页"""
        day = int(datetime(2026, 10, 16).timestamp())
        rejected = (200, {}, _response([], code=9004, msg="The submitted order parameters are incorrect"))
        fake = FakeSend([rejected, _ok([_item(request_id="ok")])])
        result = wb.run(COOKIE, day, day + 60, send=fake)

        self.assertTrue(result.is_ok)
        self.assertEqual(result.requests, 2)
        self.assertEqual(fake.calls[0]["body"]["pageSize"], wb.PAGE_SIZE)
        self.assertEqual(fake.calls[1]["body"]["pageSize"], wb.PAGE_SIZE_FALLBACK)
        self.assertEqual([r.request_id for r in result.rows], ["ok"])

    def test_auth_expired_on_gateway_html(self):
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([(401, None, "<html>401 Authorization Required</html>")])
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertEqual(result.status, "auth_expired")

    def test_failed_on_non_json_200(self):
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([(502, None, "<html>bad gateway</html>")] * 2)
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertEqual(result.status, "failed")
        self.assertIn("502", result.message)

    def test_failed_on_no_response(self):
        day = int(datetime(2026, 10, 16).timestamp())
        result = wb.run(COOKIE, day, day + 60, send=FakeSend([(0, None, None)] * 2))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.message, "无响应")

    def test_failed_on_api_code(self):
        """业务码错误：首页用默认页大小重试一次仍失败 → 上报（不吞错因）"""
        day = int(datetime(2026, 10, 16).timestamp())
        bad = (200, {}, _response([], code=500, msg="boom"))
        fake = FakeSend([bad, bad])
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertEqual(result.status, "failed")
        self.assertIn("500", result.message)
        self.assertEqual(result.requests, 2, "只重试一次")

    def test_partial_rows_kept_on_auth_expiry(self):
        """前半段拿到数据后凭据过期：已拿到的行要带回来（调用方照样入库）"""
        day1 = int(datetime(2026, 10, 15).timestamp())
        day2 = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item(request_id="a", time_text="2026-10-15 09:00:00")]),
                         (401, None, "<html>401</html>")])
        result = wb.run(COOKIE, day1, day2 + 60, send=fake)
        self.assertEqual(result.status, "auth_expired")
        self.assertEqual([r.request_id for r in result.rows], ["a"])

    def test_truncated_when_page_cap_hit(self):
        """翻页到上限仍没拉完 → truncated（调用方据此不推进水位）"""
        day = int(datetime(2026, 10, 16).timestamp())
        responses = [(200, {}, _response([_item(request_id="r%d" % i)], total=10 ** 6))
                     for i in range(wb.MAX_PAGES_PER_DAY)]
        fake = FakeSend(responses)
        result = wb.run(COOKIE, day, day + 60, send=fake)
        self.assertTrue(result.truncated)
        self.assertEqual(result.requests, wb.MAX_PAGES_PER_DAY)

    def test_send_exception_becomes_failed(self):
        day = int(datetime(2026, 10, 16).timestamp())
        result = wb.run(COOKIE, day, day + 60,
                        send=FakeSend([RuntimeError("boom"), RuntimeError("boom")]))
        self.assertEqual(result.status, "failed")
        self.assertIn("boom", result.message)


class TestRenewal(unittest.TestCase):
    def test_new_cookie_from_set_cookie_header(self):
        """服务端回写 session 时，把新值并进整条 Cookie 交给调用方持久化"""
        day = int(datetime(2026, 10, 16).timestamp())
        headers = {"Set-Cookie": "session=fresh|1792744353|sig; Path=/; HttpOnly"}
        fake = FakeSend([_ok([_item()], headers=headers)])
        result = wb.run(COOKIE, day, day + 60, send=fake)

        self.assertTrue(result.is_ok)
        self.assertIn("session=fresh|1792744353|sig", result.new_cookie)
        self.assertIn("session_2=abc|def", result.new_cookie, "未回写的键要保留")

    def test_no_header_no_new_cookie(self):
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item()])])
        self.assertEqual(wb.run(COOKIE, day, day + 60, send=fake).new_cookie, "")

    def test_unrelated_set_cookie_ignored(self):
        day = int(datetime(2026, 10, 16).timestamp())
        fake = FakeSend([_ok([_item()], headers={"Set-Cookie": "i18next=zh-CN; Path=/"})])
        self.assertEqual(wb.run(COOKIE, day, day + 60, send=fake).new_cookie, "")


# ---- 节流常量 ----


class TestConstants(unittest.TestCase):
    def test_defaults_and_bounds(self):
        self.assertEqual(wb.BG_MIN_INTERVAL, 600)
        self.assertEqual(wb.INTERACTIVE_MIN_INTERVAL, 60)
        self.assertEqual((wb.BG_INTERVAL_MIN, wb.BG_INTERVAL_MAX), (60, 7200))
        self.assertEqual((wb.FIRST_FETCH_DAYS, wb.PAGE_LOOKBACK_DAYS), (30, 2))
        self.assertEqual(wb.API_BASE, "https://www.codebuddy.cn")
        self.assertEqual(wb.USAGE_PATH, "/billing/meter/get-user-request-usage")

    def test_shared_with_http_common(self):
        """默认值与边界不各写一份：以 http_common 为单一来源"""
        self.assertEqual(wb.BG_MIN_INTERVAL, http_common.DEFAULT_BG_INTERVAL)
        self.assertEqual(wb.BG_INTERVAL_MAX, http_common.BG_INTERVAL_MAX)
        self.assertIs(wb.clamp_bg_interval, http_common.clamp_bg_interval)

    def test_credit_total(self):
        rows = [wb.WorkBuddyRow("a", "c", "m", 1.5, 1),
                wb.WorkBuddyRow("b", "c", "m", 2.25, 2)]
        self.assertAlmostEqual(wb.credit_total(rows), 3.75, places=6)


class TestHttpRetry(unittest.TestCase):
    """传输层重试：一次网络抖动不该浪费整轮同步（也不能把 4xx 当抖动重试）"""

    def test_retries_network_error_then_succeeds(self):
        calls = []

        def fake_once(url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                return (0, None, None)
            return (200, {}, "{}")

        with mock.patch.object(http_common, "_http_post_once", fake_once), \
                mock.patch.object(http_common.time, "sleep", lambda s: None):
            status, _headers, _text = http_common.http_post("http://x")
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)

    def test_retries_429_and_honours_retry_after(self):
        sleeps = []
        calls = []

        def fake_once(url, headers, body, timeout):
            calls.append(url)
            if len(calls) == 1:
                return (429, {"Retry-After": "2"}, "")
            return (200, {}, "{}")

        with mock.patch.object(http_common, "_http_post_once", fake_once), \
                mock.patch.object(http_common.time, "sleep", sleeps.append):
            status, _headers, _text = http_common.http_post("http://x")
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2.0], "应尊重 Retry-After")

    def test_does_not_retry_4xx(self):
        calls = []

        def fake_once(url, headers, body, timeout):
            calls.append(url)
            return (401, {}, "denied")

        with mock.patch.object(http_common, "_http_post_once", fake_once):
            status, _headers, _text = http_common.http_post("http://x")
        self.assertEqual(status, 401)
        self.assertEqual(len(calls), 1, "401 是凭据失效，重试无意义")


if __name__ == "__main__":
    unittest.main()
