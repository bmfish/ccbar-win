"""分享卡/周报卡渲染 + 与周报模块的联调。

渲染是纯函数，可以在任何平台单测（Pillow 是项目既有依赖）。
二维码那部分依赖 qr 模块，若编码失败会降级为不画二维码——所以这里额外
断言"二维码真的画上去了"，避免静默降级。
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import share_card  # noqa: E402
import themes  # noqa: E402
import weekly_report  # noqa: E402


def distinct_colors(img):
    return len(set(img.getdata()))


class TestShareCard(unittest.TestCase):
    def test_share_card_size_and_content(self):
        img = share_card.share_card_image(
            1_234_567, 8_000_000, 30_000_000, 900_000_000,
            [("08-0%d" % i, i * 1000) for i in range(1, 8)])
        self.assertEqual(img.size, (640, 420))
        self.assertEqual(img.mode, "RGB")
        self.assertGreater(distinct_colors(img), 10, "卡片不能是纯色")

    def test_share_card_tolerates_missing_trend(self):
        """没数据时（趋势不足 2 点）也不能抛异常"""
        img = share_card.share_card_image(0, 0, 0, 0, [])
        self.assertEqual(img.size, (640, 420))
        img = share_card.share_card_image(1, 1, 1, 1, [("a", 1)])
        self.assertEqual(img.size, (640, 420))

    def test_accent_follows_theme(self):
        """换主题后卡片主色要跟着换（海蓝 #2E8CF2）"""
        img = share_card.share_card_image(1000, 2000, 3000, 4000, [],
                                         theme=themes.find("海蓝"))
        colors = set(img.getdata())
        r, g, b = themes.parse_hex("#2E8CF2")
        # 抗锯齿/混合后不会完全精确，允许邻近色
        near = [c for c in colors if abs(c[0] - r) < 12 and abs(c[1] - g) < 12
                and abs(c[2] - b) < 12]
        self.assertTrue(near, "卡片上找不到主题主色")

    def test_weekly_card(self):
        img = share_card.weekly_card_image("2026年7月27日 ~ 2026年8月2日",
                                          70_000, 12, 20_000,
                                          [("07-27", 1000), ("07-28", 2000)])
        self.assertEqual(img.size, (460, 360))
        self.assertGreater(distinct_colors(img), 10)

    def test_weekly_card_png_bytes(self):
        data = share_card.weekly_card_png("x ~ y", 1, 1, 1, [("a", 1)])
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), "必须是 PNG")

    def test_qr_is_actually_drawn(self):
        """二维码不能静默降级成空白（qr 模块的 to_png 必须能吃 hex 颜色）"""
        img = share_card._qr_image(share_card.QR_URL, 64)
        self.assertIsNotNone(img, "_qr_image 返回 None：二维码没画上去")
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(distinct_colors(img), 2, "二维码应是黑白两色且锐利")

    def test_long_image_grows_with_blocks(self):
        small = share_card.insight_long_image([("费用", [("今日", "$1.00")])])
        big = share_card.insight_long_image([("费用", [("今日", "$1.00")]),
                                             ("洞察", [("连续", "7 天")])])
        self.assertEqual(small.size[0], 720)
        self.assertGreater(big.size[1], small.size[1])
        self.assertEqual(share_card.insight_long_image([]).size[0], 720)


class FakeStore:
    def __init__(self, total=70000, reqs=12):
        self.total = total
        self.reqs = reqs

    def query_window_stats(self, days_ago_from, days_ago_to):
        if self.total <= 0:
            return None
        return {"total": self.total, "reqs": self.reqs}

    def query_daily_tokens_between(self, days_ago_from, days_ago_to):
        days = list(range(days_ago_from, days_ago_to - 1, -1))
        return [("2026-07-%02d" % (i + 1), 1000 * (i + 1)) for i, _ in enumerate(days)]


class TestWeeklyReportIntegration(unittest.TestCase):
    """周报调度 + 真实渲染：这是周一自动出报的完整链路"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccbar-weekly-integration-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generates_a_real_png(self):
        path = weekly_report.generate_if_needed(
            FakeStore(), share_card.weekly_card_png, output_dir=self.tmp,
            now=datetime(2026, 8, 3, 9, 0))
        self.assertTrue(path and os.path.exists(path))
        self.assertTrue(os.path.basename(path).startswith("ccbar-weekly-2026-08-03"))

        from PIL import Image
        with Image.open(path) as img:
            self.assertEqual(img.size, (460, 360))

    def test_no_usage_no_file(self):
        path = weekly_report.generate_if_needed(
            FakeStore(total=0), share_card.weekly_card_png, output_dir=self.tmp,
            now=datetime(2026, 8, 3, 9, 0))
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
