"""流水 v2 / 积分账本 / 签到记录：小时聚合、多日聚合、账本口径"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import stats_store
import trae_checkin
import trae_client_credentials as tcc
from stats_store import StatsStore
from trae_sync import TraeRow


_ORIG_STORAGE_OVERRIDE = None


def setUpModule():
    """隔离本机真实 Trae 客户端存储（签到 / 流水断言不随开发机登录态漂移）"""
    global _ORIG_STORAGE_OVERRIDE
    _ORIG_STORAGE_OVERRIDE = tcc.storage_override
    tcc.storage_override = []


def tearDownModule():
    tcc.storage_override = _ORIG_STORAGE_OVERRIDE


class TestTimelineV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-tl2-")
        self._orig = StatsStore.store_path
        StatsStore.store_path = staticmethod(lambda: os.path.join(self.tmp, "ccbar.db"))
        self.store = StatsStore()
        self.store.rebuild([], trae_enabled=True, trae_sessionid="sid")

    def tearDown(self):
        StatsStore.store_path = self._orig
        self.store._close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yday = today - timedelta(days=1)
        at = lambda d, h: int((d + timedelta(hours=h)).timestamp())
        self.store.upsert_trae_rows([
            TraeRow("t1", "m", 100, 50, 0, 0, 0, 2.5, at(today, 2)),
            TraeRow("t2", "m", 20, 10, 0, 0, 0, 1.0, at(today, 6)),
            TraeRow("y1", "m", 10, 5, 0, 0, 0, 1.5, at(yday, 1)),
        ])
        self.store._rebuild_daily_agg_window(
            (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            datetime.now().strftime("%Y-%m-%d"))

    def test_hourly_aggregate(self):
        self._seed()
        rows = self.store.query_hourly_timeline()
        self.assertEqual([r[0] for r in rows], [2, 6])
        self.assertEqual(rows[0][1], 1)
        self.assertAlmostEqual(rows[0][3], 2.5)
        self.assertEqual(rows[0][2], 150)

    def test_daily_aggregate_includes_today_and_history(self):
        self._seed()
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self.store.query_timeline_daily(yday, today)
        by_date = {}
        for d, _src, reqs, token, credits in rows:
            cur = by_date.setdefault(d, [0, 0, 0.0])
            cur[0] += reqs
            cur[1] += token
            cur[2] += credits
        self.assertEqual(by_date[yday][0], 1)
        self.assertAlmostEqual(by_date[yday][2], 1.5)
        self.assertEqual(by_date[today][0], 2, "今日由 usage_all 实时补齐")
        self.assertAlmostEqual(by_date[today][2], 3.5)
        self.assertEqual(by_date[today][1], 180)

    def test_credits_mtd(self):
        self._seed()
        self.assertAlmostEqual(self.store.query_credits_mtd(), 5.0)

    def test_checkin_log_fixup_and_today_badge(self):
        self.store._append_checkin_log("u1", True, 0, "success")
        self.store._append_checkin_log("u1", True, 0, "已经签到")
        today = datetime.now().strftime("%Y-%m-%d")
        self.store._fixup_checkin_log("u1", today, 300)
        log = self.store.checkin_log()
        self.assertEqual(len(log), 1, "同日重复 +0 记录合并")
        self.assertEqual(log[0]["credits"], 300)
        self.assertAlmostEqual(self.store.checkin_today_credits(), 300)
        self.assertAlmostEqual(self.store.checkin_month_gain(), 300)
        self.assertEqual(len(self.store.checkin_gains(30)), 1)

    def test_checkin_engine_respects_scheduled_minute(self):
        """起始小时后按账号随机一个分钟：未到点不发请求，到点才签"""
        acc = self._fake_account()
        today = datetime.now().strftime("%Y-%m-%d")
        minute = self.store._checkin_slot_minute("u1", today)
        status = trae_checkin.CheckinStatus(
            enable=True, checked_in=True, credits=0, code=0)
        with mock.patch.object(stats_store.trae_client_credentials, "discover",
                               return_value=[acc]), \
                mock.patch.object(stats_store.trae_checkin, "status",
                                  return_value=status) as status_mock, \
                mock.patch.object(stats_store.trae_checkin, "claim") as claim:
            early = datetime.now().replace(hour=8, minute=minute, second=0,
                                           microsecond=0)
            self.assertFalse(self.store.trae_checkin_if_needed(
                now=early, enabled=True, start_hour=9))
            status_mock.assert_not_called()
            due = early.replace(hour=9)
            self.assertTrue(self.store.trae_checkin_if_needed(
                now=due, enabled=True, start_hour=9))
            status_mock.assert_called_once()
            claim.assert_not_called()

    def test_checkin_is_skipped_while_another_run_is_active(self):
        """签到互斥：另一轮还在跑时直接返回，避免并发重复 claim"""
        self.store._checkin_guard.acquire()
        try:
            self.assertFalse(self.store.trae_checkin_if_needed(
                force=True, enabled=True, start_hour=9))
        finally:
            self.store._checkin_guard.release()

    def _fake_account(self):
        return tcc.ClientTraeAccount(
            "u1", "a.b.c", int(datetime.now().timestamp()) + 86400,
            device_id="1", source_app="Trae CN")

    def test_checkin_engine_claims_and_records_status_reward(self):
        """claim 响应不带金额：奖励必须取 status 的 credits + extra_credits"""
        status = trae_checkin.CheckinStatus(
            enable=True, checked_in=False, credits=200, extra_credits=100,
            code=0, message="success")
        claim = trae_checkin.CheckinClaim(True, code=0, message="success")
        with mock.patch.object(stats_store.trae_client_credentials, "discover",
                               return_value=[self._fake_account()]), \
                mock.patch.object(stats_store.trae_checkin, "status",
                                  return_value=status), \
                mock.patch.object(stats_store.trae_checkin, "claim",
                                  return_value=claim):
            self.assertTrue(self.store.trae_checkin_if_needed(
                force=True, enabled=True, start_hour=9))
        log = self.store.checkin_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["credits"], 300)
        self.assertTrue(self.store.checkin_done_today("u1"))

    def test_checkin_already_signed_is_recorded_once(self):
        """did_checked_in=true → 不调 claim，只补记一次真实奖励"""
        status = trae_checkin.CheckinStatus(
            enable=True, checked_in=True, credits=200, extra_credits=100,
            code=0, message="success")
        with mock.patch.object(stats_store.trae_client_credentials, "discover",
                               return_value=[self._fake_account()]), \
                mock.patch.object(stats_store.trae_checkin, "status",
                                  return_value=status), \
                mock.patch.object(stats_store.trae_checkin, "claim") as claim:
            self.store.trae_checkin_if_needed(force=True, enabled=True, start_hour=9)
            self.store.trae_checkin_if_needed(force=True, enabled=True, start_hour=9)
            claim.assert_not_called()
        log = self.store.checkin_log()
        self.assertEqual(len(log), 1, "同一天只记一笔")
        self.assertEqual(log[0]["credits"], 300)

    def test_checkin_skips_when_already_done_today(self):
        """今日已有签到水位时，即使手动 force 也不再请求 status/claim"""
        today = datetime.now().strftime("%Y-%m-%d")
        self.store._meta_set("trae_checkin_day_u1", today)
        self.store.conn.commit()
        with mock.patch.object(stats_store.trae_client_credentials, "discover",
                               return_value=[self._fake_account()]), \
                mock.patch.object(stats_store.trae_checkin, "status") as status, \
                mock.patch.object(stats_store.trae_checkin, "claim") as claim:
            self.assertFalse(self.store.trae_checkin_if_needed(
                force=True, enabled=True, start_hour=9))
            status.assert_not_called()
            claim.assert_not_called()
        self.assertEqual(self.store.checkin_log(), [], "已签账号不得再写签到记录")

    def test_claim_already_does_not_log_duplicate_reward(self):
        """服务端已签（跨进程/跨设备）只补水位，重复触发也只保留一笔奖励"""
        status = trae_checkin.CheckinStatus(
            enable=True, checked_in=False, credits=200, extra_credits=100,
            code=0, message="success")
        claim = trae_checkin.CheckinClaim(
            True, code=9095, message="当前设备今日已经签到，请明日再来哦～",
            already=True)
        with mock.patch.object(stats_store.trae_client_credentials, "discover",
                               return_value=[self._fake_account()]), \
                mock.patch.object(stats_store.trae_checkin, "status",
                                  return_value=status), \
                mock.patch.object(stats_store.trae_checkin, "claim",
                                  return_value=claim):
            self.store.trae_checkin_if_needed(force=True, enabled=True, start_hour=9)
            self.assertEqual(len(self.store.checkin_log()), 1)
            # 模拟另一端已签、本地水位被清：再跑一次也不能重复记奖励
            self.store._meta_set("trae_checkin_day_u1", "")
            self.store.conn.commit()
            self.store.trae_checkin_if_needed(force=True, enabled=True, start_hour=9)
        self.assertEqual(len(self.store.checkin_log()), 1, "成功后不得重复记奖励")


if __name__ == "__main__":
    unittest.main()
