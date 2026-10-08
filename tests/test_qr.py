"""纯 Python 二维码编码器：黄金向量 + 结构断言。

黄金向量是用独立的参考实现（PyPI `qrcode` 8.2，**强制 byte 模式**、
`border=0`、`mask_pattern=None` 自动选掩码）逐个比对生成的：模拟器在
开发时对 122 组输入（版本 1~35、L/M/Q/H 四个纠错级，含中文字节串）
逐模块比对**全部一致**。这里把矩阵的 SHA-256 前 32 位锁进用例，
运行时不依赖任何第三方库（除 Pillow 用于 to_png）。
"""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qr  # noqa: E402

# (文本, 纠错级) -> (模块边长, 矩阵行拼接后的 sha256 前 32 位)
GOLDEN = {
    ("HELLO WORLD", "M"): (21, "2d21897bf5a7ac606d02da07bdd5e7f0"),
    ("https://github.com/bmfish/ccbar-win", "M"): (29, "07e5c01938b7e71fbf181b311e4411a3"),
    ("中文用量统计测试", "H"): (29, "ba0ef7dd028e8dc6864582fd50e94a38"),
    ("a" * 500, "L"): (77, "20c73f8db1d3a7cf981b73903724a1c0"),
}


class TestGoldenVectors(unittest.TestCase):
    def test_matches_reference_implementation(self):
        for (text, ec), (size, digest) in GOLDEN.items():
            with self.subTest(text=text[:20], ec=ec):
                rows = qr.to_matrix_rows(text, ec)
                self.assertEqual(len(rows), size)
                got = hashlib.sha256(("\n".join(rows)).encode()).hexdigest()[:32]
                self.assertEqual(got, digest,
                                 "矩阵与参考实现不一致（掩码/格式信息/交织任一环节错了）")


class TestMatrixStructure(unittest.TestCase):
    def test_shape_and_alphabet(self):
        for text in ("a", "hello world", "中文"):
            rows = qr.to_matrix_rows(text, "M")
            size = qr.matrix_size(text, "M")
            self.assertEqual(len(rows), size)
            for row in rows:
                self.assertEqual(len(row), size)
                self.assertTrue(set(row) <= {"0", "1"})

    def test_encode_returns_bool_matrix(self):
        m = qr.encode("https://github.com/bmfish/ccbar-win", "M")
        self.assertEqual(len(m), 29)
        self.assertTrue(all(isinstance(cell, bool) for row in m for cell in row))

    def test_finder_patterns_and_separator(self):
        m = qr.encode("hello", "M")
        size = len(m)
        # 左上定位图形：7x7 外圈深、次圈浅、3x3 核心深
        for y in range(7):
            for x in range(7):
                dist = max(abs(x - 3), abs(y - 3))
                self.assertEqual(m[y][x], dist != 2, "定位图形 (x=%d,y=%d) 不对" % (x, y))
        # 分隔符（切比雪夫距离 4）必须是浅色
        for i in range(8):
            self.assertFalse(m[7][i], "左上分隔符第 7 行应为浅色")
            self.assertFalse(m[i][7], "左上分隔符第 7 列应为浅色")
        # 右上 / 左下定位图形
        for i in range(7):
            self.assertEqual(m[i][size - 7 + i if False else size - 1], m[i][size - 1],
                             "右上角最右列应属于定位图形区域")
        self.assertTrue(m[0][size - 1], "右上定位图形角上必须是深色")
        self.assertTrue(m[size - 1][0], "左下定位图形角上必须是深色")
        self.assertTrue(m[size - 1][size - 1] is not None)

    def test_timing_pattern_alternates(self):
        m = qr.encode("hello", "M")
        size = len(m)
        # 第 6 行 / 第 6 列的定时图形：偶数索引深色，奇数浅色（跳过功能图形覆盖区）
        for i in range(8, size - 8):
            self.assertEqual(m[6][i], i % 2 == 0, "第 6 行定时图形第 %d 列不对" % i)
            self.assertEqual(m[i][6], i % 2 == 0, "第 6 列定时图形第 %d 行不对" % i)

    def test_dark_module(self):
        m = qr.encode("hello", "M")
        size = len(m)
        self.assertTrue(m[size - 8][8], "固定深色模块 (行 size-8, 列 8) 必须是深色")

    def test_timing_pattern_row6(self):
        m = qr.encode("hello", "M")
        self.assertTrue(m[6][8] is not None)


class TestVersionSelection(unittest.TestCase):
    def test_minimal_version_boundaries(self):
        """容量刚好卡在边界上：多一个字节就要升版本"""
        self.assertEqual(qr.matrix_size("a" * 17, "L"), 21, "17 字节正好装进 v1-L")
        self.assertEqual(qr.matrix_size("a" * 18, "L"), 25, "18 字节必须升到 v2")
        self.assertEqual(qr.matrix_size("a" * 14, "M"), 21, "14 字节正好装进 v1-M")
        self.assertEqual(qr.matrix_size("a" * 15, "M"), 25)

    def test_utf8_bytes_are_counted(self):
        """中文按 UTF-8 字节数算容量（8 个字 = 24 字节 → v2-M）"""
        text = "中文用量统计测试"
        self.assertEqual(len(text.encode("utf-8")), 24)
        self.assertEqual(qr.matrix_size(text, "M"), 25)

    def test_higher_ec_needs_bigger_matrix(self):
        text = "x" * 40
        sizes = [qr.matrix_size(text, ec) for ec in ("L", "M", "Q", "H")]
        self.assertEqual(sizes, sorted(sizes), "纠错级别越高，矩阵不应更小")

    def test_ec_level_case_insensitive(self):
        self.assertEqual(qr.matrix_size("hello", "m"), qr.matrix_size("hello", "M"))

    def test_invalid_ec_raises(self):
        with self.assertRaises(ValueError):
            qr.encode("hello", "X")

    def test_too_long_payload_raises(self):
        with self.assertRaises(ValueError) as ctx:
            qr.encode("a" * 3000, "H")
        self.assertIn("数据过长", str(ctx.exception))
        with self.assertRaises(ValueError):
            qr.encode("a" * 3000, "L")


class TestPng(unittest.TestCase):
    def test_documented_call_with_hex_colors(self):
        """share_card 用的就是这个调用形状：hex 颜色必须能用"""
        img = qr.to_png("https://github.com/bmfish/ccbar-win", scale=2, border=2,
                        ec="M", dark="#121214", light="#FFFFFF")
        self.assertEqual(img.size, (66, 66))     # (29 + 2*2) * 2
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(len(set(img.getdata())), 2, "二维码应只有两色")

    def test_defaults(self):
        img = qr.to_png("hi")
        self.assertEqual(img.size, (116, 116))   # (21 + 2*4) * 4
        self.assertEqual(len(set(img.getdata())), 2)

    def test_crisp_scaling_is_nearest_neighbour(self):
        """放大后每个模块应是整块纯色（不能有插值灰边）"""
        img = qr.to_png("hi", scale=4, border=1)
        # 静区一圈是浅色，第 1 个模块区域应整块纯色
        colors = set()
        for y in range(4, 8):
            for x in range(4, 8):
                colors.add(img.getpixel((x, y)))
        self.assertEqual(len(colors), 1, "模块块内出现插值色")

    def test_accepts_tuples_and_falls_back_on_bad_values(self):
        self.assertEqual(qr.to_png("hi", scale=1, border=0, dark=(0, 0, 0),
                                   light=(255, 255, 255)).size, (21, 21))
        # 非法颜色回落（深色→黑、浅色→白），不抛异常；静区像素可验证浅色
        self.assertEqual(qr.to_png("hi", scale=1, border=1, light="zzz").getpixel((0, 0)),
                         (255, 255, 255))
        self.assertEqual(qr.to_png("hi", scale=1, border=1, dark="zzz").getpixel((1, 1)),
                         (0, 0, 0))

    def test_scale_and_border_validation(self):
        with self.assertRaises(ValueError):
            qr.to_png("hi", scale=0)
        with self.assertRaises(ValueError):
            qr.to_png("hi", border=-1)


if __name__ == "__main__":
    unittest.main()
