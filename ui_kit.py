"""UI 基础组件：设计令牌 / 颜色工具 / 图表绘制 / 卡片助手（从 main.py 拆出）。

叶子模块：不依赖 main，只依赖 themes / l10n；tkinter 只在函数内 import
（tests 在无显示环境下也要能 import）。
"""

from datetime import datetime
import themes
import l10n

class Design:
    """统一的设计令牌。

    颜色分两类：固定的（背景/文字/状态/用量色阶/字体）与主题驱动的
    （品牌色、数据高亮、大数字、图表色带、卡片填充/边框），
    后者由 Design.apply(theme) 按主题包覆盖，见 themes.design_tokens()。
    """

    # 品牌色（主题驱动，这里是与"默认主题"等价的内置值）
    BRAND = "#E86D45"          # 品牌橙
    DATA = "#F2B373"           # 数据高亮（表格里的数值）
    BIG_NUMBER = "#E86D45"     # 大数字（空=跟随 BRAND）
    BACKGROUND = "#171719"     # 深色背景（mac Design.backgroundDark = rgb(0.09,0.09,0.11)）
    CARD_FILL = "#252527"      # 卡片填充（白 6% 叠在背景上）
    CARD_BORDER = "#2E2E30"    # 卡片边框（白 10%）
    SEPARATOR = "#2E2E30"      # 分隔线（白 10%）
    SCANLINES = False          # CRT 扫描线特效

    # 主题驱动，但 mac 把它们算出来后被 Design.apply 丢掉过；现在一并落到 Design 上
    TREND = {"yesterday": "#007AFF", "week": "#AF52DE",
             "month": "#32ADE2", "total": "#E86D45"}
    GLOW_RADIUS = 14           # 大数字光晕半径
    GLOW_ALPHA = 0.30          # 大数字光晕不透明度
    BIG_NUMBER_WEIGHT = "bold"  # "bold" / "heavy"

    # 文字（mac 是 white @ 0.60 / 0.38 叠在背景上，这里直接算成等效色）
    TEXT_PRIMARY = "#FFFFFF"
    TEXT_SECONDARY = themes.blend("#FFFFFF", "#171719", 0.60)
    TEXT_MUTED = themes.blend("#FFFFFF", "#171719", 0.38)

    # 圆角（mac Design.cardCornerRadius=10 / barHeight=6）
    CARD_RADIUS = 10
    FIELD_RADIUS = 4
    BAR_HEIGHT = 6

    # 图表渐变色带（蓝 → 青 → 绿 → 黄 → 橙 → 粉）
    GRADIENT = [
        "#5A8CF2",  # 蓝
        "#66C7BF",  # 青
        "#73D180",  # 绿
        "#F2C759",  # 黄
        "#E86D45",  # 橙
        "#E67399",  # 粉
    ]

    # 模型配色（与环形图一致）
    MODEL_COLORS = [
        "#E86D45",  # 品牌橙
        "#5A8CF2",  # 蓝
        "#4CC38A",  # 绿
        "#F2A65A",  # 橙
        "#A78BFA",  # 紫
        "#F472B6",  # 粉
    ]

    # 状态色
    SUCCESS = "#4CC38A"
    WARNING = "#F2A65A"
    ERROR = "#E5675C"

    # 闪电 LED 三态（对齐 mac makeMenuBarIcon）：静息是模板单色（系统自适应灰，
    # Windows 没有 template 机制，取等效中灰）、本次刷新有增量转绿、增量撞红线转红。
    LED_IDLE = "#8E8E93"     # mac template（系统自适应）的等效灰
    LED_GREEN = "#34C759"    # NSColor.systemGreen
    LED_RED = "#FF3B30"      # NSColor.systemRed

    # 面板交互色（白色以 8% / 16% / 4% 叠在背景上的等效色）
    BTN_BG = themes.blend("#FFFFFF", "#171719", 0.08)
    BTN_BG_HOVER = themes.blend("#FFFFFF", "#171719", 0.16)
    ROW_HOVER = themes.blend("#FFFFFF", "#171719", 0.04)

    # 用量色阶关键色（按进度 0.0 ~ 1.0 排列）
    USAGE_STOPS = [
        (0.00, "#6BD99E"),  # 浅绿
        (0.35, "#A6DE73"),  # 黄绿
        (0.60, "#F2CC59"),  # 黄
        (0.82, "#F29447"),  # 橙
        (1.00, "#E6474D"),  # 红
    ]

    # 字体（对齐 mac 的磅值；tkinter 无 SF Pro Rounded / monospacedDigits，
    # 中文走雅黑、数字走 Consolas 等宽，只调 size/weight。雅黑渲染比 SF 大，故 CJK 取 mac-2）
    FONT_UI = ("Microsoft YaHei UI", 10)
    FONT_UI_SMALL = ("Microsoft YaHei UI", 9)
    FONT_UI_TINY = ("Microsoft YaHei UI", 8)
    FONT_MONO = ("Consolas", 10)
    FONT_MONO_SMALL = ("Consolas", 9)
    FONT_MONO_TINY = ("Consolas", 8)
    FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
    FONT_WINDOW_TITLE = ("Microsoft YaHei UI", 14, "bold")   # mac 设置窗口 18pt semibold
    FONT_BIG = ("Segoe UI", 26, "bold")            # mac 30pt bold rounded + monospacedDigits
    FONT_VALUE = ("Consolas", 12, "bold")          # mac 13pt semibold monospacedDigits
    FONT_SECTION = ("Microsoft YaHei UI", 9, "bold")
    FONT_SECTION_TITLE = ("Microsoft YaHei UI", 9, "bold")   # mac 11pt semibold（「趋势」）
    FONT_CARD_TITLE = ("Microsoft YaHei UI", 10, "bold")     # mac 12pt semibold
    FONT_GREETING = ("Microsoft YaHei UI", 9)               # mac 11pt medium
    FONT_MODEL_NAME = ("Microsoft YaHei UI", 9)              # mac 11pt medium
    FONT_MODEL_VALUE = ("Consolas", 9, "bold")               # mac 11pt medium
    FONT_TREND = ("Microsoft YaHei UI", 10)                  # mac 12pt medium
    FONT_TREND_VALUE = ("Consolas", 10, "bold")              # mac 12pt semibold
    FONT_PREDICTION = ("Microsoft YaHei UI", 8)              # mac 10pt
    FONT_DETAIL_STAT = ("Microsoft YaHei UI", 12, "bold")    # mac 16pt bold
    FONT_INSIGHT_VALUE = ("Microsoft YaHei UI", 16, "bold")  # mac 24pt bold
    FONT_CHEVRON = ("Microsoft YaHei UI", 13)                # mac 15pt medium
    FONT_BTN = ("Microsoft YaHei UI", 8)
    FONT_BTN_ICON = ("Segoe UI Emoji", 13)

    @staticmethod
    def fmt_tokens(n):
        """格式化 token 数量（中英双语：万/亿 ↔ K/M/B）"""
        return l10n.format_tokens(n)

    # ------------------------------------------------ 主题

    @classmethod
    def apply(cls, theme):
        """按主题覆盖颜色令牌（字体/背景/状态色/用量色阶不随主题变）。"""
        tokens = themes.design_tokens(theme, background=cls.BACKGROUND)
        cls.BRAND = tokens["BRAND"]
        cls.DATA = tokens["DATA"]
        cls.BIG_NUMBER = tokens["BIG_NUMBER"]
        cls.GRADIENT = tokens["GRADIENT"]
        cls.MODEL_COLORS = tokens["MODEL_COLORS"]
        cls.CARD_FILL = tokens["CARD_FILL"]
        cls.CARD_BORDER = tokens["CARD_BORDER"]
        cls.SEPARATOR = tokens["SEPARATOR"]
        cls.BTN_BG = tokens["BTN_BG"]
        cls.BTN_BG_HOVER = tokens["BTN_BG_HOVER"]
        cls.ROW_HOVER = tokens["ROW_HOVER"]
        cls.SCANLINES = tokens["SCANLINES"]
        # 这四个以前被算出来又丢掉：glow 是大数字光晕，TREND 是趋势行图标色，
        # BIG_NUMBER_WEIGHT 是大数字字重——mac 三处都用，缺了就对不上。
        cls.TREND = tokens["TREND"]
        cls.GLOW_RADIUS = tokens["GLOW_RADIUS"]
        cls.GLOW_ALPHA = tokens["GLOW_ALPHA"]
        cls.BIG_NUMBER_WEIGHT = tokens["BIG_NUMBER_WEIGHT"]
        return tokens

def _hex_to_rgb(hex_color):
    """#RRGGBB -> (r, g, b)"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def _rgb_to_hex(rgb):
    """(r, g, b) -> #RRGGBB"""
    return "#{:02X}{:02X}{:02X}".format(
        max(0, min(255, int(rgb[0]))),
        max(0, min(255, int(rgb[1]))),
        max(0, min(255, int(rgb[2]))),
    )

def blend(color, background, alpha):
    """把 color 以 alpha 不透明度混合到 background 上（tkinter Canvas 不支持透明度）"""
    c = _hex_to_rgb(color)
    b = _hex_to_rgb(background)
    return _rgb_to_hex(tuple(c[i] * alpha + b[i] * (1 - alpha) for i in range(3)))

def usage_color(progress):
    """按用量进度取色（0.0 浅绿 → 1.0 红）"""
    p = min(max(progress, 0.0), 1.0)
    stops = Design.USAGE_STOPS

    for i in range(len(stops) - 1):
        a_pos, a_color = stops[i]
        b_pos, b_color = stops[i + 1]
        if p <= b_pos:
            span = b_pos - a_pos
            t = (p - a_pos) / span if span > 0 else 0
            c1 = _hex_to_rgb(a_color)
            c2 = _hex_to_rgb(b_color)
            rgb = tuple(c1[k] + (c2[k] - c1[k]) * t for k in range(3))
            return _rgb_to_hex(rgb)

    return stops[-1][1]



# 今日用量（面板大数字 + 托盘图标）标红的阈值：超过 8000万 才变红
TODAY_RED_THRESHOLD = 80_000_000


def today_usage_color(total):
    """今日用量配色

    8000万以下走绿 → 黄 → 橙（最高只到橙），超过阈值才标红。
    """
    if total > TODAY_RED_THRESHOLD:
        return Design.USAGE_STOPS[-1][1]
    # 0.82 是色阶里橙色那一档，卡住不让它滑到红
    return usage_color(total / TODAY_RED_THRESHOLD * 0.82)


def model_colors(count=6):
    """模型配色：每小时换一次顺序

    同一小时内颜色稳定，跨小时自动换一套。
    """
    colors = Design.MODEL_COLORS[:]

    # 当前小时种子
    now = datetime.now()
    seed = (now.year * 1_000_000 + now.month * 10_000
            + now.day * 100 + now.hour)

    # Fisher-Yates 洗牌（确定性 LCG）
    state = (seed * 6364136223846793005 + 1442695040888963407) % (1 << 64)

    def next_rand():
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return state >> 33

    for i in range(len(colors) - 1, 0, -1):
        j = next_rand() % (i + 1)
        colors[i], colors[j] = colors[j], colors[i]

    return [colors[i % len(colors)] for i in range(count)]

def gradient_color(colors, progress, hue_offset=0.0):
    """在色带上按进度取色，可选色相偏移"""
    import colorsys

    if not colors:
        return Design.BRAND
    if len(colors) == 1:
        return colors[0]

    p = min(max(progress, 0.0), 1.0)
    scaled = p * (len(colors) - 1)
    idx = min(int(scaled), len(colors) - 2)
    t = scaled - idx

    c1 = _hex_to_rgb(colors[idx])
    c2 = _hex_to_rgb(colors[idx + 1])
    rgb = tuple((c1[i] + (c2[i] - c1[i]) * t) / 255.0 for i in range(3))

    if hue_offset:
        h, s, v = colorsys.rgb_to_hsv(*rgb)
        h = (h + hue_offset) % 1.0
        rgb = colorsys.hsv_to_rgb(h, s, v)

    return _rgb_to_hex(tuple(x * 255 for x in rgb))

class ChartCanvas:
    """基于 tkinter Canvas 的图表绘制工具"""

    @staticmethod
    def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
        """画圆角矩形（tkinter 没有原生圆角，用两个矩形 + 四个扇形拼）

        r 会自动收敛到不超过短边的一半。
        """
        r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        if r <= 0:
            return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)

        kwargs.setdefault("outline", "")
        items = [
            canvas.create_rectangle(x1 + r, y1, x2 - r, y2, **kwargs),
            canvas.create_rectangle(x1, y1 + r, x2, y2 - r, **kwargs),
        ]
        # 四个角的扇形：start/extent 组合出 90 度圆角
        corners = [
            (x1, y1, x1 + 2 * r, y1 + 2 * r, 90, 90),
            (x2 - 2 * r, y1, x2, y1 + 2 * r, 0, 90),
            (x2 - 2 * r, y2 - 2 * r, x2, y2, 270, 90),
            (x1, y2 - 2 * r, x1 + 2 * r, y2, 180, 90),
        ]
        for bx1, by1, bx2, by2, start, extent in corners:
            items.append(canvas.create_arc(
                bx1, by1, bx2, by2, start=start, extent=extent,
                style="pieslice", **kwargs))
        return items

    @staticmethod
    def draw_bar_chart(canvas, values, width, height, labels=None,
                       use_gradient=True, hue_offset=0.0, padding=6, label_height=16,
                       highlight_index=None):
        """绘制渐变柱状图

        highlight_index 指定的那根柱子用警示色（峰值高亮，对齐 mac 详情窗口）。
        """
        canvas.delete("all")
        if not values:
            return

        max_val = max(values) or 1
        n = len(values)
        available_h = height - label_height - padding * 2

        # 计算柱宽（留出间隙）
        slot = (width - padding * 2) / n
        bar_w = max(4, slot - 4)

        for i, val in enumerate(values):
            x = padding + i * slot
            bar_h = max(2, (val / max_val) * available_h)
            y_bottom = padding + available_h
            y_top = y_bottom - bar_h

            color = (gradient_color(Design.GRADIENT, i / max(n - 1, 1), hue_offset)
                     if use_gradient else Design.BRAND)
            if highlight_index is not None and i == highlight_index:
                color = Design.WARNING      # 峰值柱高亮

            # 圆角矩形（用两个矩形模拟上圆角）
            r = min(3, bar_w / 2, bar_h / 2)
            canvas.create_rectangle(x, y_top + r, x + bar_w, y_bottom,
                                    fill=color, outline="")
            canvas.create_oval(x, y_top, x + bar_w, y_top + r * 2,
                               fill=color, outline="")

            # 标签（隔一个显示）
            if labels and i < len(labels) and i % 2 == 0:
                canvas.create_text(x + bar_w / 2, height - label_height / 2,
                                   text=str(labels[i]), fill=Design.TEXT_MUTED,
                                   font=Design.FONT_MONO_TINY)

    @staticmethod
    def draw_sparkline(canvas, values, width, height, use_gradient=True,
                       hue_offset=0.0, padding=6, highlight_index=None):
        """绘制渐变折线图（带渐变填充）

        highlight_index 指定的数据点加一圈警示色标记（峰值点，mac LineTrendChart 同款）。
        """
        canvas.delete("all")
        if len(values) < 2:
            return

        max_val = max(values)
        min_val = min(values)
        val_range = max_val - min_val or 1
        n = len(values)
        step_x = (width - padding * 2) / (n - 1)
        available_h = height - padding * 2

        # 计算所有点
        points = []
        for i, val in enumerate(values):
            x = padding + i * step_x
            y = padding + (1 - (val - min_val) / val_range) * available_h
            points.append((x, y))

        # 渐变填充区域（用多边形模拟，按水平方向分片）
        for i in range(n - 1):
            alpha = 0.22
            c = (gradient_color(Design.GRADIENT, i / max(n - 1, 1), hue_offset)
                 if use_gradient else Design.BRAND)
            fill_color = blend(c, Design.BACKGROUND, alpha * 0.5)
            canvas.create_polygon(
                points[i][0], points[i][1],
                points[i + 1][0], points[i + 1][1],
                points[i + 1][0], height - padding,
                points[i][0], height - padding,
                fill=fill_color, outline=""
            )

        # 渐变色折线（逐段绘制）
        for i in range(n - 1):
            c = (gradient_color(Design.GRADIENT, i / max(n - 1, 1), hue_offset)
                 if use_gradient else Design.BRAND)
            canvas.create_line(points[i][0], points[i][1],
                               points[i + 1][0], points[i + 1][1],
                               fill=c, width=2, capstyle="round")

        # 峰值点常驻标记（画在终点圆点之前，避免被盖住）
        if highlight_index is not None and 0 <= highlight_index < n:
            px, py = points[highlight_index]
            canvas.create_oval(px - 4, py - 4, px + 4, py + 4,
                               outline=Design.WARNING, width=2)

        # 终点圆点
        end_color = (gradient_color(Design.GRADIENT, 1.0, hue_offset)
                     if use_gradient else Design.BRAND)
        lx, ly = points[-1]
        canvas.create_oval(lx - 5, ly - 5, lx + 5, ly + 5,
                           outline=end_color, width=2)
        canvas.create_oval(lx - 3.5, ly - 3.5, lx + 3.5, ly + 3.5,
                           fill=end_color, outline="")

    @staticmethod
    def draw_donut(canvas, items, size, hue_offset=0.0):
        """绘制环形图
        items: [(value, color, label), ...]
        """
        canvas.delete("all")
        total = sum(v for v, _, _ in items)
        if total <= 0:
            return

        pad = 10
        bbox = (pad, pad, size - pad, size - pad)
        start = 90  # 从顶部开始

        for value, color, _ in items:
            extent = -(value / total) * 360  # 负值 = 顺时针
            canvas.create_arc(*bbox, start=start, extent=extent,
                              style="arc", outline=color, width=16)
            start += extent

        # 中心总量
        center = size / 2
        canvas.create_text(center, center, text=Design.fmt_tokens(total),
                           fill=Design.TEXT_PRIMARY, font=Design.FONT_MONO_SMALL)

    @staticmethod
    def draw_sparkline_backdrop(canvas, values, width, height):
        """趋势行背后的淡色 sparkline（mac 的 hourlySparkline backdrop）。

        白 22% 细线 + 5% 面积，两端横向淡出；不画端点和峰值环——它只是垫底装饰，
        不能抢趋势数值的视觉焦点。detail 窗口用的 draw_sparkline 不受影响。
        """
        if not values or width <= 1 or height <= 1:
            return
        canvas.delete("all")
        vmax = max(values) or 1
        n = len(values)
        pad = 4
        usable_w = width - pad * 2
        usable_h = height - pad * 2
        pts = [(pad + usable_w * i / max(n - 1, 1),
                pad + usable_h * (1 - v / vmax)) for i, v in enumerate(values)]
        base = height - pad

        def fade(t):
            # mac 的横向遮罩：0 → 0.05 淡入，0.94 → 1 淡出
            if t < 0.05:
                return t / 0.05
            if t > 0.94:
                return max(0.0, (1.0 - t) / 0.06)
            return 1.0

        for i in range(n - 1):
            a = fade(i / max(n - 1, 1))
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            canvas.create_polygon(x1, y1, x2, y2, x2, base, x1, base,
                                  fill=blend("#FFFFFF", Design.BACKGROUND, 0.05 * a),
                                  outline="")
            canvas.create_line(x1, y1, x2, y2,
                               fill=blend("#FFFFFF", Design.BACKGROUND, 0.22 * a),
                               width=1)

# ------------------------------------------------------------ 卡片 / 语义取色原语
# 对齐 macOS 的 popoverCard / insightCard。tkinter 的 Frame 没有圆角，
# SetWindowRgn 又只作用于 Toplevel，所以卡片一律用 Canvas 自绘圆角底。
# 注意 tkinter 只在函数内 import：tests 在无显示环境下也要能 import main。

def work_area():
    """工作区矩形 (left, top, right, bottom)，排除任务栏。

    Windows 走 SPI_GETWORKAREA；取不到就退化成整屏。弹窗贴靠与窗口居中都用它。
    """
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    import tkinter as tk
    root = tk._default_root
    w = root.winfo_screenwidth() if root is not None else 1920
    h = root.winfo_screenheight() if root is not None else 1080
    return 0, 0, w, h

def center_geometry(default, work=None):
    """把 "WxH" 形式的缺省几何居中到工作区，返回 "WxH+X+Y"。

    只给 WxH 时 Tk 会把窗口丢在级联起点（34, 57，就在屏幕左上角），
    所以详情/洞察/设置这些没几何记忆的窗口必须显式给坐标。
    """
    import re
    m = re.match(r"^\s*(\d+)x(\d+)\s*$", default or "")
    if not m:
        return default
    w, h = int(m.group(1)), int(m.group(2))
    left, top, right, bottom = work if work else work_area()
    x = left + max((right - left - w) // 2, 0)
    y = top + max((bottom - top - h) // 2, 0)
    return "%dx%d+%d+%d" % (w, h, x, y)

def card_underlay(canvas, w, h, radius=None, fill=None, border=None):
    """铺一张圆角卡片底：外层边框色 + 内层填充色收 1px。

    ChartCanvas.round_rect 不支持描边——它把 kwargs 原样传给两个 create_rectangle，
    outline 会横穿卡片中间，所以边框只能靠双层填充叠出来。
    """
    radius = Design.CARD_RADIUS if radius is None else radius
    fill = fill or Design.CARD_FILL
    border = border or Design.CARD_BORDER
    canvas.delete("all")
    if w <= 2 or h <= 2:
        return
    ChartCanvas.round_rect(canvas, 0, 0, w, h, radius, fill=border)
    ChartCanvas.round_rect(canvas, 1, 1, w - 1, h - 1, max(radius - 1, 0), fill=fill)

def rounded_card(parent, padx=12, pady=8, radius=None, fill=None, border=None,
                 bg=None, expand=False):
    """圆角卡片容器，返回 (card, content)。

    content 是真正放控件的 Frame，背景是 CARD_FILL；它按 padx/pady 内缩，
    内缩量（默认 12）大于圆角（10），方角就不会盖住卡片的圆角。
    card 直接 pack/grid 即可。content 的子控件 bg 要设成 CARD_FILL，不是 BACKGROUND。
    """
    import tkinter as tk
    radius = Design.CARD_RADIUS if radius is None else radius
    fill = fill or Design.CARD_FILL
    border = border or Design.CARD_BORDER
    bg = bg or Design.BACKGROUND

    card = tk.Frame(parent, bg=bg, highlightthickness=0)
    canvas = tk.Canvas(card, bg=bg, highlightthickness=0, bd=0)
    canvas.place(x=0, y=0, relwidth=1, relheight=1)
    content = tk.Frame(card, bg=fill, highlightthickness=0)
    content.pack(fill=tk.BOTH if expand else tk.X, expand=expand,
                 padx=padx, pady=pady)

    def _redraw(event, c=canvas, r=radius, f=fill, b=border):
        card_underlay(c, event.width, event.height, r, f, b)

    canvas.bind("<Configure>", _redraw)
    card.underlay = canvas
    card.content = content
    return card, content

def card_title(parent, title, command=None):
    """卡片标题行：标题（含 emoji 图标）在左，`›` 在右，对齐 mac createCompactHeader。

    返回 row，command 不为 None 时整行可点。
    """
    import tkinter as tk
    row = tk.Frame(parent, bg=Design.CARD_FILL, highlightthickness=0)
    row.pack(fill=tk.X)
    tk.Label(row, text=title, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
             font=Design.FONT_CARD_TITLE, anchor='w').pack(side=tk.LEFT)
    tk.Label(row, text="›", fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
             font=Design.FONT_CHEVRON).pack(side=tk.RIGHT)
    return row

def section_title(parent, text, padx=16, pady=(12, 2), bg=None):
    """非卡片小节标题（mac 的「趋势」那类），没有 `›`。"""
    import tkinter as tk
    bg = bg or Design.BACKGROUND
    tk.Label(parent, text=text, fg=Design.TEXT_PRIMARY, bg=bg,
             font=Design.FONT_SECTION_TITLE, anchor='w').pack(
        anchor='w', padx=padx, pady=pady)

def muted_line(parent, text, padx=20, bg=None, pady=2):
    """灰色说明行。"""
    import tkinter as tk
    bg = bg or Design.BACKGROUND
    tk.Label(parent, text=text, fg=Design.TEXT_MUTED, bg=bg,
             font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X, padx=padx, pady=pady)

def flat_button(parent, text, cmd, primary=False):
    """扁平按钮（洞察/设置页的工具按钮）。"""
    import tkinter as tk
    return tk.Button(
        parent, text=text, command=cmd,
        bg=Design.BRAND if primary else Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
        activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
        relief=tk.FLAT, bd=0, padx=12, pady=4, cursor="hand2",
        font=Design.FONT_UI_SMALL)

def semantic_color(role, **ctx):
    """mac 的语义取色规则集中在这里，别处不要各写各的。

    role: primary / secondary / muted / data / bigNumber / accent / cache_rate
    """
    if role == "primary":
        return Design.TEXT_PRIMARY
    if role == "secondary":
        return Design.TEXT_SECONDARY
    if role == "muted":
        return Design.TEXT_MUTED
    if role == "data":
        return Design.DATA
    if role == "bigNumber":
        return Design.BIG_NUMBER
    if role == "accent":
        return Design.BRAND
    if role == "cache_rate":
        # mac cacheRateColor：rate > 75 成功色，否则警告色（75.0% 恰好算警告）
        rate = float(ctx.get("rate", 0) or 0)
        return Design.SUCCESS if rate > 75 else Design.WARNING
    return Design.TEXT_PRIMARY

def glow_text(canvas, x, y, text, font=None, color=None, anchor="w"):
    """大数字带光晕（mac 的 .shadow(accent.opacity(glowAlpha), radius: glowRadius)）。

    tkinter 没有阴影，用多圈半透明同字叠出柔光，真文字压在最上面。
    没走 PIL：_pil_font 在没装雅黑的机器上会回落到默认字体，而 _build 会被
    tests/ui_fixture 在裸 Frame 上调用，不该多出图片资源依赖。
    """
    import math
    font = font or Design.FONT_BIG
    color = color or Design.BIG_NUMBER
    halo = blend(color, Design.BACKGROUND, Design.GLOW_ALPHA * 0.30)
    for ring in (1.0, 0.45):
        r = max(1.0, Design.GLOW_RADIUS * ring)
        for i in range(8):
            angle = 2 * math.pi * i / 8
            canvas.create_text(x + r * math.cos(angle), y + r * math.sin(angle),
                               text=text, fill=halo, font=font, anchor=anchor)
    return canvas.create_text(x, y, text=text, fill=color, font=font, anchor=anchor)

class ChartReadout:
    """图表拖选读数（对齐 mac 的 BarReadoutChart / LineTrendChart）

    在图表 Canvas 上按下/拖动时，按 x 取最近的柱子或数据点，画一条竖直参考线
    并在指针附近显示「标签  数值」；松开即收起。读数会钳制在画布内。

    state 是绘图函数共用的那个 dict（含 labels/values），刷新图表时内容自动跟着变。
    kind="bars" 按柱槽取整（含间隙），kind="line" 按数据点间距取整。
    """

    TAG = "readout"
    PADDING = 6

    def __init__(self, canvas, state, kind="bars", fmt=None):
        self.canvas = canvas
        self.state = state
        self.kind = kind
        self.fmt = fmt or (lambda v: Design.fmt_tokens(int(v)))
        canvas.bind("<ButtonPress-1>", self._on_press, add="+")
        canvas.bind("<B1-Motion>", self._on_motion, add="+")
        canvas.bind("<ButtonRelease-1>", self._on_release, add="+")

    # ------------------------------------------------ 事件

    def _on_press(self, event):
        self.show_at(event.x)

    def _on_motion(self, event):
        self.show_at(event.x)

    def _on_release(self, _event=None):
        self.clear()

    # ------------------------------------------------ 绘制

    def clear(self):
        try:
            self.canvas.delete(self.TAG)
        except Exception:
            pass

    def index_at(self, x):
        """x 像素 → 最近的数据下标；画布还没布局（宽=1）或没数据时返回 None"""
        labels = self.state.get("labels") or []
        n = len(labels)
        if n <= 0:
            return None
        try:
            width = self.canvas.winfo_width()
        except Exception:
            return None
        if width <= 1:
            return None
        usable = max(width - self.PADDING * 2, 1)
        if self.kind == "line":
            step = usable / max(n - 1, 1)
            idx = int(round((x - self.PADDING) / step)) if step > 0 else 0
        else:
            slot = usable / n
            idx = int((x - self.PADDING) / slot) if slot > 0 else 0
        return min(max(idx, 0), n - 1)

    def show_at(self, pointer_x):
        """在 pointer_x 处显示读数（参考线落在数据点/柱槽中心）"""
        labels = self.state.get("labels") or []
        values = self.state.get("values") or []
        idx = self.index_at(pointer_x)
        self.clear()
        if idx is None or idx >= len(values):
            return
        try:
            width = self.canvas.winfo_width()
            height = self.canvas.winfo_height()
        except Exception:
            return
        if width <= 1:
            return

        n = len(labels)
        if self.kind == "line":
            step = max(width - self.PADDING * 2, 1) / max(n - 1, 1)
            cx = self.PADDING + idx * step
        else:
            slot = max(width - self.PADDING * 2, 1) / n
            cx = self.PADDING + (idx + 0.5) * slot
        self.canvas.create_line(cx, 2, cx, max(height - 2, 3),
                                fill=blend("#FFFFFF", Design.BACKGROUND, 0.25),
                                tags=self.TAG)

        item = self.canvas.create_text(
            pointer_x, 2, text="%s  %s" % (labels[idx], self.fmt(values[idx])),
            anchor="nw", fill=Design.TEXT_PRIMARY, font=Design.FONT_MONO_TINY,
            tags=self.TAG)
        x1, _y1, x2, _y2 = self.canvas.bbox(item)
        dx = 0
        if x1 < 2:
            dx = 2 - x1
        elif x2 > width - 2:
            dx = width - 2 - x2
        if dx:
            self.canvas.move(item, dx, 0)

def format_credits(value):
    """积分显示：整数不带小数点，小数保留两位（与 mac fmtCredits 同口径）"""
    v = float(value or 0)
    if v == round(v) and abs(v) < 100_000:
        return str(int(v))
    return "%.2f" % v