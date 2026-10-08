"""纯 Python 二维码编码器（零第三方依赖）——对齐 macOS 版 CIQRCodeGenerator。

macOS 版分享卡用 CoreImage 的 CIQRCodeGenerator（纠错级 M）把仓库地址画成二维码；
Windows 版没有等价的系统 API，又不想为一个二维码就往 requirements.txt 里塞
qrcode/pypng 之类的运行时依赖，所以这里按 ISO/IEC 18004 自己实现最小闭环：

    字节模式（UTF-8）+ 版本 1..40 + L/M/Q/H 四档纠错 + 自动选最小版本 +
    GF(256)/0x11D 上的 Reed-Solomon + 分块交织 + 余数位 +
    定位/分隔/定时/校正图形 + 深色模块 + 格式信息 BCH(15,5) + 版本信息 BCH(18,6) +
    8 种数据掩码按标准罚分选优。

对外只有四个函数：encode / matrix_size / to_png / to_matrix_rows。
导入本模块不需要任何可选依赖；to_png 里才延迟 import PIL（Pillow 是项目既有依赖）。

掩码选优的口径与主流 Python 库 qrcode 逐位一致（含「评估罚分时格式/版本信息区
按浅色空着」这一细节），这样同一段文本在不同实现里得到同一张矩阵。
"""
# ---------------------------------------------------------------- 常量表

# 纠错级顺序：与下面的表行一一对应
_EC_LEVELS = ("L", "M", "Q", "H")
_EC_INDEX = {name: i for i, name in enumerate(_EC_LEVELS)}
# 格式信息里的 2 bit 纠错级编码：L=01 M=00 Q=11 H=10
_EC_FORMAT_BITS = (1, 0, 3, 2)

# 每块纠错码字数（行 = L/M/Q/H，列 = 版本 1..40）
_ECC_PER_BLOCK = (
    (7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)

# 分块数（行 = L/M/Q/H，列 = 版本 1..40）
_NUM_BLOCKS = (
    (1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

# 掩码函数：入参 (x=列, y=行)，与 ISO 附录里的 i(行)/j(列) 口径一致
_MASK_FUNCS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (y // 2 + x // 3) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0,
    lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda x, y: ((x * y) % 3 + (x + y) % 2) % 2 == 0,
)

# N3 罚分要匹配的两个 11 位图样：10111010000 / 00001011101
_PATTERN_DARK_FIRST = 0b10111010000  # 1488
_PATTERN_DARK_LAST = 0b00001011101   # 93


# ---------------------------------------------------------------- 容量

def _raw_data_modules(ver):
    """该版本除功能图形外可用的模块总数（含最后不足一字节的余数位）"""
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        num_align = ver // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver, ec_idx):
    """该版本/纠错级下可写的数据码字数（已扣掉纠错码字）"""
    raw = _raw_data_modules(ver) // 8
    return raw - _ECC_PER_BLOCK[ec_idx][ver - 1] * _NUM_BLOCKS[ec_idx][ver - 1]


def _align_positions(ver):
    """校正图形中心坐标表（ISO 表 E.1 的等价算法）"""
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    out = [6] * n
    pos = ver * 4 + 10
    for i in range(n - 1, 0, -1):
        out[i] = pos
        pos -= step
    return out


def _payload(text):
    """统一取 UTF-8 字节串（bytes 原样使用）"""
    if isinstance(text, str):
        return text.encode("utf-8")
    return bytes(text)


def _check_ec(ec):
    """纠错级 -> 表下标；非法值直接报错"""
    key = str(ec).upper()
    if key not in _EC_INDEX:
        raise ValueError("纠错级别只能是 L/M/Q/H 之一，收到 %r" % (ec,))
    return _EC_INDEX[key]


def _char_count_bits(ver):
    """字节模式的字符计数位宽：v1-9 用 8 bit，v10-40 用 16 bit"""
    return 8 if ver <= 9 else 16


def _select_version(data_len, ec_idx):
    """选能装下的最小版本；装不下则 ValueError（带字节数）"""
    for ver in range(1, 41):
        need = 4 + _char_count_bits(ver) + 8 * data_len
        if need <= 8 * _data_codewords(ver, ec_idx):
            return ver
    cap = _data_codewords(40, ec_idx)
    max_bytes = (cap * 8 - 4 - _char_count_bits(40)) // 8
    raise ValueError(
        "数据过长：%d 字节超出 40 版本 %s 级容量（最多 %d 字节）"
        % (data_len, _EC_LEVELS[ec_idx], max_bytes))


# ---------------------------------------------------------------- GF(256) / RS

def _gf_mul(x, y):
    """GF(256) 乘法，本原多项式 0x11D"""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    """生成多项式 (x-a^0)(x-a^1)...(x-a^(degree-1)) 的系数（省略最高次 1）"""
    result = [0] * degree
    result[-1] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    """多项式长除法取余：data 的纠错码字"""
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result[0]
        del result[0]
        result.append(0)
        for i in range(len(divisor)):
            result[i] ^= _gf_mul(divisor[i], factor)
    return result


# ---------------------------------------------------------------- 数据段

def _append_bits(bits, value, width):
    """按 MSB 优先追加 width 位"""
    for i in range(width - 1, -1, -1):
        bits.append((value >> i) & 1)


def _data_codewords_bytes(data, ver, ec_idx):
    """模式指示符 + 字符计数 + 数据 + 终止符 + 字节对齐 + 填充码字"""
    cap = _data_codewords(ver, ec_idx)
    bits = []
    _append_bits(bits, 0b0100, 4)               # 字节模式
    _append_bits(bits, len(data), _char_count_bits(ver))
    for byte in data:
        _append_bits(bits, byte, 8)
    bits = bits[:cap * 8]
    for _ in range(min(4, cap * 8 - len(bits))):  # 终止符
        bits.append(0)
    while len(bits) % 8:                          # 补到字节边界
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    pad = 0xEC                                    # 0xEC / 0x11 交替填充
    while len(out) < cap:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    return bytes(out)


def _add_ecc_and_interleave(data, ver, ec_idx):
    """按版本/纠错级分块、逐块算 RS、再按标准交织（短块留空位跳过）"""
    ecc_len = _ECC_PER_BLOCK[ec_idx][ver - 1]
    num_blocks = _NUM_BLOCKS[ec_idx][ver - 1]
    raw = _raw_data_modules(ver) // 8
    num_short = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    block_len = short_len + 1
    divisor = _rs_divisor(ecc_len)

    blocks = []
    k = 0
    for i in range(num_blocks):
        take = short_len - ecc_len + (0 if i < num_short else 1)
        dat = list(data[k:k + take])
        k += take
        block = dat + [0] * (block_len - len(dat))
        block[block_len - ecc_len:] = _rs_remainder(dat, divisor)
        blocks.append(block)

    out = bytearray()
    for i in range(block_len):
        for j, block in enumerate(blocks):
            if i == short_len - ecc_len and j < num_short:
                continue                          # 短块的数据/纠错之间的空位
            out.append(block[i])
    return bytes(out)


# ---------------------------------------------------------------- BCH

def _format_bits(ec_idx, mask):
    """格式信息：BCH(15,5)，生成式 0x537，最后异或掩码 0x5412"""
    data = (_EC_FORMAT_BITS[ec_idx] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(ver):
    """版本信息：BCH(18,6)，生成式 0x1F25"""
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (ver << 12) | rem


# ---------------------------------------------------------------- 罚分 / 掩码

def _apply_mask(modules, fixed, size, mask):
    """对非功能模块异或掩码（异或两次即还原）"""
    func = _MASK_FUNCS[mask]
    for y in range(size):
        row = modules[y]
        fixed_row = fixed[y]
        for x in range(size):
            if not fixed_row[x] and func(x, y):
                row[x] = not row[x]


def _penalty(modules, size):
    """标准罚分：N1=3（连续同色，长度 L 记 L-2）+ N2=3 + N3=40 + N4=10"""
    score = 0
    lines = list(modules)
    lines += [[modules[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        # N1：行/列里 >=5 个连续同色模块，每段记 3+(L-5)
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        # N3：1:1:3:1:1 类定位图形（前后各 4 个浅色模块），窗口不越界
        bits = 0
        for cell in line:
            bits = (bits << 1) | cell
        for i in range(size - 10):
            window = (bits >> (size - 11 - i)) & 0x7FF
            if window == _PATTERN_DARK_FIRST or window == _PATTERN_DARK_LAST:
                score += 40
    # N2：2x2 同色块
    for y in range(size - 1):
        row0, row1 = modules[y], modules[y + 1]
        for x in range(size - 1):
            if row0[x] == row0[x + 1] == row1[x] == row1[x + 1]:
                score += 3
    # N4：深色模块比例每偏离 50% 满 5% 记 10
    dark = sum(1 for row in modules for cell in row if cell)
    score += int(abs(dark / (size * size) * 100 - 50) / 5) * 10
    return score


# ---------------------------------------------------------------- 矩阵

class _QR:
    """一次构建的状态：功能图形、数据、掩码；矩阵 True = 深色"""

    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()

    # ---- 功能图形

    def _set(self, x, y, dark):
        self.modules[y][x] = dark
        self.fixed[y][x] = True

    def _mark(self, x, y):
        """占位：只标成功能模块，值保持浅色"""
        self.fixed[y][x] = True

    def _finder(self, cx, cy):
        """定位图形 + 分隔符（切比雪夫距离 4 的一圈是浅色分隔符）"""
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    dist = max(abs(dx), abs(dy))
                    self._set(x, y, dist != 2 and dist != 4)

    def _alignment(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def _function_patterns(self):
        for i in range(self.size):                    # 定时图形
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        self._finder(3, 3)                            # 三个定位图形
        self._finder(self.size - 4, 3)
        self._finder(3, self.size - 4)
        positions = _align_positions(self.version)    # 校正图形（压过定时图形）
        last = len(positions) - 1
        for i, cx in enumerate(positions):
            for j, cy in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue                          # 三个定位图形角上不画
                self._alignment(cx, cy)
        self._reserve_format()
        if self.version >= 7:
            self._reserve_version()

    def _reserve_format(self):
        for i in range(6):
            self._mark(8, i)
        self._mark(8, 7)
        self._mark(8, 8)
        self._mark(7, 8)
        for i in range(9, 15):
            self._mark(14 - i, 8)
        for i in range(8):
            self._mark(self.size - 1 - i, 8)
        for i in range(8, 15):
            self._mark(8, self.size - 15 + i)
        self._mark(8, self.size - 8)

    def _reserve_version(self):
        for i in range(18):
            a = self.size - 11 + i % 3
            b = i // 3
            self._mark(a, b)
            self._mark(b, a)

    def _write_format(self, ec_idx, mask):
        """两份格式信息 + 固定的深色模块"""
        bits = _format_bits(ec_idx, mask)
        for i in range(6):
            self._set(8, i, (bits >> i) & 1)
        self._set(8, 7, (bits >> 6) & 1)
        self._set(8, 8, (bits >> 7) & 1)
        self._set(7, 8, (bits >> 8) & 1)
        for i in range(9, 15):
            self._set(14 - i, 8, (bits >> i) & 1)
        for i in range(8):
            self._set(self.size - 1 - i, 8, (bits >> i) & 1)
        for i in range(8, 15):
            self._set(8, self.size - 15 + i, (bits >> i) & 1)
        self._set(8, self.size - 8, True)

    def _write_version(self):
        bits = _version_bits(self.version)
        for i in range(18):
            dark = bool((bits >> i) & 1)
            a = self.size - 11 + i % 3
            b = i // 3
            self._set(a, b, dark)
            self._set(b, a, dark)

    # ---- 数据

    def _draw_data(self, codewords):
        """从右下角起、两列一组蛇形填充；剩余的非功能模块留浅色，稍后一并掩码"""
        bits = []
        for byte in codewords:
            _append_bits(bits, byte, 8)
        total = len(bits)
        index = 0
        right = self.size - 1
        upward = True
        while right >= 1:
            if right == 6:                            # 跳过竖向定时图形那一列
                right = 5
            for vert in range(self.size):
                y = self.size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.fixed[y][x] and index < total:
                        self.modules[y][x] = bool(bits[index])
                        index += 1
            upward = not upward
            right -= 2

    # ---- 构建

    def build(self, codewords, ec_idx):
        self._draw_data(codewords)
        best, best_score = 0, None
        for mask in range(8):
            _apply_mask(self.modules, self.fixed, self.size, mask)
            score = _penalty(self.modules, self.size)
            _apply_mask(self.modules, self.fixed, self.size, mask)  # 还原
            if best_score is None or score < best_score:
                best, best_score = mask, score
        _apply_mask(self.modules, self.fixed, self.size, best)
        self._write_format(ec_idx, best)
        if self.version >= 7:
            self._write_version()
        # 统一成 bool：掩码/格式位的按位运算会留下 int 0/1
        self.modules = [[bool(cell) for cell in row] for row in self.modules]
        return self.modules


# ---------------------------------------------------------------- 对外 API

def encode(text, ec="M"):
    """把文本编码成二维码矩阵：list[list[bool]]，True = 深色模块，不含静区。

    字节模式（UTF-8），自动选能装下的最小版本（1..40）。
    """
    data = _payload(text)
    ec_idx = _check_ec(ec)
    ver = _select_version(len(data), ec_idx)
    codewords = _data_codewords_bytes(data, ver, ec_idx)
    codewords = _add_ecc_and_interleave(codewords, ver, ec_idx)
    return _QR(ver).build(codewords, ec_idx)


def matrix_size(text, ec="M"):
    """返回模块边长（= 版本 * 4 + 17，不含静区）"""
    return _select_version(len(_payload(text)), _check_ec(ec)) * 4 + 17


def to_matrix_rows(text, ec="M"):
    """矩阵转成 "0"/"1" 字符串列表，方便测试与调试"""
    return ["".join("1" if cell else "0" for cell in row) for row in encode(text, ec)]


def _rgb(value, fallback):
    """颜色参数 → (r, g, b)：支持 #RRGGBB / RRGGBB / 3 位缩写 / 已是元组；非法回落 fallback"""
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(int(max(0, min(255, c))) for c in value)
    if isinstance(value, str):
        body = value.strip().lstrip("#")
        if len(body) == 3:
            body = "".join(c * 2 for c in body)
        if len(body) == 6:
            try:
                return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
    return fallback


def to_png(text, scale=4, border=4, ec="M", dark="#000000", light="#FFFFFF"):
    """渲染成 PIL.Image（最近邻放大，边缘不糊）；border 为静区模块数。

    PIL 在这里才导入，模块本身导入不依赖 Pillow。
    """
    from PIL import Image
    scale = int(scale)
    border = int(border)
    if scale < 1 or border < 0:
        raise ValueError("scale 必须 >= 1 且 border 不能为负")
    dark_rgb = _rgb(dark, (0, 0, 0))
    light_rgb = _rgb(light, (255, 255, 255))
    rows = encode(text, ec)
    side = len(rows) + 2 * border
    small = Image.new("RGB", (side, side), light_rgb)
    pixels = small.load()
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            if cell:
                pixels[x + border, y + border] = dark_rgb
    return small.resize((side * scale, side * scale), Image.NEAREST)
