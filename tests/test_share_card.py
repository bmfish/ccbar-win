"""洞察长图渲染（v2.0 起分享卡/周报卡已移除，仅保留导出长图）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import share_card  # noqa: E402


class TestInsightLongImage(unittest.TestCase):
    def test_long_image_grows_with_blocks(self):
        small = share_card.insight_long_image([("费用", [("今日", "$1.00")])])
        big = share_card.insight_long_image([("费用", [("今日", "$1.00")]),
                                             ("洞察", [("连续", "7 天")])])
        self.assertEqual(small.size[0], 720)
        self.assertGreater(big.size[1], small.size[1])
        self.assertEqual(share_card.insight_long_image([]).size[0], 720)

    def test_long_image_can_be_saved_as_png(self):
        import tempfile
        img = share_card.insight_long_image([("流水", [("今日", "1.2 万")])])
        path = os.path.join(tempfile.mkdtemp(prefix="ccbar-longimg-"), "x.png")
        img.save(path)
        with open(path, "rb") as f:
            self.assertTrue(f.read(8).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
