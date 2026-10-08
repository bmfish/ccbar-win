import sys

# Windows 控制台默认 cp1252，中文 print 会抛 UnicodeEncodeError（CI 实锤），统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import  os
import threading
import time
import queue
import functools
from datetime import datetime, timedelta
from pathlib import Path
import random

# Windows 系统托盘
import pystray
from PIL import Image, ImageDraw
import win10toast

from stats_store import StatsStore, SOURCE_REGISTRY, validate_db, adapter_for
from update_check import fetch_latest_version, is_newer_version, RELEASES_URL
import app_settings
import autostart
import l10n
import share_card
import themes
import weekly_report

# 当前版本（发布时随 tag 更新）
APP_VERSION = "1.5.0"

# 数据库路径（cc-switch 默认值，可在设置中修改）
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")


# ============================================================
# 设计系统（与 macOS 版保持一致）
# ============================================================

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
    BACKGROUND = "#1C1C1C"     # 深色背景
    CARD_FILL = "#2A2A2A"      # 卡片填充
    CARD_BORDER = "#3D3D3D"    # 卡片边框
    SEPARATOR = "#2A2A2A"      # 分隔线
    SCANLINES = False          # CRT 扫描线特效

    # 文字
    TEXT_PRIMARY = "#FFFFFF"
    TEXT_SECONDARY = "#8C8C8C"
    TEXT_MUTED = "#6B6B6B"

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

    # 面板交互色（白色以 8% / 16% / 4% 叠在 #1C1C1C 上的等效色）
    BTN_BG = "#2E2E2E"
    BTN_BG_HOVER = "#404040"
    ROW_HOVER = "#262626"

    # 用量色阶关键色（按进度 0.0 ~ 1.0 排列）
    USAGE_STOPS = [
        (0.00, "#6BD99E"),  # 浅绿
        (0.35, "#A6DE73"),  # 黄绿
        (0.60, "#F2CC59"),  # 黄
        (0.82, "#F29447"),  # 橙
        (1.00, "#E6474D"),  # 红
    ]

    # 字体
    FONT_UI = ("Microsoft YaHei UI", 10)
    FONT_UI_SMALL = ("Microsoft YaHei UI", 9)
    FONT_UI_TINY = ("Microsoft YaHei UI", 8)
    FONT_MONO = ("Consolas", 10)
    FONT_MONO_SMALL = ("Consolas", 9)
    FONT_MONO_TINY = ("Consolas", 8)
    FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
    FONT_BIG = ("Consolas", 22, "bold")
    FONT_VALUE = ("Consolas", 12)
    FONT_SECTION = ("Microsoft YaHei UI", 9, "bold")
    FONT_CHEVRON = ("Microsoft YaHei UI", 11)
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
                       use_gradient=True, hue_offset=0.0, padding=6, label_height=16):
        """绘制渐变柱状图"""
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
                       hue_offset=0.0, padding=6):
        """绘制渐变折线图（带渐变填充）"""
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


def _on_gui(method):
    """把整个方法体调度到 GUI 线程执行

    pystray 的菜单回调是在托盘消息循环线程里同步跑的，在那里直接
    root.mainloop() 会把托盘卡死（右键不出菜单）。所以所有窗口都必须
    投递到 GUI 线程创建。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._ui(lambda: method(self, *args, **kwargs))
    return wrapper


class PopoverWindow:
    """托盘左键弹出的自绘面板（对齐 macOS 版 popover 的观感）

    只在 GUI 线程上操作，外部调用一律经 CcBarTray._ui() 投递。
    """

    WIDTH = 300
    PAD = 12
    RADIUS = 10
    BTN_HEIGHT = 40
    BTN_GAP = 4

    def __init__(self, app):
        self.app = app
        self.win = None
        self._armed = False
        self._btn_down = False
        self._last_close = 0.0

    # ---------------- 生命周期 ----------------

    @property
    def is_open(self):
        return self.win is not None

    def toggle(self):
        if self.is_open:
            self.close()
        elif time.time() - self._last_close > 0.25:
            # 刚因"点了面板外面"而关掉时，这次点击不再重新弹出来
            self.show()

    def close(self):
        win, self.win = self.win, None
        self._last_close = time.time()
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def show(self):
        import tkinter as tk

        if self.win is not None:
            self.close()

        win = tk.Toplevel(self.app._ui_root)
        win.overrideredirect(True)
        win.configure(bg=Design.CARD_BORDER)   # 1px 边框靠外层底色透出来
        win.attributes("-topmost", True)
        self.win = win
        self._armed = False
        self._btn_down = False

        inner = tk.Frame(win, bg=Design.BACKGROUND)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        body = tk.Frame(inner, bg=Design.BACKGROUND)
        body.pack(fill=tk.BOTH, expand=True, padx=self.PAD, pady=(8, 6))

        self._build(body)

        # 定位：贴光标上方，钳制在工作区内
        win.update_idletasks()
        height = win.winfo_reqheight()
        x, y = self._position(win, height)
        win.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

        self._round_corners(win, self.RADIUS)
        win.lift()
        win.focus_force()
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", self._on_focus_out)
        win.after(250, self._arm)
        win.after(150, self._poll_outside_click)

    def _arm(self):
        """确认窗口真的拿到了焦点，才启用"失焦即关"

        overrideredirect 窗口在 Windows 上 focus_force 可能失败，那时
        失焦判断会误伤，所以干脆只靠"点外部关闭"来兜底。
        """
        win = self.win
        if win is None:
            return
        try:
            self._armed = win.focus_get() is not None
        except Exception:
            self._armed = False

    def _poll_outside_click(self):
        """轮询左键状态：在面板外面按下就关闭

        不依赖焦点，是面板关闭的主要途径。只认"按下"这个边沿，
        否则弹出瞬间鼠标还按着（点托盘的慢速点击）会被误判。
        """
        win = self.win
        if win is None:
            return
        try:
            import ctypes
            # VK_LBUTTON = 0x01
            down = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
            if down and not self._btn_down:
                px, py = win.winfo_pointerxy()
                x1, y1 = win.winfo_rootx(), win.winfo_rooty()
                inside = (x1 <= px <= x1 + win.winfo_width()
                          and y1 <= py <= y1 + win.winfo_height())
                if not inside:
                    self.close()
                    return
            self._btn_down = down
        except Exception:
            pass
        win.after(100, self._poll_outside_click)

    def _on_focus_out(self, _event=None):
        if self.win is not None:
            self.win.after(120, self._check_focus)

    def _check_focus(self):
        win = self.win
        if win is None or not self._armed:
            return
        try:
            if win.focus_get() is None:   # 焦点跑到别的程序了
                self.close()
        except Exception:
            pass

    @staticmethod
    def _position(win, height):
        """面板贴在光标上方（托盘在右下角），并钳制在工作区内"""
        cx, cy = win.winfo_pointerxy()
        left, top, right, bottom = PopoverWindow._work_area(win)
        width = PopoverWindow.WIDTH

        x = min(max(cx - width // 2, left + 8), right - width - 8)
        y = cy - height - 12
        if y < top + 8:
            y = min(cy + 12, bottom - height - 8)
        return x, y

    @staticmethod
    def _work_area(win):
        """工作区（排除任务栏）"""
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()

    @staticmethod
    def _round_corners(win, radius):
        """用窗口区域裁出圆角

        Win10 没有 DWMWA_WINDOW_CORNER_PREFERENCE（Win11 才有），
        所以走 SetWindowRgn。失败就保持直角，不能因此崩。
        """
        try:
            import ctypes
            win.update_idletasks()
            w, h = win.winfo_width(), win.winfo_height()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, w + 1, h + 1, radius * 2, radius * 2)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    # ---------------- 内容构建 ----------------

    def _build(self, parent):
        import tkinter as tk

        app = self.app
        today = app.query_day_stats(0)
        yesterday = app.query_day_stats(1)
        week = app.query_day_stats(7)
        month = app.query_day_stats(30)
        total = app.query_total_stats()
        models = app.query_model_breakdown()
        work_hours = app.query_work_hours()

        # 问候语
        greeting = random.choice(app.GREETINGS)
        if len(greeting) > 20:
            greeting = greeting[:19] + "…"
        tk.Label(parent, text=greeting, fg=Design.TEXT_SECONDARY,
                 bg=Design.BACKGROUND, font=Design.FONT_UI).pack(fill=tk.X)
        self._separator(parent)

        # 今日用量
        if today:
            self._section_header(parent, "📊 今日用量",
                                 self._open(app.show_hourly_detail_today))
            tk.Label(parent, text=Design.fmt_tokens(today["total"]),
                     fg=today_usage_color(today["total"]), bg=Design.BACKGROUND,
                     font=Design.FONT_BIG,
                     anchor='w').pack(fill=tk.X, pady=(2, 6))
            self._stat_columns(parent, today, work_hours)
        else:
            if app.store.attached:
                tk.Label(parent, text="📊 今日暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(
                             fill=tk.X, pady=6)
            else:
                self._stat_row(parent, "🌶️", "未找到数据源", "去设置",
                               self._open(app.show_settings))
        self._separator(parent)

        # 模型分布
        if models:
            self._section_header(parent, "🤖 模型分布",
                                 self._open(app.show_model_detail))
            max_total = models[0]["total"] if models else 1
            colors = model_colors()
            for idx, m in enumerate(models[:3]):
                name = m["model"]
                if len(name) > 14:
                    name = name[:14] + "…"
                self._model_row(parent, name, m["total"], max_total,
                                colors[idx % len(colors)])
            self._separator(parent)

        # 时间段统计
        if yesterday:
            self._stat_row(parent, "📅", "昨日", Design.fmt_tokens(yesterday["total"]),
                           self._open(app.show_hourly_detail_yesterday))
        if week:
            self._stat_row(parent, "📊", "近7天", Design.fmt_tokens(week["total"]),
                           self._open(app.show_weekly_detail))
        if month:
            self._stat_row(parent, "📆", "近30天", Design.fmt_tokens(month["total"]),
                           self._open(app.show_monthly_detail))
        if total:
            self._stat_row(parent, "📈", "历史总量", Design.fmt_tokens(total["total"]),
                           self._open(app.show_monthly_detail))
        self._separator(parent)

        # 按钮栏
        self._button_bar(parent, [
            ("📋", "复制", self._on_copy),
            ("🔄", "刷新", self._on_refresh),
            ("⚙️", "设置", self._on_settings),
            ("❌", "退出", self._on_quit),
        ])

    def _separator(self, parent):
        import tkinter as tk
        tk.Frame(parent, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=6)

    def _new_row(self, parent, height):
        import tkinter as tk
        row = tk.Frame(parent, bg=Design.BACKGROUND, height=height)
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        return row

    def _section_header(self, parent, title, command):
        import tkinter as tk
        row = self._new_row(parent, 22)
        tk.Label(row, text=title, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_SECTION, anchor='w').pack(side=tk.LEFT)
        tk.Label(row, text="›", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_CHEVRON).pack(side=tk.RIGHT)
        self._bind_row(row, command)

    def _stat_columns(self, parent, today, work_hours):
        """三列指标：请求数 / 缓存命中 / 时长"""
        import tkinter as tk

        total_input = today["input"] + today["cache_create"] + today["cache_read"]
        cache_rate = (today["cache_read"] / total_input * 100) if total_input > 0 else 0

        columns = [
            ("请求数", f"{today['reqs']}次", Design.TEXT_PRIMARY),
            ("缓存命中", f"{cache_rate:.0f}%",
             Design.SUCCESS if cache_rate > 80 else Design.WARNING),
        ]
        if work_hours:
            columns.append(("时长", f"{work_hours}h", Design.TEXT_PRIMARY))

        row = tk.Frame(parent, bg=Design.BACKGROUND)
        row.pack(fill=tk.X, pady=(0, 4))
        for i, (label, value, color) in enumerate(columns):
            row.grid_columnconfigure(i, weight=1, uniform="stat")
            cell = tk.Frame(row, bg=Design.BACKGROUND)
            cell.grid(row=0, column=i, sticky="ew")
            tk.Label(cell, text=label, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_UI_SMALL).pack()
            tk.Label(cell, text=value, fg=color, bg=Design.BACKGROUND,
                     font=Design.FONT_VALUE).pack(pady=(2, 0))

    def _stat_row(self, parent, icon, title, value, command):
        import tkinter as tk
        row = self._new_row(parent, 28)
        tk.Label(row, text=icon, bg=Design.BACKGROUND,
                 font=Design.FONT_UI).pack(side=tk.LEFT)
        tk.Label(row, text=title, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI).pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(row, text="›", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_CHEVRON).pack(side=tk.RIGHT)
        tk.Label(row, text=value, fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                 font=Design.FONT_MONO).pack(side=tk.RIGHT, padx=(0, 6))
        self._bind_row(row, command)

    def _model_row(self, parent, name, value, max_value, color):
        """模型名 + 右对齐用量 + 圆角进度条"""
        import tkinter as tk

        row = tk.Frame(parent, bg=Design.BACKGROUND)
        row.pack(fill=tk.X, pady=(0, 6))

        top = tk.Frame(row, bg=Design.BACKGROUND)
        top.pack(fill=tk.X)
        tk.Label(top, text=name, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(side=tk.LEFT)
        tk.Label(top, text=Design.fmt_tokens(value), fg=Design.TEXT_SECONDARY,
                 bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL,
                 anchor='e').pack(side=tk.RIGHT)

        ratio = (value / max_value) if max_value > 0 else 0
        bar = tk.Canvas(row, width=1, height=5, bg=Design.BACKGROUND,
                        highlightthickness=0)
        bar.pack(fill=tk.X, pady=(3, 0))

        def redraw(_event=None):
            bar.delete("all")
            w = bar.winfo_width()
            if w <= 1:
                w = self.WIDTH - 2 - 2 * self.PAD
            ChartCanvas.round_rect(bar, 0, 0, w, 5, 2.5, fill=Design.CARD_BORDER)
            fill_w = w * min(max(ratio, 0.0), 1.0)
            if fill_w > 1:
                ChartCanvas.round_rect(bar, 0, 0, fill_w, 5, 2.5, fill=color)

        bar.bind("<Configure>", redraw)

    def _button_bar(self, parent, buttons):
        """四宫格按钮：等宽圆角 + hover 高亮"""
        import tkinter as tk

        bar = tk.Canvas(parent, width=1, height=self.BTN_HEIGHT,
                        bg=Design.BACKGROUND, highlightthickness=0)
        bar.pack(fill=tk.X)
        state = {"index": -1}

        def geometry():
            w = bar.winfo_width()
            if w <= 1:
                w = self.WIDTH - 2 - 2 * self.PAD
            n = len(buttons)
            return w, (w - self.BTN_GAP * (n - 1)) / n

        def index_at(x):
            _w, bw = geometry()
            if bw <= 0:
                return -1
            i = int(x / (bw + self.BTN_GAP))
            return i if 0 <= i < len(buttons) else -1

        def redraw(_event=None):
            bar.delete("all")
            _w, bw = geometry()
            for i, (icon, label, _cmd) in enumerate(buttons):
                x1 = i * (bw + self.BTN_GAP)
                x2 = x1 + bw
                fill = Design.BTN_BG_HOVER if i == state["index"] else Design.BTN_BG
                ChartCanvas.round_rect(bar, x1, 0, x2, self.BTN_HEIGHT, 6, fill=fill)
                cx = (x1 + x2) / 2
                bar.create_text(cx, 13, text=icon, fill=Design.TEXT_PRIMARY,
                                font=Design.FONT_BTN_ICON)
                bar.create_text(cx, 28, text=label, fill=Design.TEXT_PRIMARY,
                                font=Design.FONT_BTN)

        def on_motion(event):
            i = index_at(event.x)
            if i != state["index"]:
                state["index"] = i
                redraw()
                bar.configure(cursor="hand2" if i >= 0 else "")

        def on_leave(_event):
            if state["index"] != -1:
                state["index"] = -1
                redraw()

        def on_click(event):
            i = index_at(event.x)
            if i >= 0:
                buttons[i][2]()

        bar.bind("<Configure>", redraw)
        bar.bind("<Motion>", on_motion)
        bar.bind("<Leave>", on_leave)
        bar.bind("<Button-1>", on_click)

    # ---------------- 行交互 ----------------

    def _open(self, command):
        """点开别的窗口前先收起面板"""
        def run():
            self.close()
            command()
        return run

    def _bind_row(self, row, command):
        """整行可点：绑到行和它所有子控件，带 hover 高亮"""
        def enter(_event):
            self._set_bg(row, Design.ROW_HOVER)

        def leave(_event):
            # 移到子控件上也会触发 Leave，这时不算真的离开
            under = row.winfo_containing(*row.winfo_pointerxy())
            if under is not None and self._is_within(under, row):
                return
            self._set_bg(row, Design.BACKGROUND)

        for widget in self._descendants(row):
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", lambda e, c=command: c())
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    @staticmethod
    def _descendants(widget):
        yield widget
        for child in widget.winfo_children():
            yield from PopoverWindow._descendants(child)

    @staticmethod
    def _is_within(widget, ancestor):
        node = widget
        while node is not None:
            if node is ancestor:
                return True
            node = getattr(node, "master", None)
        return False

    @staticmethod
    def _set_bg(widget, color):
        for w in PopoverWindow._descendants(widget):
            try:
                w.configure(bg=color)
            except Exception:
                pass

    # ---------------- 按钮动作 ----------------

    def _on_copy(self):
        self.close()
        self.app.copy_stats(self.app.icon, None)

    def _on_refresh(self):
        self.app.refresh_data(self.app.icon, None)
        self.show()          # 重建内容，刷新数据

    def _on_settings(self):
        self.close()
        self.app.show_settings()

    def _on_quit(self):
        self.close()
        self.app.quit_app(self.app.icon, None)


class CcBarTray:
    GREETINGS = [
        "今天也要加油写 Bug 哦 ✨",
        "代码如诗，Bug 如风 🌸",
        "写代码不如谈恋爱 💕",
        "需求又改了，习惯就好 🫠",
        "今天不出 Bug，明天出什么 🎯",
        "写代码使我快乐（并不）🎭",
        "技术债也是债 💸",
        "今天的需求明天再做 🌙",
        "码农的一天从咖啡开始 ☕",
        "Git commit -m '又一个 Bug' 🔧",
        "产品经理说很简单 🤡",
        "这个需求一天就能做完 📝",
        "代码能跑就行 🏃",
        "今天也是充满 Bug 的一天 🐛",
        "先实现，再优化（永远不优化）⏳",
        "这个接口我三分钟就写完 ⚡",
        "测试？什么测试？ 🎲",
        "线上出 Bug 了？不可能 🚫",
        "重构？先加个 if 吧 🤔",
        "这个功能很简单的 🎪",
    ]

    def __init__(self):
        self.icon = None
        # 设置：JSON 存储（app_settings），首次运行自动迁移老的 settings.txt
        self.settings = app_settings.Settings()
        self.theme = None
        # 自建统计库：历史每日补账 + 今日实时查询（对源库只读）
        self.store = StatsStore()

        # GUI 线程：所有 tkinter 窗口都跑在这个线程上，避免卡住托盘消息循环
        self._ui_queue = queue.Queue()
        self._ui_root = None
        self.popover = PopoverWindow(self)

        self.load_settings()

    # ------------------------------------------------------------
    # GUI 线程调度
    # ------------------------------------------------------------

    def _start_gui(self):
        """起一个常驻 GUI 线程，持有隐藏的 Tk root 并跑 mainloop"""
        import tkinter as tk

        ready = threading.Event()

        def run_ui():
            self._ui_root = tk.Tk()
            self._ui_root.withdraw()
            self._ui_root.after(50, self._drain_ui)
            ready.set()
            self._ui_root.mainloop()

        threading.Thread(target=run_ui, daemon=True, name="ccbar-gui").start()
        ready.wait(timeout=10)

    def _drain_ui(self):
        """在 GUI 线程里执行队列中的回调"""
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                print(f"UI 回调异常: {e}")

        if self._ui_root is not None:
            self._ui_root.after(50, self._drain_ui)

    def _ui(self, fn):
        """把回调投递到 GUI 线程执行（可从任意线程调用）"""
        if self._ui_root is None:
            print("GUI 线程未就绪，忽略 UI 请求")
            return
        self._ui_queue.put(fn)

    def toggle_popover(self, icon=None, item=None):
        """左键点托盘：开关面板"""
        self._ui(self.popover.toggle)

    @staticmethod
    def _bring_to_front(win):
        """把窗口提到最前

        托盘程序不是前台进程，Windows 不会自动把新建的窗口带到前面，
        用户会以为点了没反应。用 topmost 顶一下再撤销。
        """
        try:
            win.deiconify()
            win.lift()
            win.attributes("-topmost", True)
            win.after(300, lambda: CcBarTray._drop_topmost(win))
            win.focus_force()
        except Exception:
            pass

    @staticmethod
    def _drop_topmost(win):
        try:
            if win.winfo_exists():
                win.attributes("-topmost", False)
        except Exception:
            pass

    def load_settings(self):
        """加载设置（JSON 存储；首次运行自动迁移老的 settings.txt）"""
        self.settings.reload()
        self.apply_language()
        self.apply_theme()

    def save_settings(self):
        """保存设置"""
        self.settings.save()

    def apply_language(self):
        """按设置切换界面语言（中/英/跟随系统）"""
        l10n.set_language(self.settings.get("app_language") or "system")

    def apply_theme(self):
        """按设置套用主题，返回当前主题字典"""
        self.theme = themes.find(self.settings.get("theme") or "默认主题",
                                self.settings.get("custom_themes") or [])
        Design.apply(self.theme)
        return self.theme

    def backup_dir(self):
        """自动备份目录 ~/.ccbar/backups（与 macOS 版一样按天一份、滚动保留）"""
        path = os.path.join(os.path.expanduser("~"), ".ccbar", "backups")
        return path

    def maybe_auto_backup(self):
        """每天首次刷新时自动备份统计库，滚动保留 7 份（幂等：当天已备份就跳过）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.settings.get("last_auto_backup_date") == today:
            return None
        auto = getattr(self.store, "auto_backup", None)
        if auto is None:
            return None
        path = auto(self.backup_dir(), keep=7, today=today)
        # 备份失败不记账，下次刷新再试
        if path:
            self.settings.set("last_auto_backup_date", today)
            self.settings.save()
        return path

    def connect_store(self):
        """按当前设置（重）建统计库连接并 ATTACH 各数据源，随后补账一次"""
        configs = []
        for adapter_id, enabled, path in self.settings.source_configs():
            adapter = adapter_for(adapter_id)   # trae 是 HTTP 源，不在适配器注册表里
            if adapter is None:
                continue
            configs.append((adapter, enabled, path or adapter.default_path))
        self.store.rebuild(configs)
        self.store.sync_if_needed()

    def create_icon(self, color=None):
        """生成闪电图标

        color 为 None 时使用 ccBar.ico；否则按指定颜色绘制（用于用量变色）
        """
        if color is None:
            ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccBar.ico")
            if os.path.exists(ico_path):
                return Image.open(ico_path)

        # 绘制闪电（4x 超采样后缩小，边缘更平滑）
        scale = 4
        size = 64
        img = Image.new('RGBA', (size * scale, size * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pts = [(35, 63), (21, 35), (31, 35), (28, 7), (42, 35), (31, 35)]
        rgb = _hex_to_rgb(color or Design.BRAND)
        draw.polygon([(x * scale, y * scale) for x, y in pts],
                     fill=rgb + (255,))
        return img.resize((size, size), Image.LANCZOS)

    def update_icon_color(self):
        """根据今日用量更新托盘图标颜色"""
        today = self.query_day_stats(0)
        if not today:
            return

        color = today_usage_color(today["total"])
        if color == getattr(self, "_last_icon_color", None):
            return  # 颜色没变就不重绘

        self._last_icon_color = color
        if self.icon:
            try:
                self.icon.icon = self.create_icon(color)
                self.icon.title = f"{self.fmt_tokens(today['total'])}"
            except Exception:
                pass

    def check_token_milestone(self, total):
        """每累计 N 万 token 跨档通知一次（与 macOS 版同款口径：0=关闭，每档每天最多一次）"""
        interval_wan = self.settings.get("notify_interval", 1000)
        if interval_wan <= 0:
            return
        interval = interval_wan * 10_000
        tier = int(total / interval)
        if tier <= 0:
            return

        # 每个档位每天只通知一次（持久化，重启不重弹）
        today = datetime.now().strftime("%Y-%m-%d")
        mark = f"milestone_{today}_{tier}"
        if self._daily_mark_seen(mark):
            return
        self._daily_mark_add(mark)

        # 计算增量
        delta = total - (tier - 1) * interval if total % interval else interval

        # Toast 通知（Windows 原生）
        try:
            toaster = win10toast.ToastNotifier()
            toaster.show_toast(
                "🫧 里程碑",
                f"+{self.fmt_tokens(delta)} tokens！今日已达 {self.fmt_tokens(total)}（每{interval_wan}万通知一次）",
                duration=5,
                threaded=True
            )
        except Exception:
            pass

        # 托盘标题闪烁（加 ✨ 前缀，0.6秒后恢复）
        if self.icon:
            try:
                original = self.icon.title or f"{self.fmt_tokens(total)}"
                self.icon.title = f"✨ {self.fmt_tokens(total)}"

                def restore():
                    import time
                    time.sleep(0.6)
                    if self.icon:
                        self.icon.title = f"{self.fmt_tokens(total)}"

                t = threading.Thread(target=restore, daemon=True)
                t.start()
            except Exception:
                pass

    # ------------------------------------------------------------ 每日一次性标记（预警/里程碑共用）

    def _marks_file(self):
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        return config_dir / "notified.txt"

    def _daily_mark_seen(self, mark):
        """标记是否已打过；顺手把文件里非今天的旧行清掉"""
        f = self._marks_file()
        if not f.exists():
            return False
        with open(f, "r") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        today = datetime.now().strftime("%Y-%m-%d")
        kept = [l for l in lines if today in l]
        if kept != lines:
            with open(f, "w") as fh:
                fh.write("\n".join(kept) + ("\n" if kept else ""))
        return mark in lines

    def _daily_mark_add(self, mark):
        with open(self._marks_file(), "a") as fh:
            fh.write(mark + "\n")

    # ------------------------------------------------------------ 查询辅助

    @staticmethod
    def _local_midnight_epoch(days_ago=0):
        """本地时区 N 天前（0=今天，-1=明天）0 点的 epoch 秒"""
        day = datetime.now() - timedelta(days=days_ago)
        return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _day_str(days_ago=0):
        return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def _today_live(self):
        """今日实时聚合（usage_all 的"今日"段），无数据返回 None"""
        row = self.store.query_one("""
            SELECT COALESCE(SUM(request_count), 0),
                   COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_creation_tokens), 0),
                   COALESCE(SUM(cache_read_tokens), 0)
            FROM usage_all WHERE created_at >= ? AND created_at < ?
        """, (self._local_midnight_epoch(0), self._local_midnight_epoch(-1)))
        if not row:
            return None
        return self._stats_from_row(row)

    def _today_live_or_zero(self):
        t = self._today_live()
        if not t:
            t = {"reqs": 0, "input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "total": 0}
        return t

    @staticmethod
    def _stats_from_row(row):
        return {
            "reqs": row[0],
            "input": row[1],
            "output": row[2],
            "cache_create": row[3],
            "cache_read": row[4],
            "total": row[1] + row[2] + row[3] + row[4],
        }

    def _agg_sum(self, from_day, to_day):
        """daily_agg 日期区间汇总（仅覆盖昨天及更早；今日由调用方叠加实时值）"""
        row = self.store.query_one("""
            SELECT COALESCE(SUM(reqs), 0),
                   COALESCE(SUM(input), 0),
                   COALESCE(SUM(output), 0),
                   COALESCE(SUM(cache_create), 0),
                   COALESCE(SUM(cache_read), 0)
            FROM daily_agg WHERE date >= ? AND date <= ?
        """, (from_day, to_day))
        if not row:
            return None
        return self._stats_from_row(row)

    def query_day_stats(self, days=0):
        """查询统计：今日实时；历史区间读 daily_agg 聚合表（不再扫明细）"""
        if days == 0:
            return self._today_live()

        if days == 1:
            y = self._day_str(1)
            agg = self._agg_sum(y, y)
            return agg or self._today_live_or_zero()

        agg = self._agg_sum(self._day_str(days), self._day_str(1))
        if not agg:
            return self._today_live()
        today = self._today_live_or_zero()
        return {
            "reqs": agg["reqs"] + today["reqs"],
            "input": agg["input"] + today["input"],
            "output": agg["output"] + today["output"],
            "cache_create": agg["cache_create"] + today["cache_create"],
            "cache_read": agg["cache_read"] + today["cache_read"],
            "total": agg["total"] + today["total"],
        }

    def query_hourly_stats(self, days_ago=0):
        """查询每小时统计（epoch 区间条件，走索引）"""
        rows = self.store.query_all("""
            SELECT
                strftime('%H', created_at, 'unixepoch', 'localtime') as hour,
                COALESCE(SUM(request_count), 0) as reqs,
                COALESCE(SUM(output_tokens), 0) as output,
                COALESCE(SUM(input_tokens), 0) as input,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read
            FROM usage_all
            WHERE created_at >= ? AND created_at < ?
            GROUP BY hour
            ORDER BY hour
        """, (self._local_midnight_epoch(days_ago), self._local_midnight_epoch(days_ago - 1)))
        if rows is None:
            return None

        hourly_data = {}
        for row in rows:
            hour = int(row[0])
            hourly_data[hour] = {
                "reqs": row[1],
                "output": row[2],
                "input": row[3],
                "cache_read": row[4]
            }
        return hourly_data

    def query_daily_stats_for_range(self, start_date, end_date):
        """查询日期范围内的每日统计（历史走 daily_agg，今天实时叠加）"""
        rows = self.store.query_all("""
            SELECT
                date,
                COALESCE(SUM(reqs), 0) as reqs,
                COALESCE(SUM(output), 0) as output,
                COALESCE(SUM(input), 0) as input,
                COALESCE(SUM(cache_read), 0) as cache_read
            FROM daily_agg
            WHERE date >= ? AND date <= ?
            GROUP BY date
        """, (start_date, end_date))
        if rows is None:
            return None

        daily_data = {}
        for row in rows:
            daily_data[row[0]] = {
                "reqs": row[1],
                "output": row[2],
                "input": row[3],
                "cache_read": row[4]
            }

        # 今天不在 daily_agg 里，实时补上
        today_str = self._day_str(0)
        if start_date <= today_str <= end_date:
            t = self._today_live()
            if t:
                daily_data[today_str] = {
                    "reqs": t["reqs"],
                    "output": t["output"],
                    "input": t["input"],
                    "cache_read": t["cache_read"]
                }
        return daily_data

    def query_model_breakdown(self):
        """查询模型分布（跨渠道合并，口径与今日用量一致）"""
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day.timestamp())

        rows = self.store.query_all("""
            SELECT
                model,
                COALESCE(SUM(input_tokens), 0) as input,
                COALESCE(SUM(output_tokens), 0) as output,
                COALESCE(SUM(input_tokens + output_tokens
                             + cache_creation_tokens + cache_read_tokens), 0) as total
            FROM usage_all
            WHERE created_at >= ?
            GROUP BY model
            ORDER BY total DESC
            LIMIT 5
        """, (start_timestamp,))
        if rows is None:
            return None

        breakdown = []
        for row in rows:
            breakdown.append({
                "model": row[0],
                "input": row[1],
                "output": row[2],
                "total": row[3]
            })
        return breakdown if breakdown else None

    def query_work_hours(self):
        """查询工作时长"""
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day.timestamp())

        row = self.store.query_one("""
            SELECT MIN(created_at) FROM usage_all WHERE created_at >= ?
        """, (start_timestamp,))
        if row and row[0]:
            start = datetime.fromtimestamp(row[0])
            hours = (datetime.now() - start).total_seconds() / 3600
            if hours > 0:
                return f"{hours:.1f}"
        return None

    def query_total_stats(self):
        """查询历史总量（历史读 daily_agg + 今日实时，不再全表扫明细）"""
        agg = self._agg_sum("0000-01-01", "9999-12-31")
        today = self._today_live_or_zero()
        total = (agg["total"] if agg else 0) + today["total"]
        reqs = (agg["reqs"] if agg else 0) + today["reqs"]
        if total:
            return {"reqs": reqs, "total": total}
        return None

    def query_model_breakdown_by_day(self, days_ago=0):
        """查询某天的模型分布（含来源渠道；epoch 区间条件，走索引）"""
        rows = self.store.query_all("""
            SELECT
                source,
                model,
                COALESCE(SUM(request_count), 0) as reqs,
                COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) as total_token,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read
            FROM usage_all
            WHERE created_at >= ? AND created_at < ?
            GROUP BY source, model
            ORDER BY total_token DESC
        """, (self._local_midnight_epoch(days_ago), self._local_midnight_epoch(days_ago - 1)))
        if rows is None:
            return None

        models = []
        for row in rows:
            models.append({
                "source": row[0],
                "model": row[1],
                "reqs": row[2],
                "total_token": row[3],
                "cache_read": row[4]
            })
        return models

    def query_source_breakdown(self):
        """今日各数据源分账（source, 请求数, token 总量）"""
        rows = self.store.query_all("""
            SELECT source,
                   COALESCE(SUM(request_count), 0),
                   COALESCE(SUM(input_tokens + output_tokens
                                + cache_read_tokens + cache_creation_tokens), 0)
            FROM usage_all
            WHERE created_at >= ? AND created_at < ?
            GROUP BY source
            ORDER BY 3 DESC
        """, (self._local_midnight_epoch(0), self._local_midnight_epoch(-1)))
        if not rows:
            return []
        return [{"source": r[0], "reqs": r[1], "total": r[2]} for r in rows]

    def check_warning(self, stats):
        """检查是否需要预警"""
        if not self.settings["warning_enabled"]:
            return

        today_key = f"warning_{datetime.now().strftime('%Y-%m-%d')}"
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        notified_file = config_dir / "notified.txt"

        if notified_file.exists():
            with open(notified_file, "r") as f:
                if today_key in f.read():
                    return

        threshold_tokens = self.settings["warning_threshold"] * 10000
        if stats["total"] >= threshold_tokens:
            try:
                toaster = win10toast.ToastNotifier()
                toaster.show_toast(
                    "用量预警",
                    f"今日 Token 用量已达 {self.fmt_tokens(stats['total'])}，超过预警阈值 {self.settings['warning_threshold']}万",
                    duration=10
                )
            except:
                pass

            with open(notified_file, "a") as f:
                f.write(f"{today_key}\n")

    def fmt_tokens(self, tokens):
        """格式化 token 数量"""
        if tokens >= 100000000:
            return f"{tokens/100000000:.2f}亿"
        elif tokens >= 10000:
            return f"{tokens//10000}万"
        else:
            return str(tokens)

    def get_menu_text(self):
        """获取菜单显示文本"""
        today = self.query_day_stats(0)
        if not today:
            return "未找到数据源"

        total_str = self.fmt_tokens(today["total"])
        return total_str

    def build_menu(self):
        """构建菜单"""
        today = self.query_day_stats(0)
        yesterday = self.query_day_stats(1)
        week = self.query_day_stats(7)
        month = self.query_day_stats(30)
        models = self.query_model_breakdown()
        work_hours = self.query_work_hours()

        menu_items = []

        # default=True 的项会被左键点击触发，右键仍出原生菜单
        menu_items.append(pystray.MenuItem("打开面板", self.toggle_popover, default=True))

        greeting = random.choice(self.GREETINGS)
        menu_items.append(pystray.MenuItem(greeting[:16], None, enabled=False))
        menu_items.append(pystray.Menu.SEPARATOR)

        # 今日数据
        if today:
            self.check_warning(today)

            today_str = self.fmt_tokens(today["total"])
            menu_items.append(pystray.MenuItem(f"📊 今日: {today_str}", self.show_hourly_detail_today))

            menu_items.append(pystray.MenuItem(f"  🔢 请求: {today['reqs']}次", None, enabled=False))

            # 缓存命中率
            total_input = today["input"] + today["cache_create"] + today["cache_read"]
            cache_rate = (today["cache_read"] / total_input * 100) if total_input > 0 else 0
            menu_items.append(pystray.MenuItem(f"  💾 缓存命中: {cache_rate:.1f}%", None, enabled=False))

            if work_hours:
                menu_items.append(pystray.MenuItem(f"  ⏱️ 时长: {work_hours}h", None, enabled=False))
        else:
            if self.store.attached:
                menu_items.append(pystray.MenuItem("📊 今日暂无数据", None, enabled=False))
            else:
                menu_items.append(pystray.MenuItem("🌶️ 未找到数据源，请去设置", self.show_settings))

        menu_items.append(pystray.Menu.SEPARATOR)

        # 模型分布
        if models:
            menu_items.append(pystray.MenuItem("🤖 模型分布", self.show_model_detail))
            for m in models[:3]:
                model_name = m["model"][:15] + "…" if len(m["model"]) > 15 else m["model"]
                menu_items.append(pystray.MenuItem(f"  {model_name}: {self.fmt_tokens(m['total'])}", None, enabled=False))
            menu_items.append(pystray.Menu.SEPARATOR)

        # 昨日
        if yesterday:
            menu_items.append(pystray.MenuItem(f"📅 昨日: {self.fmt_tokens(yesterday['total'])}", self.show_hourly_detail_yesterday))

        # 近7天
        if week:
            menu_items.append(pystray.MenuItem(f"📅 近7天: {self.fmt_tokens(week['total'])}", self.show_weekly_detail))

        # 近30天
        if month:
            menu_items.append(pystray.MenuItem(f"📆 近30天: {self.fmt_tokens(month['total'])}", self.show_monthly_detail))

        menu_items.append(pystray.Menu.SEPARATOR)

        # 刷新
        menu_items.append(pystray.MenuItem("🔄 刷新", self.refresh_data))

        # 洞察中心
        menu_items.append(pystray.MenuItem("📈 洞察中心", self.show_insights))

        # 备份数据
        menu_items.append(pystray.MenuItem("💾 备份数据", self.backup_data))

        # 检查更新
        menu_items.append(pystray.MenuItem("⬇️ 检查更新", self.check_for_updates))

        # 设置
        menu_items.append(pystray.MenuItem("⚙️ 设置", self.show_settings))

        # 退出
        menu_items.append(pystray.MenuItem("❌ 退出", self.quit_app))

        return menu_items

    def show_hourly_detail_today(self, icon=None, item=None):
        """显示今日每小时详情"""
        self.show_hourly_detail(days_ago=0)

    def show_hourly_detail_yesterday(self, icon=None, item=None):
        """显示昨日每小时详情"""
        self.show_hourly_detail(days_ago=1)

    @_on_gui
    def show_hourly_detail(self, days_ago=0, date_str=None):
        """显示每小时详情窗口"""
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title("每小时用量详情")
        root.geometry("520x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(460, 400)

        # 计算日期
        if date_str:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            target_date = datetime.now() - timedelta(days=days_ago)

        # 顶部导航栏
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def nav_btn(parent, text, cmd):
            return tk.Button(parent, text=text, command=cmd,
                             bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             activebackground=Design.CARD_BORDER,
                             activeforeground=Design.TEXT_PRIMARY,
                             relief="flat", bd=0, width=3,
                             font=Design.FONT_UI_SMALL, cursor="hand2")

        def prev_day():
            nonlocal target_date, days_ago
            target_date -= timedelta(days=1)
            days_ago = (datetime.now() - target_date).days
            refresh()

        def next_day():
            nonlocal target_date, days_ago
            next_date = target_date + timedelta(days=1)
            if next_date <= datetime.now():
                target_date = next_date
                days_ago = (datetime.now() - target_date).days
                refresh()

        nav_btn(nav, "◀", prev_day).pack(side=tk.LEFT)

        date_label = tk.Label(nav, text=target_date.strftime("%y-%m-%d"),
                              font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                              bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)

        nav_btn(nav, "▶", next_day).pack(side=tk.RIGHT)

        # 图表区域（柱状图）
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))

        chart_canvas = tk.Canvas(chart_frame, height=110, bg=Design.BACKGROUND,
                                 highlightthickness=0)
        chart_canvas.pack(fill=tk.X)

        # 柱状图状态：canvas 要等窗口布局完成才有真实宽度，
        # 一建好就画的话 winfo_width() 只有 1，柱子会全挤在左边
        chart_state = {}

        def draw_chart(_event=None):
            if not chart_state:
                chart_canvas.delete("all")
                return
            width = chart_canvas.winfo_width()
            if width <= 1:
                return
            ChartCanvas.draw_bar_chart(chart_canvas, chart_state["values"], width, 110,
                                       labels=chart_state["labels"],
                                       use_gradient=True,
                                       hue_offset=chart_state["hue_offset"])

        chart_canvas.bind("<Configure>", draw_chart)

        # 表头
        header = tk.Frame(root, bg=Design.BACKGROUND)
        header.pack(fill=tk.X, padx=16)

        cols = [("时间", 7, 'w'), ("请求数", 9, 'e'), ("总token", 11, 'e'), ("缓存读", 11, 'e')]
        for text, width, anchor in cols:
            tk.Label(header, text=text, width=width, anchor=anchor,
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        tk.Frame(root, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, padx=16, pady=4)

        # 可滚动的数据区
        list_container = tk.Frame(root, bg=Design.BACKGROUND)
        list_container.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        scrollbar = tk.Scrollbar(list_container, orient="vertical")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        data_frame = tk.Frame(list_container, bg=Design.BACKGROUND)
        data_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        canvas_scroll = tk.Canvas(data_frame, bg=Design.BACKGROUND, highlightthickness=0)
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas_scroll, bg=Design.BACKGROUND)
        canvas_scroll.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_configure(event):
            canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
            canvas_scroll.itemconfigure("inner", width=canvas_scroll.winfo_width())

        inner.bind("<Configure>", on_configure)
        canvas_scroll.bind("<Configure>", on_configure)

        def on_mousewheel(event):
            canvas_scroll.yview_scroll(int(-event.delta / 120), "units")

        canvas_scroll.bind("<MouseWheel>", on_mousewheel)
        inner.bind("<MouseWheel>", on_mousewheel)

        def refresh():
            date_label.config(text=target_date.strftime("%y-%m-%d"))
            for w in inner.winfo_children():
                w.destroy()

            hourly_data = self.query_hourly_stats(days_ago)
            if not hourly_data:
                tk.Label(inner, text="暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                chart_state.clear()
                chart_canvas.delete("all")
                return

            hours_with_data = [h for h, d in hourly_data.items() if d["reqs"] > 0]
            if not hours_with_data:
                tk.Label(inner, text="暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                chart_state.clear()
                chart_canvas.delete("all")
                return

            start_hour = min(hours_with_data)
            end_hour = max(hours_with_data)

            # 合计
            total_reqs = sum(d["reqs"] for d in hourly_data.values())
            total_token = sum(d["output"] + d["input"] + d["cache_read"] for d in hourly_data.values())
            total_cache = sum(d["cache_read"] for d in hourly_data.values())

            # 绘制柱状图（按日期偏移色相）
            day_of_year = target_date.timetuple().tm_yday
            hue_offset = (day_of_year % 6) / 6.0
            values = [hourly_data.get(h, {}).get("reqs", 0) for h in range(start_hour, end_hour + 1)]
            labels = list(range(start_hour, end_hour + 1))
            chart_state.clear()
            chart_state.update(values=values, labels=labels, hue_offset=hue_offset)
            draw_chart()

            # 合计行
            total_row = tk.Frame(inner, bg=Design.BACKGROUND)
            total_row.pack(fill=tk.X)
            cells = [("合计", 7, 'w'), (f"{total_reqs}次", 9, 'e'),
                     (Design.fmt_tokens(total_token), 11, 'e'),
                     (Design.fmt_tokens(total_cache), 11, 'e')]
            for text, width, anchor in cells:
                tk.Label(total_row, text=text, width=width, anchor=anchor,
                         fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            # 每小时数据
            for hour in range(start_hour, end_hour + 1):
                d = hourly_data.get(hour, {"reqs": 0, "output": 0, "input": 0, "cache_read": 0})
                hour_token = d["output"] + d["input"] + d["cache_read"]

                row = tk.Frame(inner, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, pady=1)

                vals = [
                    (f"{hour}时", 7, 'w', Design.GRADIENT[0]),
                    (f"{d['reqs']}次" if d['reqs'] > 0 else "-", 9, 'e',
                     Design.TEXT_PRIMARY if d['reqs'] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(hour_token), 11, 'e',
                     Design.TEXT_PRIMARY if hour_token > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(d['cache_read']), 11, 'e',
                     Design.TEXT_SECONDARY if d['cache_read'] > 0 else Design.TEXT_MUTED),
                ]
                for text, width, anchor, fg in vals:
                    tk.Label(row, text=text, width=width, anchor=anchor, fg=fg,
                             bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        refresh()
        self._bring_to_front(root)

    def show_weekly_detail(self, icon=None, item=None):
        """显示近7天详情"""
        self.show_daily_detail(days=7, title="近7天用量")

    def show_monthly_detail(self, icon=None, item=None):
        """显示近30天详情"""
        self.show_daily_detail(days=30, title="近30天用量")

    @_on_gui
    def show_model_detail(self, icon=None, item=None):
        """显示模型分布详情（带环形图）"""
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title("模型分布详情")
        root.geometry("620x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(520, 420)

        current_days_ago = 0
        current_date = datetime.now()

        # 顶部导航栏
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def nav_btn(parent, text, cmd):
            return tk.Button(parent, text=text, command=cmd,
                             bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             activebackground=Design.CARD_BORDER,
                             activeforeground=Design.TEXT_PRIMARY,
                             relief="flat", bd=0, width=3,
                             font=Design.FONT_UI_SMALL, cursor="hand2")

        def prev_day():
            nonlocal current_days_ago, current_date
            current_days_ago += 1
            current_date = datetime.now() - timedelta(days=current_days_ago)
            refresh_model()

        def next_day():
            nonlocal current_days_ago, current_date
            if current_days_ago > 0:
                current_days_ago -= 1
                current_date = datetime.now() - timedelta(days=current_days_ago)
                refresh_model()

        nav_btn(nav, "◀", prev_day).pack(side=tk.LEFT)

        date_label = tk.Label(nav, text=current_date.strftime("%y-%m-%d"),
                              font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                              bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)

        nav_btn(nav, "▶", next_day).pack(side=tk.RIGHT)

        # 图表区：左侧环形图 + 右侧图例
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))

        donut_canvas = tk.Canvas(chart_frame, width=160, height=160,
                                 bg=Design.BACKGROUND, highlightthickness=0)
        donut_canvas.pack(side=tk.LEFT)

        legend_frame = tk.Frame(chart_frame, bg=Design.BACKGROUND)
        legend_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        # 表头
        header = tk.Frame(root, bg=Design.BACKGROUND)
        header.pack(fill=tk.X, padx=16)

        cols = [("模型", 22, 'w'), ("请求数", 9, 'e'), ("总token", 11, 'e'), ("缓存读", 11, 'e')]
        for text, width, anchor in cols:
            tk.Label(header, text=text, width=width, anchor=anchor,
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        tk.Frame(root, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, padx=16, pady=4)

        # 数据滚动区
        list_container = tk.Frame(root, bg=Design.BACKGROUND)
        list_container.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        scrollbar = tk.Scrollbar(list_container, orient="vertical")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        data_frame = tk.Frame(list_container, bg=Design.BACKGROUND)
        data_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        canvas_scroll = tk.Canvas(data_frame, bg=Design.BACKGROUND, highlightthickness=0,
                                  yscrollcommand=scrollbar.set)
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=canvas_scroll.yview)

        inner = tk.Frame(canvas_scroll, bg=Design.BACKGROUND)
        canvas_scroll.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_configure(event):
            canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
            canvas_scroll.itemconfigure("inner", width=canvas_scroll.winfo_width())

        inner.bind("<Configure>", on_configure)
        canvas_scroll.bind("<Configure>", on_configure)

        def on_mousewheel(event):
            canvas_scroll.yview_scroll(int(-event.delta / 120), "units")

        canvas_scroll.bind("<MouseWheel>", on_mousewheel)
        inner.bind("<MouseWheel>", on_mousewheel)

        def refresh_model():
            for widget in inner.winfo_children():
                widget.destroy()
            for widget in legend_frame.winfo_children():
                widget.destroy()

            date_label.config(text=current_date.strftime("%y-%m-%d"))

            models = self.query_model_breakdown_by_day(current_days_ago)
            if not models:
                tk.Label(inner, text="暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                donut_canvas.delete("all")
                return

            total_reqs = sum(m["reqs"] for m in models)
            total_token = sum(m["total_token"] for m in models)
            total_cache = sum(m["cache_read"] for m in models)

            # 模型配色（按小时轮换，同一小时内稳定）
            colors = model_colors()

            # 环形图：跨渠道按模型合并，展示整体分布（取前 6）
            merged = {}
            for m in models:
                acc = merged.get(m["model"], (0, 0))
                merged[m["model"]] = (acc[0] + m["total_token"], acc[1] + m["cache_read"])
            merged_list = sorted(merged.items(), key=lambda kv: kv[1][0], reverse=True)

            items = []
            for idx, (model_name, (token, _)) in enumerate(merged_list[:6]):
                items.append((token, colors[idx % len(colors)], model_name))

            ChartCanvas.draw_donut(donut_canvas, items, 160)

            # 图例
            for idx, (model_name, (token, _)) in enumerate(merged_list[:6]):
                color = colors[idx % len(colors)]
                pct = (token / total_token * 100) if total_token > 0 else 0
                short_name = model_name[:16] + "…" if len(model_name) > 16 else model_name

                item_frame = tk.Frame(legend_frame, bg=Design.BACKGROUND)
                item_frame.pack(fill=tk.X, pady=2)

                # 颜色圆点
                dot = tk.Canvas(item_frame, width=10, height=10,
                                bg=Design.BACKGROUND, highlightthickness=0)
                dot.create_oval(1, 1, 9, 9, fill=color, outline="")
                dot.pack(side=tk.LEFT)

                tk.Label(item_frame, text=short_name, fg=Design.TEXT_PRIMARY,
                         bg=Design.BACKGROUND, font=Design.FONT_UI_TINY,
                         anchor='w').pack(side=tk.LEFT, padx=(4, 0))

                tk.Label(item_frame, text=f"{pct:.1f}%", fg=Design.TEXT_SECONDARY,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_TINY,
                         anchor='e').pack(side=tk.RIGHT)

            # 表格按渠道分组（渠道按各自总量降序）
            group_order = []
            groups = {}
            for m in models:
                if m["source"] not in groups:
                    group_order.append(m["source"])
                    groups[m["source"]] = []
                groups[m["source"]].append(m)
            group_order.sort(key=lambda src: -sum(m["total_token"] for m in groups[src]))

            for gi, src in enumerate(group_order):
                src_models = groups[src]
                src_token = sum(m["total_token"] for m in src_models)

                # 渠道小节头：渠道名 + 该渠道总 Token
                head = tk.Frame(inner, bg=Design.BACKGROUND)
                head.pack(fill=tk.X)
                tk.Label(head, text=f"● {self.store.source_display_name(src)}",
                         width=20, anchor='w', fg=Design.BRAND, bg=Design.BACKGROUND,
                         font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
                tk.Label(head, text=Design.fmt_tokens(src_token), width=31, anchor='e',
                         fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=2)

                # 该渠道的每个模型（带专属颜色圆点）
                for idx, m in enumerate(src_models):
                    row = tk.Frame(inner, bg=Design.BACKGROUND)
                    row.pack(fill=tk.X, pady=1)

                    # 颜色圆点（前6个有色，其余灰色）
                    color = (colors[idx % len(colors)]
                             if gi == 0 and idx < 6 else Design.TEXT_MUTED)
                    dot = tk.Canvas(row, width=10, height=10,
                                    bg=Design.BACKGROUND, highlightthickness=0)
                    dot.create_oval(1, 1, 9, 9, fill=color, outline="")
                    dot.pack(side=tk.LEFT, padx=(0, 4))

                short_name = m["model"][:18] + "…" if len(m["model"]) > 18 else m["model"]

                tk.Label(row, text=short_name, width=20, anchor='w', fg=Design.TEXT_PRIMARY,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                tk.Label(row, text=f"{m['reqs']}次", width=9, anchor='e',
                         fg=Design.TEXT_PRIMARY if m['reqs'] > 0 else Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                tk.Label(row, text=Design.fmt_tokens(m['total_token']), width=11, anchor='e',
                         fg=Design.TEXT_PRIMARY if m['total_token'] > 0 else Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                tk.Label(row, text=Design.fmt_tokens(m['cache_read']), width=11, anchor='e',
                         fg=Design.TEXT_SECONDARY if m['cache_read'] > 0 else Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        refresh_model()
        self._bring_to_front(root)

    @_on_gui
    def show_daily_detail(self, days=7, title="近7天用量"):
        """显示每日详情窗口（7天/30天）"""
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title(title)
        root.geometry("560x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(480, 400)

        current_date = datetime.now()

        # 顶部导航栏
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def nav_btn(parent, text, cmd):
            return tk.Button(parent, text=text, command=cmd,
                             bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             activebackground=Design.CARD_BORDER,
                             activeforeground=Design.TEXT_PRIMARY,
                             relief="flat", bd=0, width=3,
                             font=Design.FONT_UI_SMALL, cursor="hand2")

        def prev_period():
            nonlocal current_date
            if days == 7:
                current_date -= timedelta(days=7)
            else:
                if current_date.month == 1:
                    current_date = current_date.replace(year=current_date.year - 1, month=12)
                else:
                    current_date = current_date.replace(month=current_date.month - 1)
            refresh_daily()

        def next_period():
            nonlocal current_date
            if days == 7:
                if current_date + timedelta(days=7) <= datetime.now():
                    current_date += timedelta(days=7)
            else:
                if current_date.month == 12:
                    next_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    next_date = current_date.replace(month=current_date.month + 1)
                if next_date <= datetime.now():
                    current_date = next_date
            refresh_daily()

        nav_btn(nav, "◀", prev_period).pack(side=tk.LEFT)

        date_label = tk.Label(nav, text="", font=Design.FONT_MONO,
                              fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)

        nav_btn(nav, "▶", next_period).pack(side=tk.RIGHT)

        # 折线图
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))

        chart_canvas = tk.Canvas(chart_frame, height=70, bg=Design.BACKGROUND,
                                 highlightthickness=0)
        chart_canvas.pack(fill=tk.X)

        # 折线图状态：理由同每小时详情，等布局完成有宽度再画
        chart_state = {}

        def draw_chart(_event=None):
            if not chart_state:
                chart_canvas.delete("all")
                return
            width = chart_canvas.winfo_width()
            if width <= 1:
                return
            ChartCanvas.draw_sparkline(chart_canvas, chart_state["values"], width, 70,
                                       use_gradient=True,
                                       hue_offset=chart_state["hue_offset"])

        chart_canvas.bind("<Configure>", draw_chart)

        # 表头
        header = tk.Frame(root, bg=Design.BACKGROUND)
        header.pack(fill=tk.X, padx=16)

        cols = [("日期", 8, 'w'), ("请求数", 9, 'e'), ("总token", 11, 'e'), ("缓存读", 11, 'e')]
        for text, width, anchor in cols:
            tk.Label(header, text=text, width=width, anchor=anchor,
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        tk.Frame(root, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, padx=16, pady=4)

        # 数据滚动区
        list_container = tk.Frame(root, bg=Design.BACKGROUND)
        list_container.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        scrollbar = tk.Scrollbar(list_container, orient="vertical")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        data_frame = tk.Frame(list_container, bg=Design.BACKGROUND)
        data_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        canvas_scroll = tk.Canvas(data_frame, bg=Design.BACKGROUND, highlightthickness=0,
                                  yscrollcommand=scrollbar.set)
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=canvas_scroll.yview)

        inner = tk.Frame(canvas_scroll, bg=Design.BACKGROUND)
        canvas_scroll.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_configure(event):
            canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
            canvas_scroll.itemconfigure("inner", width=canvas_scroll.winfo_width())

        inner.bind("<Configure>", on_configure)
        canvas_scroll.bind("<Configure>", on_configure)

        def on_mousewheel(event):
            canvas_scroll.yview_scroll(int(-event.delta / 120), "units")

        canvas_scroll.bind("<MouseWheel>", on_mousewheel)
        inner.bind("<MouseWheel>", on_mousewheel)

        def refresh_daily():
            for widget in inner.winfo_children():
                widget.destroy()

            if days == 7:
                weekday = current_date.weekday()
                week_start = current_date - timedelta(days=weekday)
                week_end = week_start + timedelta(days=6)
                start_date = week_start.strftime("%Y-%m-%d")
                end_date = week_end.strftime("%Y-%m-%d")
                date_label.config(text=f"{week_start.strftime('%y-%m-%d')} ~ {week_end.strftime('%y-%m-%d')}")
                hue_offset = (week_start.isocalendar()[1] % 8) / 8.0
            else:
                month_start = current_date.replace(day=1)
                if current_date.month == 12:
                    month_end = current_date.replace(year=current_date.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    month_end = current_date.replace(month=current_date.month + 1, day=1) - timedelta(days=1)
                start_date = month_start.strftime("%Y-%m-%d")
                end_date = min(month_end, datetime.now()).strftime("%Y-%m-%d")
                date_label.config(text=current_date.strftime("%y-%m"))
                hue_offset = ((current_date.month * 3) % 8) / 8.0

            daily_data = self.query_daily_stats_for_range(start_date, end_date)
            if not daily_data:
                tk.Label(inner, text="暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                chart_state.clear()
                chart_canvas.delete("all")
                return

            sorted_dates = sorted(daily_data.keys())

            total_reqs = sum(d["reqs"] for d in daily_data.values())
            total_token = sum(d["output"] + d["input"] + d["cache_read"] for d in daily_data.values())
            total_cache = sum(d["cache_read"] for d in daily_data.values())

            # 绘制折线图
            values = [d["output"] + d["input"] + d["cache_read"] for d in daily_data.values()]
            chart_state.clear()
            chart_state.update(values=values, hue_offset=hue_offset)
            draw_chart()

            # 合计行
            total_row = tk.Frame(inner, bg=Design.BACKGROUND)
            total_row.pack(fill=tk.X)
            cells = [("合计", 8, 'w'), (f"{total_reqs}次", 9, 'e'),
                     (Design.fmt_tokens(total_token), 11, 'e'),
                     (Design.fmt_tokens(total_cache), 11, 'e')]
            for text, width, anchor in cells:
                tk.Label(total_row, text=text, width=width, anchor=anchor,
                         fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            # 每日数据
            for date_str in sorted_dates:
                d = daily_data[date_str]
                day_token = d["output"] + d["input"] + d["cache_read"]
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                display_date = date_obj.strftime("%m/%d")

                row = tk.Frame(inner, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, pady=1)

                vals = [
                    (display_date, 8, 'w', Design.GRADIENT[0]),
                    (f"{d['reqs']}次" if d['reqs'] > 0 else "-", 9, 'e',
                     Design.TEXT_PRIMARY if d['reqs'] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(day_token), 11, 'e',
                     Design.TEXT_PRIMARY if day_token > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(d['cache_read']), 11, 'e',
                     Design.TEXT_SECONDARY if d['cache_read'] > 0 else Design.TEXT_MUTED),
                ]
                for text, width, anchor, fg in vals:
                    tk.Label(row, text=text, width=width, anchor=anchor, fg=fg,
                             bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)

        refresh_daily()
        self._bring_to_front(root)

    def copy_stats(self, icon, item):
        """复制今日统计"""
        import pyperclip

        today = self.query_day_stats(0)
        models = self.query_model_breakdown()

        text = "ccBar 今日用量统计\n"
        text += "==================\n"

        if today:
            text += f"Token 总量: {self.fmt_tokens(today['total'])}\n"
            text += f"请求数量: {today['reqs']}\n"
            text += f"输入 Token: {self.fmt_tokens(today['input'])}\n"
            text += f"输出 Token: {self.fmt_tokens(today['output'])}\n"

        sources = self.query_source_breakdown()
        if len(sources) > 1:
            text += "\n数据源分布:\n"
            for src in sources:
                text += f"  {self.store.source_display_name(src['source'])}: {self.fmt_tokens(src['total'])}\n"

        if models:
            text += "\n模型分布:\n"
            for m in models:
                text += f"  {m['model']}: {self.fmt_tokens(m['total'])}\n"

        try:
            pyperclip.copy(text)
            toaster = win10toast.ToastNotifier()
            toaster.show_toast("已复制", "统计数据已复制到剪贴板", duration=3)
        except:
            pass

    def refresh_data(self, icon, item):
        """刷新数据"""
        if self.icon:
            menu = pystray.Menu(*self.build_menu())
            self.icon.menu = menu

    # ------------------------------------------------------------ 洞察中心

    @staticmethod
    def _pil_font(size, bold=False):
        """PIL 中文字体：微软雅黑优先，逐级回落"""
        from PIL import ImageFont
        for path in (["C:/Windows/Fonts/msyhbd.ttc"] if bold else []) + [
                "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"]:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _bar_chart_png(series, colors, width=680, height=150, budget_line=None,
                       value_fmt=None):
        """深底堆叠柱状图。series: [(label, {名称: 值})]，colors: {名称: "#hex"}

        budget_line 有值时按同一纵轴画一条日预算虚线并标注（费用页用）；
        value_fmt 有值时纵轴极值按它格式化（费用图传 money，token 图走默认万/亿）。
        """
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), "#1E1E1E")
        d = ImageDraw.Draw(img)
        if not series:
            d.text((width // 2, height // 2), "暂无数据", fill="#6B6B6B",
                   anchor="mm", font=CcBarTray._pil_font(13))
            return img
        top, bottom = 12, height - 22
        names = list(colors.keys())
        totals = [sum(vals.get(n, 0) for n in names) for _, vals in series]
        # 预算线也要参与纵轴取最大值，否则线会画到图外看不见
        maxv = max(totals + ([budget_line] if budget_line else [])) or 1
        slot = width / len(series)
        bw = max(slot * 0.62, 2)
        for i, (label, vals) in enumerate(series):
            y = bottom
            for n in names:
                v = vals.get(n, 0)
                if v <= 0:
                    continue
                h = v / maxv * (bottom - top)
                d.rectangle([i * slot + (slot - bw) / 2, y - h, i * slot + (slot + bw) / 2, y],
                            fill=colors[n])
                y -= h
        d.line([(0, bottom), (width, bottom)], fill="#3D3D3D")
        if budget_line and budget_line > 0:
            # PIL 没有虚线 API，按「4 实 3 虚」逐段画（与 mac 的 dash [4,3] 同款）
            y = bottom - (budget_line / maxv) * (bottom - top)
            x = 0
            while x < width:
                d.line([(x, y), (min(x + 4, width), y)], fill=Design.WARNING)
                x += 7
            label = "日预算 " + (value_fmt(budget_line) if value_fmt else "%.2f" % budget_line)
            d.text((width - 4, y - 13 if y - 13 > 0 else y + 3), label,
                   fill=Design.WARNING, anchor="ra", font=CcBarTray._pil_font(10))
        d.text((4, 2), (value_fmt(maxv) if value_fmt else CcBarTray.fmt_tokens_static(maxv)),
               fill="#6B6B6B", font=CcBarTray._pil_font(11))
        if series:
            d.text((4, height - 16), series[0][0], fill="#6B6B6B", font=CcBarTray._pil_font(10))
            d.text((width - 4, height - 16), series[-1][0], fill="#6B6B6B",
                   anchor="ra", font=CcBarTray._pil_font(10))
        return img

    @staticmethod
    def _line_chart_png(points, color, width=680, height=120, suffix=""):
        """深底折线图。points: [(label, value)]"""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), "#1E1E1E")
        d = ImageDraw.Draw(img)
        if len(points) < 2:
            d.text((width // 2, height // 2), "暂无数据", fill="#6B6B6B",
                   anchor="mm", font=CcBarTray._pil_font(13))
            return img
        top, bottom = 12, height - 20
        maxv = max(v for _, v in points) or 1
        step = (width - 16) / (len(points) - 1)
        pts = [(8 + i * step, bottom - (v / maxv) * (bottom - top)) for i, (_, v) in enumerate(points)]
        d.line(pts, fill=color, width=2)
        d.text((width - 4, 2), f"{maxv:.0f}{suffix}", fill="#6B6B6B", anchor="ra",
               font=CcBarTray._pil_font(11))
        return img

    @staticmethod
    def fmt_tokens_static(tokens):
        """静态格式化（洞察图表用）：中文单位"""
        if tokens >= 100_000_000:
            return f"{tokens / 100_000_000:.2f}亿"
        if tokens >= 10_000:
            return f"{tokens // 10_000}万"
        return f"{tokens}"

    @staticmethod
    def _fmt_credits(v):
        """积分显示：整数不带小数点，小数保留两位（与 mac formatCredits 同口径）"""
        v = float(v or 0)
        if v == round(v) and abs(v) < 100_000:
            return str(int(v))
        return "%.2f" % v

    @staticmethod
    def _short_date(epoch):
        """短日期：同年 MM-dd，跨年 yy-MM-dd（与 mac ModelMilestone 的 shortDate 一致）"""
        try:
            d = datetime.fromtimestamp(int(epoch))
        except (TypeError, ValueError, OverflowError, OSError):
            return "-"
        return d.strftime("%m-%d") if d.year == datetime.now().year else d.strftime("%y-%m-%d")

    @staticmethod
    def _session_stats(rows):
        """今日会话统计：相邻请求间隔 > 30 分钟切新会话 → (会话数, 平均分钟, 最长分钟)。

        与 macOS 版 InsightsViewModel.sessionStats 同口径：按时间升序，
        超过 30 分钟算新会话，时长 = 会话内「末次 - 首次」。
        """
        times = sorted(r[0] for r in rows if r and r[0])
        if not times:
            return (0, 0, 0)
        durations = []
        start = last = times[0]
        for t in times[1:]:
            if t - last > 30 * 60:
                durations.append(last - start)
                start = t
            last = t
        durations.append(last - start)
        avg = sum(durations) // max(len(durations), 1) // 60
        return (len(durations), avg, max(durations) // 60)

    def _share_card_png(self):
        """用量战报分享卡（与 macOS 版 UsageShareCard 同款版式，含二维码）"""
        today = self.query_day_stats(0) or {"total": 0}
        week = self.query_day_stats(7) or {"total": 0}
        month = self.query_day_stats(30) or {"total": 0}
        total = self.query_total_stats() or {"total": 0}
        trend = [(d[5:], t) for d, t in (self.store.query_daily_tokens(7) or [])]
        try:
            credits = self.store.query_today_credits()
        except Exception:
            credits = 0.0
        return share_card.share_card_image(
            today["total"], week["total"], month["total"], total["total"], trend,
            today_credits=credits, theme=self.theme)

    def _weekly_card_png(self):
        """用量周报卡（上一个完整周：上周一 ~ 上周日，与 mac weeklyShareCard 同字段）"""
        now = datetime.now()
        frm, to = weekly_report.last_week_window(now)
        stats = self.store.query_window_stats(frm, to) or {}
        daily = self.store.query_daily_tokens_between(frm, to) or []
        peak = max((t for _, t in daily), default=0)
        labels = weekly_report.trend_labels(now)
        trend = [(labels[i] if i < len(labels) else d, t) for i, (d, t) in enumerate(daily)]
        return share_card.weekly_card_image(
            weekly_report.date_range_text(now), int(stats.get("total", 0) or 0),
            int(stats.get("reqs", 0) or 0), peak, trend, theme=self.theme)

    @staticmethod
    def _insight_card(parent, row, col, title, value, sub=""):
        """洞察页统计卡（两列网格中的一项）"""
        import tkinter as tk

        card = tk.Frame(parent, bg=Design.CARD_FILL, highlightbackground=Design.CARD_BORDER,
                        highlightthickness=1, padx=14, pady=10)
        card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
        tk.Label(card, text=title, fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                 font=("Microsoft YaHei UI", 10), anchor='w').pack(fill=tk.X)
        tk.Label(card, text=value, fg=Design.BIG_NUMBER, bg=Design.CARD_FILL,
                 font=("Microsoft YaHei UI", 17, "bold"), anchor='w').pack(fill=tk.X)
        if sub:
            tk.Label(card, text=sub, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)

    @_on_gui
    def show_insights(self, icon=None, item=None):
        """洞察中心：费用 / 洞察 / 分享 / 渠道 / 流水 五页（与 macOS 版同口径）"""
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        store = self.store

        # ------------------------------------------------ 数据一次全查
        # 全部走索引区间，毫秒级；一次全查省得各页互相清数据（mac InsightsViewModel 同款）
        cost_today, cost7, cost30 = store.query_cost(0), store.query_cost(7), store.query_cost(30)
        cost_daily = store.query_cost_daily(30)
        cost_models = store.query_cost_by_model(30)
        streak = store.query_streak()
        this_week, last_week = store.query_weekly_delta()
        peak = store.query_peak_day(30)
        top = store.query_top_model(30)
        mtd, elapsed, dim = store.query_month_progress()
        projected = mtd // elapsed * dim if elapsed else 0
        total_all = (self.query_total_stats() or {"total": 0})["total"]
        channels = store.query_channel_daily(30)
        apps = store.query_app_daily(30)
        comp = store.query_composition_daily(30)
        today_timeline = store.query_timeline()
        # 月度预算（设置里 > 0 才出面）；未计费渠道按默认单价（$/M tokens）估算
        budget = float(self.settings.get("monthly_budget_usd") or 0)
        price = float(self.settings.get("default_token_price") or 0)
        cost_mtd, mtd_elapsed, month_days = store.query_cost_mtd()
        daily_budget = budget / max(month_days, 1)
        est = {0: 0.0, 7: 0.0, 30: 0.0}
        if price > 0:
            for days in (0, 7, 30):
                est[days] = store.query_unmetered_tokens(days) / 1_000_000 * price
        # 星期分布（近 91 天，周一..周日）：Python 的 weekday() 本来就是周一=0，
        # 等价于 mac 的 (Calendar.weekday + 5) % 7
        tokens91 = store.query_daily_tokens(91)
        weekday_totals = [0] * 7
        for date_text, token in tokens91:
            try:
                weekday_totals[datetime.strptime(date_text, "%Y-%m-%d").weekday()] += token
            except (TypeError, ValueError):
                continue
        # 今日会话（相邻请求间隔 > 30 分钟切新会话，与 mac sessionStats 一致）
        session_count, session_avg, session_longest = self._session_stats(today_timeline)
        # 渠道 30 天合计（导出长图用）
        channel_totals = {}
        for _day, src, tok in channels:
            channel_totals[src] = channel_totals.get(src, 0) + tok
        top_channels = sorted(channel_totals.items(), key=lambda kv: kv[1], reverse=True)

        root = tk.Toplevel(self._ui_root)
        root.title("洞察中心")
        root.geometry("760x600")
        root.minsize(700, 480)
        root.configure(bg=Design.BACKGROUND)

        style = ttk.Style(root)
        style.theme_use("default")
        style.configure("TNotebook", background=Design.BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", background=Design.CARD_FILL, foreground=Design.TEXT_PRIMARY,
                        padding=(18, 7), font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab", background=[("selected", Design.BRAND)])
        style.configure("Insights.Treeview", background=Design.CARD_FILL, fieldbackground=Design.CARD_FILL,
                        foreground=Design.TEXT_PRIMARY, rowheight=24, borderwidth=0)
        style.configure("Insights.Treeview.Heading", background=Design.BACKGROUND,
                        foreground=Design.TEXT_MUTED, borderwidth=0)
        style.map("Insights.Treeview", background=[("selected", Design.CARD_BORDER)])
        style.configure("Insights.TCombobox", fieldbackground=Design.CARD_FILL,
                        background=Design.CARD_FILL, foreground=Design.TEXT_PRIMARY)

        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 12))

        def tab(title):
            # 注意：必须显式 nb.add()——把 Frame pack 进 Notebook 不会自动生成页签
            f = tk.Frame(nb, bg=Design.BACKGROUND)
            nb.add(f, text=title)
            return f

        def png_label(frame, img):
            lbl = tk.Label(frame, bg=Design.CARD_FILL, highlightbackground=Design.CARD_BORDER,
                           highlightthickness=1)
            photo = ImageTk.PhotoImage(img)
            lbl.configure(image=photo)
            lbl.image = photo
            lbl.pack(fill=tk.X, padx=16, pady=(10, 4))
            return lbl

        def scroll_page(parent):
            """可滚动页：Canvas + 内嵌 Frame（内容比窗口高时的兜底）。

            返回 (内嵌 Frame, 滚轮绑定函数)；滚轮绑定要等内容建完再调用。
            """
            canvas = tk.Canvas(parent, bg=Design.BACKGROUND, highlightthickness=0, bd=0)
            vsb = tk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
            inner = tk.Frame(canvas, bg=Design.BACKGROUND)
            inner_id = canvas.create_window((0, 0), window=inner, anchor='nw')
            inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)

            def bind_wheel(widget):
                # 递归绑到页内所有控件：不用 bind_all，免得和设置页抢全局滚轮绑定
                widget.bind("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
                for child in widget.winfo_children():
                    bind_wheel(child)

            return inner, bind_wheel

        def section(parent, text):
            tk.Label(parent, text=text, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10, "bold")).pack(anchor='w', padx=16, pady=(12, 2))

        def muted(parent, text):
            tk.Label(parent, text=text, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X, padx=20, pady=2)

        def btn(parent, text, cmd, primary=False):
            return tk.Button(parent, text=text, command=cmd,
                             bg=Design.BRAND if primary else Design.CARD_FILL,
                             fg=Design.TEXT_PRIMARY, activebackground=Design.BTN_BG_HOVER,
                             activeforeground=Design.TEXT_PRIMARY, relief="flat", bd=0,
                             padx=12, pady=4, cursor="hand2", font=Design.FONT_UI_SMALL)

        money = lambda v: f"${v:.2f}"

        # ============ 费用 ============
        f_cost = tab("费用")
        cost_page, cost_wheel = scroll_page(f_cost)

        card_row = tk.Frame(cost_page, bg=Design.BACKGROUND)
        card_row.pack(fill=tk.X, padx=10, pady=(8, 0))
        for i, (title, value, estimate) in enumerate(
                [("今日费用", cost_today, est[0]), ("近 7 天", cost7, est[7]),
                 ("近 30 天", cost30, est[30])]):
            card = tk.Frame(card_row, bg=Design.CARD_FILL, highlightbackground=Design.CARD_BORDER,
                            highlightthickness=1, padx=16, pady=12)
            card.grid(row=0, column=i, sticky="nsew", padx=6, pady=4)
            card_row.grid_columnconfigure(i, weight=1)
            tk.Label(card, text=title, fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                     font=("Microsoft YaHei UI", 10), anchor='w').pack(fill=tk.X)
            tk.Label(card, text=money(value + estimate), fg=Design.BIG_NUMBER, bg=Design.CARD_FILL,
                     font=("Microsoft YaHei UI", 19, "bold"), anchor='w').pack(fill=tk.X)
            if estimate > 0:
                # mac 版卡片小字：实测 $a · 估算 $b
                tk.Label(card, text="实测 %s · 估算 %s" % (money(value), money(estimate)),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL, font=Design.FONT_MONO_TINY,
                         anchor='w').pack(fill=tk.X)

        if budget > 0:
            # 月度预算卡（mac CostPage.budgetCard 同款文案与字段）
            ratio = cost_mtd / budget
            over = cost_mtd > budget
            projected_cost = cost_mtd / mtd_elapsed * month_days if mtd_elapsed else 0.0
            bcard = tk.Frame(cost_page, bg=Design.CARD_FILL, highlightbackground=Design.CARD_BORDER,
                             highlightthickness=1, padx=16, pady=12)
            bcard.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(bcard, text="本月预算 $%.2f" % budget, fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=("Microsoft YaHei UI", 11, "bold"),
                     anchor='w').pack(fill=tk.X)
            bar = tk.Frame(bcard, bg=Design.BTN_BG, height=6)
            bar.pack(fill=tk.X, pady=(8, 6))
            bar.pack_propagate(False)
            fill_ratio = min(max(ratio, 0.0), 1.0)
            tk.Frame(bar, bg=usage_color(fill_ratio)).place(relwidth=fill_ratio, relheight=1.0)
            brow = tk.Frame(bcard, bg=Design.CARD_FILL)
            brow.pack(fill=tk.X)
            tk.Label(brow, text="本月已花 " + money(cost_mtd), fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
            tk.Label(brow,
                     text=("已超预算 " + money(cost_mtd - budget)) if over
                     else ("剩余 " + money(budget - cost_mtd)),
                     fg=Design.ERROR if over else Design.SUCCESS, bg=Design.CARD_FILL,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
            tk.Label(bcard,
                     text="按当前速率预计 %s · 已用预算 %.0f%%" % (money(projected_cost), ratio * 100),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL,
                     anchor='w').pack(fill=tk.X, pady=(4, 0))

        section(cost_page, "近 30 天费用走势")
        cost_series = [(d[5:], {"费用": c}) for d, c in cost_daily]
        png_label(cost_page, self._bar_chart_png(
            cost_series, {"费用": Design.BRAND},
            budget_line=daily_budget if budget > 0 else None, value_fmt=money))

        section(cost_page, "模型费用排行（近 30 天）")
        if not cost_models:
            muted(cost_page, "暂无数据")
        for m, c, tok in cost_models:
            row = tk.Frame(cost_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=m, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=f"{money(c)} · {self.fmt_tokens(tok)}",
                     fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        # 性价比榜：token / $（cost > 0.005 才参与，避免除零噪声）
        section(cost_page, "性价比榜（近 30 天）")
        ranked = sorted(((m, tok / c) for m, c, tok in cost_models if c > 0.005),
                        key=lambda kv: kv[1], reverse=True)
        if not ranked:
            muted(cost_page, "暂无数据")
        for i, (m, per_dollar) in enumerate(ranked):
            row = tk.Frame(cost_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=f"{i + 1}. {m}", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=self.fmt_tokens(int(per_dollar)) + " / $1", fg=Design.DATA,
                     bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        foot = "费用按 cc-switch 记录的单价折算；未计费渠道可在设置里配默认单价估算"
        if price > 0:
            foot += "（当前按 $%g/M tokens 估算）" % price
        tk.Label(cost_page, text=foot, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                 wraplength=690).pack(fill=tk.X, padx=16, pady=(14, 8))
        cost_wheel(cost_page)

        # ============ 洞察 ============
        f_ins = tab("洞察")
        ins_page, ins_wheel = scroll_page(f_ins)

        def export_long_image():
            """导出长图：把各页要点汇总成一张 PNG（mac「导出长图」）"""
            from tkinter import filedialog, messagebox
            blocks = [
                ("费用", [("今日费用", money(cost_today + est[0])),
                          ("近 7 天", money(cost7 + est[7])),
                          ("近 30 天", money(cost30 + est[30])),
                          ("本月已花", money(cost_mtd))]),
                ("用量", [("连续使用", "%d 天" % streak),
                          ("今日会话", "%d 个 · 平均 %d 分钟 · 最长 %d 分钟"
                           % (session_count, session_avg, session_longest)),
                          ("本周用量", self.fmt_tokens(this_week)),
                          ("历史总量", self.fmt_tokens(total_all)),
                          ("单日峰值（近 30 天）", self.fmt_tokens(peak[1]) if peak else "-"),
                          ("预计本月消耗", self.fmt_tokens(projected))]),
                ("渠道（近 30 天）", [(store.source_display_name(s), self.fmt_tokens(t))
                                      for s, t in top_channels] or [("暂无数据", "-")]),
            ]
            path = filedialog.asksaveasfilename(
                title="导出洞察长图", defaultextension=".png",
                initialfile="ccbar-insights-%s.png" % datetime.now().strftime("%Y%m%d"),
                filetypes=[("PNG", "*.png")])
            if not path:
                return
            try:
                share_card.insight_long_image(blocks, width=720, theme=self.theme).save(path)
            except Exception as e:
                messagebox.showerror("导出失败", str(e))
                return
            messagebox.showinfo("导出完成", "已保存到：\n%s" % path)

        tools = tk.Frame(ins_page, bg=Design.BACKGROUND)
        tools.pack(fill=tk.X, padx=16, pady=(8, 0))
        btn(tools, "导出长图", export_long_image).pack(side=tk.RIGHT)

        grid = tk.Frame(ins_page, bg=Design.BACKGROUND)
        grid.pack(fill=tk.X, padx=10, pady=(6, 0))
        for c in range(2):
            grid.grid_columnconfigure(c, weight=1)
        delta_txt = ""
        if last_week > 0:
            delta_txt = f"{(this_week - last_week) / last_week * 100:+.0f}%"
        peak_h = peak[0] if peak else "-"
        peak_v = self.fmt_tokens(peak[1]) if peak else "-"
        hour_rows = store.query_one("""
            SELECT ((created_at + ?) % 86400) / 3600 AS h, SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens)
            FROM usage_all WHERE created_at >= ? AND created_at < ? GROUP BY h ORDER BY 2 DESC LIMIT 1""",
            (int(datetime.now().utcoffset().total_seconds()) if datetime.now().utcoffset() else 28800,
             store.local_midnight(30), store.local_midnight(-1)))
        peak_hour_txt = f"{hour_rows[0]:02d}:00 – {(hour_rows[0] + 1) % 24:02d}:00" if hour_rows else "-"
        cards = [
            ("🔥 连续使用", f"{streak} 天", ""),
            ("今日会话", f"{session_count} 个",
             "平均 %d 分钟 · 最长 %d 分钟" % (session_avg, session_longest)),
            ("本周用量", self.fmt_tokens(this_week), delta_txt),
            ("日均用量（近 30 天）",
             self.fmt_tokens((self.query_day_stats(30) or {"total": 0})["total"] // 30), ""),
            ("历史总量", self.fmt_tokens(total_all), ""),
            ("单日峰值（近 30 天）", peak_v, peak_h),
            ("最活跃时段（近 30 天）", peak_hour_txt, ""),
            ("使用量最大的模型（近 30 天）", self.fmt_tokens(top[1]) if top else "-",
             top[0] if top else ""),
            ("预计本月消耗", self.fmt_tokens(projected),
             f"按当前速率 · 本月已用 {self.fmt_tokens(mtd)}"),
        ]
        for i, (t, v, sub) in enumerate(cards):
            self._insight_card(grid, i // 2, i % 2, t, v, sub)

        # 星期分布（近 90 天）
        section(ins_page, "星期分布（近 90 天）")
        weekday_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        if not any(weekday_totals):
            muted(ins_page, "暂无数据")
        else:
            wd_series = [(weekday_labels[i], {weekday_labels[i]: weekday_totals[i]})
                         for i in range(7)]
            wd_colors = {weekday_labels[i]: Design.MODEL_COLORS[i % len(Design.MODEL_COLORS)]
                         for i in range(7)}
            png_label(ins_page, self._bar_chart_png(wd_series, wd_colors))

        # 近 90 天用量热力图（7 行 × 周数，首列按起始日 weekday 补位）
        section(ins_page, "近 90 天用量热力图")
        start_day = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                     - timedelta(days=90))
        pad = start_day.weekday()
        by_date = {}
        for date_text, token in tokens91:
            by_date[date_text] = token
        cells = [("", -1)] * pad
        for i in range(91):
            key = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
            cells.append((key, by_date.get(key, 0)))
        max_token = max((t for _, t in cells), default=0) or 1
        weeks = (len(cells) + 6) // 7
        cell_size, gap = 14, 3
        canvas_w = max(weeks * (cell_size + gap) + 12, 120)
        heat = tk.Canvas(ins_page, bg=Design.CARD_FILL, highlightthickness=1,
                         highlightbackground=Design.CARD_BORDER, width=canvas_w,
                         height=7 * (cell_size + gap) + 10)
        heat.pack(fill=tk.X, padx=16, pady=(4, 6))
        tip = tk.Label(heat, text="", bg=Design.BTN_BG, fg=Design.TEXT_PRIMARY,
                       font=Design.FONT_UI_TINY, padx=6, pady=2)

        def on_leave(_event=None):
            tip.place_forget()

        def make_enter(date_text, token, cx, cy):
            def enter(_event=None):
                tip.configure(text="%s · %s" % (date_text, self.fmt_tokens(token)))
                tip.place(x=min(cx + 12, max(canvas_w - 160, 12)), y=max(cy - 26, 2))
            return enter

        for idx, (date_text, token) in enumerate(cells):
            row, col = idx % 7, idx // 7
            x1 = 6 + col * (cell_size + gap)
            y1 = 5 + row * (cell_size + gap)
            if token < 0:
                color = blend("#FFFFFF", Design.CARD_FILL, 0.04)      # 首周补位格
            elif token == 0:
                color = blend("#FFFFFF", Design.CARD_FILL, 0.08)      # 无用量
            else:
                color = usage_color(min(token / max_token, 1.0))
            tag = "cell%d" % idx
            ChartCanvas.round_rect(heat, x1, y1, x1 + cell_size, y1 + cell_size, 2,
                                   fill=color, outline="", tags=tag)
            if date_text:
                heat.tag_bind(tag, "<Enter>", make_enter(date_text, token, x1, y1))
                heat.tag_bind(tag, "<Leave>", on_leave)

        # 模型编年史（默认前 10 行，可展开/收起；模型治理菜单）
        section(ins_page, "模型编年史")
        chron_wrap = tk.Frame(ins_page, bg=Design.BACKGROUND)
        chron_wrap.pack(fill=tk.X, padx=16, pady=(4, 4))
        chron_state = {"expanded": False}

        def auto_merge_models():
            from tkinter import messagebox
            groups, changed = store.auto_merge_models()
            render_chronicle()
            if groups == 0:
                messagebox.showinfo("没有需要合并的模型", "大小写、厂商前缀不同的同名模型都已一致")
            else:
                messagebox.showinfo("合并完成", "已合并 %d 组 · 改写 %d 行明细" % (groups, changed))

        def manual_merge_models():
            from tkinter import messagebox
            names = [m for m, _f, _l, _t in (store.query_model_history() or [])]
            if len(names) < 2:
                messagebox.showinfo("手动合并模型", "模型不足两个，无需合并")
                return
            dlg = tk.Toplevel(root)
            dlg.title("手动合并模型")
            dlg.configure(bg=Design.BACKGROUND)
            dlg.geometry("470x230")
            dlg.transient(root)
            tk.Label(dlg, text="手动合并模型", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor='w', padx=18, pady=(16, 8))
            from_var = tk.StringVar(value=names[0])
            to_var = tk.StringVar(value=names[0])
            for label, var in (("从", from_var), ("合并到", to_var)):
                row = tk.Frame(dlg, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, padx=18, pady=3)
                tk.Label(row, text=label, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         width=6, anchor='w', font=Design.FONT_UI).pack(side=tk.LEFT)
                ttk.Combobox(row, textvariable=var, values=names, state="readonly",
                             width=32, style="Insights.TCombobox").pack(side=tk.LEFT)
            tk.Label(dlg, text="「从」模型的所有明细行会并入「到」模型，操作不可撤销",
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_UI_SMALL).pack(anchor='w', padx=18, pady=(8, 0))
            btns = tk.Frame(dlg, bg=Design.BACKGROUND)
            btns.pack(fill=tk.X, padx=18, pady=14)

            def do_merge():
                frm, to = from_var.get(), to_var.get()
                if not frm or frm == to:
                    return
                changed = store.merge_model(frm, to)
                dlg.destroy()
                render_chronicle()
                messagebox.showinfo("合并完成", "已改写 %d 行明细" % changed)

            btn(btns, "取消", dlg.destroy).pack(side=tk.RIGHT)
            btn(btns, "合并", do_merge, primary=True).pack(side=tk.RIGHT, padx=(0, 8))

        def render_chronicle():
            for w in chron_wrap.winfo_children():
                w.destroy()
            history = store.query_model_history() or []
            if not history:
                muted(chron_wrap, "暂无数据")
                return
            shown = history if chron_state["expanded"] else history[:10]
            for model, first_epoch, last_epoch, token in shown:
                row = tk.Frame(chron_wrap, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, pady=1)
                tk.Label(row, text=model or "-", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=("Microsoft YaHei UI", 10), anchor='w').pack(side=tk.LEFT)
                tk.Label(row, text="%s → %s" % (self._short_date(first_epoch),
                                                self._short_date(last_epoch)),
                         fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                         font=Design.FONT_MONO_TINY).pack(side=tk.LEFT, padx=(8, 0))
                tk.Label(row, text=self.fmt_tokens(token), fg=Design.DATA, bg=Design.BACKGROUND,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

            def toggle():
                chron_state["expanded"] = not chron_state["expanded"]
                render_chronicle()

            if len(history) > 10:
                tk.Button(chron_wrap,
                          text=("收起" if chron_state["expanded"]
                                else "展开全部 %d 个模型" % len(history)),
                          command=toggle, bg=Design.BACKGROUND, fg=Design.BRAND,
                          activebackground=Design.BACKGROUND, activeforeground=Design.BRAND,
                          relief="flat", bd=0, cursor="hand2",
                          font=Design.FONT_UI_SMALL).pack(anchor='w', pady=(4, 0))
            menu_row = tk.Frame(chron_wrap, bg=Design.BACKGROUND)
            menu_row.pack(fill=tk.X, pady=(6, 0))
            mb = tk.Menubutton(menu_row, text="整理模型 ▾", bg=Design.CARD_FILL,
                               fg=Design.TEXT_PRIMARY, activebackground=Design.BTN_BG_HOVER,
                               activeforeground=Design.TEXT_PRIMARY, relief="flat", bd=0,
                               cursor="hand2", font=Design.FONT_UI_SMALL, padx=10, pady=3)
            menu = tk.Menu(mb, tearoff=0, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                           activebackground=Design.BRAND, activeforeground=Design.TEXT_PRIMARY,
                           font=Design.FONT_UI_SMALL)
            menu.add_command(label="自动合并同名模型", command=auto_merge_models)
            menu.add_command(label="手动合并…", command=manual_merge_models)
            mb.configure(menu=menu)
            mb.pack(side=tk.RIGHT)
            ins_wheel(chron_wrap)

        render_chronicle()
        ins_wheel(ins_page)

        # ============ 分享 ============
        f_share = tab("分享")
        share_page, share_wheel = scroll_page(f_share)

        def save_card_image(factory, prefix):
            from tkinter import filedialog, messagebox
            path = filedialog.asksaveasfilename(
                title="保存为图片", defaultextension=".png",
                initialfile="%s-%s.png" % (prefix, datetime.now().strftime("%Y%m%d")),
                filetypes=[("PNG", "*.png")])
            if not path:
                return
            try:
                factory().save(path)
            except Exception as e:
                messagebox.showerror("保存失败", str(e))
                return
            messagebox.showinfo("保存完成", "已保存到：\n%s" % path)

        def copy_card_image(factory, prefix):
            """复制到剪贴板：Windows 走 PowerShell Set-Clipboard，失败退回临时文件"""
            from tkinter import messagebox
            import subprocess
            import tempfile

            path = os.path.join(
                tempfile.gettempdir(),
                "%s-%s.png" % (prefix, datetime.now().strftime("%Y%m%d%H%M%S")))
            try:
                factory().save(path)
            except Exception as e:
                messagebox.showerror("复制失败", str(e))
                return
            if os.name == "nt":
                try:
                    subprocess.run(["powershell", "-c", "Set-Clipboard -Path '%s'" % path],
                                   check=True, capture_output=True, timeout=15)
                    messagebox.showinfo("已复制", "已复制到剪贴板")
                    return
                except Exception:
                    pass
            messagebox.showinfo("已复制", "剪贴板不可用，已保存到临时文件：\n%s" % path)

        # 战报卡（今日 / 近 7 天 / 近 30 天 / 累计 + 近 7 天趋势）
        png_label(share_page, self._share_card_png())
        share_btns = tk.Frame(share_page, bg=Design.BACKGROUND)
        share_btns.pack(fill=tk.X, padx=16, pady=(2, 8))
        btn(share_btns, "保存为图片",
            lambda: save_card_image(self._share_card_png, "ccbar-share"),
            primary=True).pack(side=tk.LEFT)
        btn(share_btns, "复制到剪贴板",
            lambda: copy_card_image(self._share_card_png, "ccbar-share")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(share_btns, text="晒用量就是最好的宣传 ✨", fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))

        tk.Frame(share_page, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=16, pady=6)
        wk_head = tk.Frame(share_page, bg=Design.BACKGROUND)
        wk_head.pack(fill=tk.X, padx=16, pady=(2, 0))
        tk.Label(wk_head, text="AI 用量周报（上周）", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)

        def open_weekly_dir():
            """打开周报目录（不存在则建），历史周报 PNG 都在这里"""
            import webbrowser
            path = weekly_report.directory()
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass
            try:
                os.startfile(path)          # Windows
            except AttributeError:
                webbrowser.open("file://" + path.replace("\\", "/"))

        btn(wk_head, "打开周报目录", open_weekly_dir).pack(side=tk.RIGHT)
        png_label(share_page, self._weekly_card_png())
        wk_btns = tk.Frame(share_page, bg=Design.BACKGROUND)
        wk_btns.pack(fill=tk.X, padx=16, pady=(2, 10))
        btn(wk_btns, "保存为图片",
            lambda: save_card_image(self._weekly_card_png, "ccbar-weekly"),
            primary=True).pack(side=tk.LEFT)
        btn(wk_btns, "复制到剪贴板",
            lambda: copy_card_image(self._weekly_card_png, "ccbar-weekly")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(wk_btns, text="每周一自动生成到周报目录 🗓", fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))
        share_wheel(share_page)

        # ============ 渠道 ============
        f_ch = tab("渠道")
        ch_page, ch_wheel = scroll_page(f_ch)
        section(ch_page, "近 30 天渠道用量（堆叠）")
        src_names = sorted({r[1] for r in channels})
        src_colors = {"cc-switch": "#4A90E2", "zcode": "#34C759", "trae": "#AF52DE"}
        palette = {n: src_colors.get(n, f"#{hash(n) % 0xFFFFFF:06X}") for n in src_names}
        by_date = {}
        for d, src, tok in channels:
            by_date.setdefault(d, {}).setdefault(src, 0)
            by_date[d][src] += tok
        ch_series = [(d[5:], by_date[d]) for d in sorted(by_date)]
        png_label(ch_page, self._bar_chart_png(ch_series, palette))

        section(ch_page, "今日各渠道")
        today_src = [r for r in channels if r[0] == datetime.now().strftime("%Y-%m-%d")]
        if not today_src:
            muted(ch_page, "暂无数据")
        for src, _d, tok in [(r[1], r[0], r[2]) for r in today_src]:
            row = tk.Frame(ch_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=f"● {store.source_display_name(src)}", fg=Design.BRAND,
                     bg=Design.BACKGROUND, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(row, text=self.fmt_tokens(tok), fg=Design.DATA, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        section(ch_page, "近 30 天应用分布（堆叠）")
        app_names = ["Claude Code", "Codex", "OpenCode", "ZCode"]
        app_map = {"claude": "Claude Code", "claude-desktop": "Claude Desktop", "codex": "Codex",
                   "opencode": "OpenCode", "zcode": "ZCode", "unknown": "未知"}

        def app_name(raw):
            return app_map.get(raw, raw)

        app_palette = {"Claude Code": "#4A90E2", "OpenCode": "#34C759", "ZCode": "#FF9500"}
        app_by_date = {}
        for d, raw, tok in apps:
            name = app_name(raw)
            app_by_date.setdefault(d, {}).setdefault(name, 0)
            app_by_date[d][name] += tok
        used_names = [n for n in app_names if any(n in v for v in app_by_date.values())]
        app_palette = {n: c for n, c in app_palette.items() if n in used_names}
        for n in used_names:
            app_palette.setdefault(n, f"#{(hash(n) * 7919) % 0xFFFFFF:06X}")
        app_series = [(d[5:], app_by_date[d]) for d in sorted(app_by_date)]
        png_label(ch_page, self._bar_chart_png(app_series, app_palette))

        section(ch_page, "近 30 天 Token 构成")
        comp_palette = {"输入": "#4A90E2", "输出": "#34C759", "缓存读": "#FF9500", "缓存创建": "#AF52DE"}
        comp_series = [(d, {"输入": i, "输出": o, "缓存读": cr, "缓存创建": cc})
                       for d, i, o, cr, cc in comp]
        png_label(ch_page, self._bar_chart_png(comp_series, comp_palette))

        section(ch_page, "缓存命中率（近 30 天）")
        hit_pts = [(d, cr / max(i + o + cr + cc, 1) * 100) for d, i, o, cr, cc in comp]
        png_label(ch_page, self._line_chart_png(hit_pts, Design.BRAND, suffix="%"))
        ch_wheel(ch_page)

        # ============ 流水 ============
        f_tl = tab("流水")
        cols = ("时间", "渠道", "模型", "Token", "费用")
        tree = ttk.Treeview(f_tl, columns=cols, show="headings", height=19, style="Insights.Treeview")
        widths = (90, 90, 220, 110, 90)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor='w' if c in ("渠道", "模型") else 'e')
        vsb = ttk.Scrollbar(f_tl, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=10)
        tree.pack(fill=tk.BOTH, expand=True, padx=(12, 0), pady=(6, 10))

        filter_bar = tk.Frame(f_tl, bg=Design.BACKGROUND)
        filter_bar.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(filter_bar, text="筛选", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)

        tl_state = {"day": datetime.now().date(), "rows": []}

        def is_today(day):
            return day == datetime.now().date()

        def day_label(day):
            today = datetime.now().date()
            if day == today:
                return "今天"
            if day == today - timedelta(days=1):
                return "昨天"
            return "%d月%d日" % (day.month, day.day)

        export_btn = btn(filter_bar, "导出 CSV", lambda: export_timeline_csv())
        export_btn.pack(side=tk.RIGHT)
        model_box = ttk.Combobox(filter_bar, values=["全部模型"], state="readonly",
                                 width=18, style="Insights.TCombobox")
        model_box.set("全部模型")
        model_box.pack(side=tk.RIGHT, padx=(0, 8))
        src_box = ttk.Combobox(filter_bar, values=["全部渠道"], state="readonly",
                               width=12, style="Insights.TCombobox")
        src_box.set("全部渠道")
        src_box.pack(side=tk.RIGHT, padx=(0, 8))

        def step_day(n):
            nxt = tl_state["day"] + timedelta(days=n)
            if nxt > datetime.now().date():
                return
            set_day(nxt)

        def choose_day():
            """点日期按钮：弹一个简单的日期输入框（YYYY-MM-DD）"""
            from tkinter import messagebox, simpledialog
            text = simpledialog.askstring(
                "选择日期", "输入日期（YYYY-MM-DD）：",
                initialvalue=tl_state["day"].strftime("%Y-%m-%d"), parent=root)
            if not text:
                return
            try:
                day = datetime.strptime(text.strip(), "%Y-%m-%d").date()
            except ValueError:
                messagebox.showerror("日期格式不对", "请按 YYYY-MM-DD 输入，例如 2026-08-01")
                return
            set_day(min(day, datetime.now().date()))

        prev_btn = btn(filter_bar, "‹ 前一天", lambda: step_day(-1))
        prev_btn.configure(padx=8)
        prev_btn.pack(side=tk.LEFT)
        day_btn = btn(filter_bar, day_label(tl_state["day"]), choose_day)
        day_btn.configure(padx=10)
        day_btn.pack(side=tk.LEFT, padx=(4, 0))
        next_btn = btn(filter_bar, "后一天 ›", lambda: step_day(1))
        next_btn.configure(padx=8)
        next_btn.pack(side=tk.LEFT, padx=(4, 0))
        today_chip = btn(filter_bar, "今天", lambda: set_day(datetime.now().date()))
        today_chip.configure(fg=Design.BRAND)

        def filtered_rows():
            src_sel, model_sel = src_box.get(), model_box.get()
            out = []
            for r in tl_state["rows"]:
                if src_sel != "全部渠道" and store.source_display_name(r[2]) != src_sel:
                    continue
                if model_sel != "全部模型" and r[1] != model_sel:
                    continue
                out.append(r)
            return out

        def refill(*_):
            tree.delete(*tree.get_children())
            for ts, model, source, token, cost, credits in filtered_rows():
                t = datetime.fromtimestamp(ts)
                if credits > 0:
                    amount = "%s 积分" % self._fmt_credits(credits)
                elif cost > 0:
                    amount = "$%.2f" % cost
                else:
                    amount = "-"
                tree.insert("", tk.END, values=(
                    t.strftime("%H:%M:%S"), store.source_display_name(source), model,
                    self.fmt_tokens(token), amount))

        def refresh_day():
            day = tl_state["day"]
            tl_state["rows"] = store.query_timeline(day) or []
            day_btn.configure(text=day_label(day),
                              fg=Design.BRAND if is_today(day) else Design.TEXT_PRIMARY)
            if is_today(day):
                today_chip.pack_forget()
                next_btn.configure(state=tk.DISABLED)
            else:
                today_chip.pack(side=tk.LEFT, padx=(6, 0))
                next_btn.configure(state=tk.NORMAL)
            src_box.configure(values=["全部渠道"] + sorted(
                {store.source_display_name(r[2]) for r in tl_state["rows"]}))
            model_box.configure(values=["全部模型"] + sorted({r[1] for r in tl_state["rows"]}))
            src_box.set("全部渠道")
            model_box.set("全部模型")
            refill()

        def set_day(day):
            tl_state["day"] = day
            refresh_day()

        def export_timeline_csv():
            """导出当前筛选下的逐笔流水 CSV（带 BOM，Excel 直开）"""
            from tkinter import filedialog, messagebox
            import csv
            rows = filtered_rows()
            path = filedialog.asksaveasfilename(
                title="导出流水", defaultextension=".csv",
                initialfile="ccbar-timeline-%s.csv" % tl_state["day"].strftime("%Y%m%d"),
                filetypes=[("CSV", "*.csv")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["时间", "模型", "渠道", "总 Token", "费用/积分"])
                    for ts, model, source, token, cost, credits in rows:
                        # 与流水页一致：Trae 行导积分，其他源导美元费用
                        if source == "trae":
                            amount = "%s 积分" % self._fmt_credits(credits)
                        else:
                            amount = "%.4f" % cost
                        writer.writerow([
                            datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            model, source, token, amount])
            except OSError as e:
                messagebox.showerror("导出失败", str(e))
                return
            messagebox.showinfo("导出完成", "已导出 %d 行到：\n%s" % (len(rows), path))

        src_box.bind("<<ComboboxSelected>>", refill)
        model_box.bind("<<ComboboxSelected>>", refill)
        refresh_day()

        # ============ 页签记忆 ============
        titles = [nb.tab(t, "text") for t in nb.tabs()]
        last_page = self.settings.get("insights_last_page") or "费用"
        if last_page not in titles:
            last_page = "费用"
        try:
            nb.select(titles.index(last_page))
        except Exception:
            pass

        def on_tab_changed(_event=None):
            try:
                name = nb.tab(nb.index(nb.select()), "text")
            except Exception:
                return
            if self.settings.get("insights_last_page") != name:
                self.settings.set("insights_last_page", name)
                self.settings.save()

        nb.bind("<<NotebookTabChanged>>", on_tab_changed)

        self._bring_to_front(root)

    @_on_gui
    def show_settings(self, icon=None, item=None):
        """显示设置窗口（深色，内容可滚动，底部按钮栏钉底）"""
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog

        root = tk.Toplevel(self._ui_root)
        root.title("ccBar 设置")
        root.geometry("560x640")
        root.minsize(520, 420)
        root.configure(bg=Design.BACKGROUND)

        tk.Label(root, text="⚙️ 设置", font=Design.FONT_TITLE, fg=Design.TEXT_PRIMARY,
                 bg=Design.BACKGROUND).pack(anchor='w', padx=24, pady=(18, 10))
        tk.Frame(root, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=24)

        # 按钮栏钉底，内容区自己滚动
        btn_bar = tk.Frame(root, bg=Design.BACKGROUND)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=(8, 16))

        body = tk.Frame(root, bg=Design.BACKGROUND)
        body.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(body, bg=Design.BACKGROUND, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=Design.BACKGROUND)
        inner_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_wheel)
        root.bind("<Destroy>",
                  lambda e: canvas.unbind_all("<MouseWheel>") if e.widget is root else None)

        OK_COLOR, WARN_COLOR = "#34C759", "#FF9500"
        LANG_LABELS = {"system": "跟随系统", "zh": "中文", "en": "English"}
        LANG_BY_LABEL = {v: k for k, v in LANG_LABELS.items()}

        def section(title):
            tk.Frame(inner, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=24, pady=(16, 8))
            tk.Label(inner, text=title, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_SECTION, anchor='w').pack(fill=tk.X, padx=24)

        def field_row(label, hint=None, label_width=18):
            row = tk.Frame(inner, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=24, pady=(10, 0))
            tk.Label(row, text=label, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_UI, width=label_width, anchor='w').pack(side=tk.LEFT)
            entry = tk.Entry(row, width=12, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             insertbackground=Design.TEXT_PRIMARY, relief="flat",
                             font=Design.FONT_MONO, highlightthickness=1,
                             highlightbackground=Design.CARD_BORDER,
                             highlightcolor=Design.BRAND)
            entry.pack(side=tk.LEFT, ipady=4)
            if hint:
                tk.Label(row, text=hint, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))
            return entry

        def small_btn(parent, text, cmd, primary=False):
            return tk.Button(parent, text=text, command=cmd,
                             bg=Design.BRAND if primary else Design.CARD_FILL,
                             fg=Design.TEXT_PRIMARY, activebackground=Design.BTN_BG_HOVER,
                             activeforeground=Design.TEXT_PRIMARY, relief="flat", bd=0,
                             padx=12, pady=4, cursor="hand2", font=Design.FONT_UI_SMALL)

        def check_row(text, value):
            var = tk.BooleanVar(value=bool(value))
            tk.Checkbutton(inner, text=text, variable=var, bg=Design.BACKGROUND,
                           fg=Design.TEXT_PRIMARY, activebackground=Design.BACKGROUND,
                           activeforeground=Design.TEXT_PRIMARY, selectcolor=Design.CARD_FILL,
                           bd=0, highlightthickness=0, font=Design.FONT_UI,
                           cursor="hand2").pack(anchor='w', padx=24, pady=3)
            return var

        custom = [dict(t) for t in (self.settings.get("custom_themes") or [])]

        # ------------------------------------------------------------ 主题
        section("外观")
        theme_row = tk.Frame(inner, bg=Design.BACKGROUND)
        theme_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(theme_row, text="主题风格", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=18, anchor='w').pack(side=tk.LEFT)

        current_theme = self.theme or themes.default_theme()
        theme_var = tk.StringVar(value=current_theme.get("name", "默认主题"))

        def theme_names():
            names = []
            for t in themes.all_themes(custom):
                if t["name"] not in names:
                    names.append(t["name"])
            return names

        theme_menu = tk.OptionMenu(theme_row, theme_var, *theme_names())
        theme_menu.configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY, relief="flat", bd=0,
                             activebackground=Design.BTN_BG_HOVER,
                             activeforeground=Design.TEXT_PRIMARY, highlightthickness=0,
                             font=Design.FONT_UI_SMALL, width=12)
        theme_menu["menu"].configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                                     activebackground=Design.BRAND, font=Design.FONT_UI_SMALL)
        theme_menu.pack(side=tk.LEFT, padx=(0, 8))

        def selected_theme():
            name = theme_var.get()
            for t in themes.all_themes(custom):
                if t["name"] == name:
                    return t
            return themes.default_theme()

        def refresh_theme_menu():
            menu = theme_menu["menu"]
            menu.delete(0, "end")
            for name in theme_names():
                menu.add_command(label=name, command=lambda n=name: theme_var.set(n))

        def import_theme():
            path = filedialog.askopenfilename(title="导入主题包",
                                              filetypes=[("ccBar 主题包", "*.json"), ("All", "*.*")])
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    theme = themes.theme_from_json(f.read())
            except Exception as e:
                messagebox.showerror("导入失败", "主题包读取失败：%s" % e)
                return
            custom[:] = themes.upsert_custom(custom, theme)
            theme_var.set(theme["name"])
            refresh_theme_menu()
            messagebox.showinfo("导入成功", "主题「%s」已加入可选列表（保存后生效）" % theme["name"])

        def export_theme():
            name = theme_var.get()
            path = filedialog.asksaveasfilename(
                title="导出主题包", defaultextension=".json",
                initialfile="ccbar-theme-%s.json" % name,
                filetypes=[("ccBar 主题包", "*.json")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(themes.theme_to_json(selected_theme()))
                messagebox.showinfo("导出成功", "已导出到：\n%s" % path)
            except Exception as e:
                messagebox.showerror("导出失败", str(e))

        def rename_theme():
            name = theme_var.get()
            theme = selected_theme()
            if not themes.is_custom(theme.get("id"), custom):
                messagebox.showinfo("提示", "内置主题不能重命名")
                return
            new_name = simpledialog.askstring("重命名主题", "新名称：", initialvalue=name,
                                             parent=root)
            if not new_name or new_name == name:
                return
            custom[:] = themes.rename_custom(custom, theme["id"], new_name)
            theme_var.set(new_name)
            refresh_theme_menu()

        def delete_theme():
            theme = selected_theme()
            if not themes.is_custom(theme.get("id"), custom):
                messagebox.showinfo("提示", "内置主题不能删除")
                return
            custom[:] = themes.delete_custom(custom, theme["id"])
            theme_var.set("默认主题")
            refresh_theme_menu()

        small_btn(theme_row, "导入", import_theme).pack(side=tk.LEFT)
        small_btn(theme_row, "导出", export_theme).pack(side=tk.LEFT, padx=(6, 0))
        small_btn(theme_row, "重命名", rename_theme).pack(side=tk.LEFT, padx=(6, 0))
        small_btn(theme_row, "删除", delete_theme).pack(side=tk.LEFT, padx=(6, 0))

        # 界面语言
        lang_row = tk.Frame(inner, bg=Design.BACKGROUND)
        lang_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(lang_row, text="界面语言", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=18, anchor='w').pack(side=tk.LEFT)
        lang_var = tk.StringVar(
            value=LANG_LABELS.get(self.settings.get("app_language") or "system", "跟随系统"))
        lang_menu = tk.OptionMenu(lang_row, lang_var, *["跟随系统", "中文", "English"])
        lang_menu.configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY, relief="flat", bd=0,
                            activebackground=Design.BTN_BG_HOVER,
                            activeforeground=Design.TEXT_PRIMARY, highlightthickness=0,
                            font=Design.FONT_UI_SMALL, width=12)
        lang_menu["menu"].configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                                    activebackground=Design.BRAND, font=Design.FONT_UI_SMALL)
        lang_menu.pack(side=tk.LEFT)
        tk.Label(lang_row, text="重启后生效", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))

        interval_entry = field_row("刷新间隔 (秒):", "范围 5 - 3000")
        interval_entry.insert(0, str(self.settings["refresh_interval"]))

        # ------------------------------------------------------------ 数据源
        section("数据源")

        source_vars, source_entries, source_status = {}, {}, {}
        source_keys = {"ccswitch": ("ccswitch_enabled", "db_path"),
                       "zcode": ("zcode_enabled", "zcode_path")}

        def validate_source(adapter_id):
            """只读试开源库并检查必需表，结果就地显示在状态行（保存时才生效）"""
            adapter = next(a for a in SOURCE_REGISTRY if a.id == adapter_id)
            path = source_entries[adapter_id].get().strip()
            status = source_status[adapter_id]
            if not path:
                status.config(text="", fg=Design.TEXT_MUTED)
                return
            error = validate_db(path, adapter.required_tables)
            if error is None:
                status.config(text="表结构正确（保存后生效）", fg=OK_COLOR)
            else:
                status.config(text=error, fg=WARN_COLOR)

        attached_ids = [a.id for a in self.store.attached]
        for adapter in SOURCE_REGISTRY:
            enable_key, path_key = source_keys.get(
                adapter.id, ("%s_enabled" % adapter.id, "%s_path" % adapter.id))

            src = tk.Frame(inner, bg=Design.BACKGROUND)
            src.pack(fill=tk.X, padx=24, pady=(8, 0))
            row = tk.Frame(src, bg=Design.BACKGROUND)
            row.pack(fill=tk.X)

            var = tk.BooleanVar(value=self.settings.get(enable_key, False))
            source_vars[adapter.id] = var
            tk.Checkbutton(row, variable=var, bg=Design.BACKGROUND, fg=Design.TEXT_PRIMARY,
                           activebackground=Design.BACKGROUND, activeforeground=Design.TEXT_PRIMARY,
                           selectcolor=Design.CARD_FILL, bd=0, highlightthickness=0,
                           cursor="hand2").pack(side=tk.LEFT)

            tk.Label(row, text=adapter.name, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_UI, width=10, anchor='w').pack(side=tk.LEFT)

            entry = tk.Entry(row, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             insertbackground=Design.TEXT_PRIMARY, relief="flat",
                             font=Design.FONT_MONO_SMALL, highlightthickness=1,
                             highlightbackground=Design.CARD_BORDER,
                             highlightcolor=Design.BRAND)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            entry.insert(0, self.settings.get(path_key) or adapter.default_path)
            source_entries[adapter.id] = entry

            def make_browse(entry_box, adapter_id):
                def browse():
                    filename = filedialog.askopenfilename(
                        title="选择数据库文件",
                        filetypes=[("SQLite", "*.db *.sqlite"), ("All", "*.*")])
                    if filename:
                        entry_box.delete(0, tk.END)
                        entry_box.insert(0, filename)
                        validate_source(adapter_id)
                return browse

            small_btn(row, "浏览", make_browse(entry, adapter.id)).pack(side=tk.LEFT, padx=(8, 0))

            status = tk.Label(src, text="", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                              font=Design.FONT_UI_SMALL, anchor='w')
            status.pack(fill=tk.X, pady=(2, 0))
            source_status[adapter.id] = status
            if adapter.id in attached_ids:
                status.config(text="已连接", fg=OK_COLOR)
            elif not var.get():
                status.config(text="未启用", fg=Design.TEXT_MUTED)
            else:
                status.config(text="未连接", fg=WARN_COLOR)
            entry.bind("<FocusOut>", lambda e, aid=adapter.id: validate_source(aid))

        # ------------------------------------------------------------ 提醒与费用
        section("提醒")
        warning_entry = field_row("预警阈值 (万):", "超过此值将弹出通知提醒")
        warning_entry.insert(0, str(self.settings["warning_threshold"]))
        notify_entry = field_row("通知间隔 (万):", "每累计N万通知一次，0=关闭")
        notify_entry.insert(0, str(self.settings.get("notify_interval", 1000)))
        led_entry = field_row("红色门槛 (万):", "单次刷新增量达到即变红，0=不变红")
        led_entry.insert(0, str(self.settings.get("led_red_threshold", 100)))

        section("费用估算")
        budget_entry = field_row("月度预算 ($):", "0=关闭预算显示")
        budget_value = self.settings.get("monthly_budget_usd") or 0
        budget_entry.insert(0, ("%g" % budget_value) if budget_value else "0")
        price_entry = field_row("默认单价 ($/M):", "未计费渠道按此估算，0=关闭")
        price_value = self.settings.get("default_token_price") or 0
        price_entry.insert(0, ("%g" % price_value) if price_value else "0")

        # ------------------------------------------------------------ 开关
        section("行为")
        warning_var = check_row("启用用量预警", self.settings["warning_enabled"])
        login_var = check_row("开机自动启动", self.settings.get("launch_at_login"))
        report_var = check_row("每周一自动生成用量周报",
                               self.settings.get("auto_weekly_report", True))

        backup_row = tk.Frame(inner, bg=Design.BACKGROUND)
        backup_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(backup_row,
                 text="上次自动备份：%s" % (self.settings.get("last_auto_backup_date") or "从未"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)

        def open_backup_dir():
            path = self.backup_dir()
            os.makedirs(path, exist_ok=True)
            try:
                os.startfile(path)          # Windows
            except AttributeError:
                import webbrowser
                webbrowser.open("file://" + path.replace("\\", "/"))

        small_btn(backup_row, "打开备份目录", open_backup_dir).pack(side=tk.RIGHT)

        # ------------------------------------------------------------ 数据
        section("数据")

        mig = tk.Frame(inner, bg=Design.BACKGROUND)
        mig.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(mig, text="明细 CSV 迁移（导入幂等：主键去重）", fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
        tk.Button(mig, text="导入 CSV", command=lambda: self.import_data(root),
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
        tk.Button(mig, text="导出 CSV", command=self.export_data,
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT, padx=(0, 8))

        def merge_database():
            """多机合并：把另一台机器的统计库明细按主键并进来（不重不漏）"""
            path = filedialog.askopenfilename(
                title="选择另一台机器的 ccbar.db",
                filetypes=[("SQLite", "*.db"), ("All", "*.*")])
            if not path:
                return
            merge = getattr(self.store, "merge_from_db", None)
            if merge is None:
                messagebox.showerror("合并失败", "当前版本不支持多机合并")
                return
            read, inserted = merge(path)
            if read == 0 and inserted == 0:
                messagebox.showerror("合并失败", "不是有效的 ccBar 统计库（缺 usage_log 表或打不开）")
                return
            messagebox.showinfo("合并完成",
                                "共读取 %d 行 · 新增 %d 行（主键去重）" % (read, inserted))

        merge_row = tk.Frame(inner, bg=Design.BACKGROUND)
        merge_row.pack(fill=tk.X, padx=24, pady=(10, 4))
        tk.Label(merge_row, text="多机合并（另一台机器的统计库）", fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
        tk.Button(merge_row, text="合并数据库…", command=merge_database,
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)

        # ------------------------------------------------------------ 按钮栏

        def reset():
            interval_entry.delete(0, tk.END)
            interval_entry.insert(0, "30")
            for adapter in SOURCE_REGISTRY:
                enable_key, path_key = source_keys.get(
                    adapter.id, ("%s_enabled" % adapter.id, "%s_path" % adapter.id))
                source_vars[adapter.id].set(adapter.id == "ccswitch")
                source_entries[adapter.id].delete(0, tk.END)
                source_entries[adapter.id].insert(0, adapter.default_path)
            warning_entry.delete(0, tk.END)
            warning_entry.insert(0, "50")
            notify_entry.delete(0, tk.END)
            notify_entry.insert(0, "1000")
            led_entry.delete(0, tk.END)
            led_entry.insert(0, "100")
            budget_entry.delete(0, tk.END)
            budget_entry.insert(0, "0")
            price_entry.delete(0, tk.END)
            price_entry.insert(0, "0")
            warning_var.set(True)
            login_var.set(False)
            report_var.set(True)
            lang_var.set("跟随系统")
            custom[:] = []
            theme_var.set("默认主题")
            refresh_theme_menu()

        def collect():
            updates = {
                "refresh_interval": interval_entry.get(),
                "warning_threshold": warning_entry.get(),
                "notify_interval": notify_entry.get(),
                "led_red_threshold": led_entry.get(),
                "monthly_budget_usd": budget_entry.get(),
                "default_token_price": price_entry.get(),
                "warning_enabled": warning_var.get(),
                "launch_at_login": login_var.get(),
                "auto_weekly_report": report_var.get(),
                "app_language": LANG_BY_LABEL.get(lang_var.get(), "system"),
                "theme": selected_theme().get("id", "默认主题"),
                "custom_themes": custom,
            }
            for adapter in SOURCE_REGISTRY:
                enable_key, path_key = source_keys.get(
                    adapter.id, ("%s_enabled" % adapter.id, "%s_path" % adapter.id))
                updates[enable_key] = source_vars[adapter.id].get()
                path_value = source_entries[adapter.id].get().strip()
                updates[path_key] = path_value or adapter.default_path
            return updates

        def save():
            updates = collect()
            errors = self.settings.validate(updates)
            if errors:
                messagebox.showerror("设置未保存", "请检查：\n· " + "\n· ".join(errors))
                return
            self.settings.apply(updates)
            # 开关类设置需要同步系统状态
            try:
                autostart.apply(self.settings.get("launch_at_login"))
            except Exception as e:
                print("开机自启设置失败:", e)
            self.apply_language()
            self.apply_theme()
            # 立即按新设置重连统计库（启用新源会触发首次全量回填）
            self.connect_store()
            messagebox.showinfo("成功", "设置已保存，将在下次刷新时生效")
            root.destroy()

        small_btn(btn_bar, "重置", reset).pack(side=tk.RIGHT)
        small_btn(btn_bar, "检查更新", self.check_for_updates).pack(side=tk.RIGHT, padx=(0, 8))
        small_btn(btn_bar, "备份数据", self.backup_data).pack(side=tk.RIGHT, padx=(0, 8))
        small_btn(btn_bar, "保存", save, primary=True).pack(side=tk.RIGHT, padx=(0, 8))

        self._bring_to_front(root)

    @_on_gui
    def backup_data(self, icon=None, item=None):
        """一键备份统计库（VACUUM INTO 导出独立 db 文件，与 macOS 版同款）"""
        from tkinter import filedialog, messagebox

        stamp = datetime.now().strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            title="备份统计库",
            defaultextension=".db",
            initialfile=f"ccbar-backup-{stamp}.db",
            filetypes=[("SQLite", "*.db"), ("All", "*.*")])
        if not path:
            return
        if self.store.backup(path):
            messagebox.showinfo(
                "备份完成",
                f"统计库已备份到：\n{path}\n\n"
                "恢复方式：退出 ccBar 后用备份文件替换\n~/.ccbar/ccbar.db")
        else:
            messagebox.showerror("备份失败", "统计库未打开或目标位置不可写")

    def check_for_updates(self, icon=None, item=None):
        """检查更新：后台请求 GitHub（网络不能堵 GUI 线程），完成后回 GUI 弹窗"""
        def worker():
            latest = fetch_latest_version()
            self._ui(lambda: self._show_update_result(latest))

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_result(self, latest):
        """已在 GUI 线程：对比版本并弹窗（语义化比较）"""
        from tkinter import messagebox
        import webbrowser

        if latest and is_newer_version(latest, APP_VERSION):
            if messagebox.askyesno(
                    "发现新版本",
                    f"最新版本 v{latest}，当前 v{APP_VERSION}\n是否前往下载？"):
                webbrowser.open(RELEASES_URL)
            return
        messagebox.showinfo(
            "检查更新",
            f"已经是最新版本（v{APP_VERSION}）" if latest
            else "检查失败，稍后再试，或直接到 GitHub Releases 页面查看")

    def export_data(self):
        """导出明细 CSV（换机迁移 / Excel 查看）"""
        from tkinter import filedialog, messagebox
        stamp = datetime.now().strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            title="导出明细", defaultextension=".csv",
            initialfile=f"ccbar-export-{stamp}.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        n = self.store.export_csv(path)
        if n >= 0:
            messagebox.showinfo("导出完成", f"已导出 {n} 行明细到：\n{path}")
        else:
            messagebox.showerror("导出失败", "统计库未打开或目标位置不可写")

    def import_data(self, root):
        """幂等导入明细 CSV：主键去重，同一文件重复导零新增"""
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(title="导入明细",
                                          filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return
        read, inserted, skipped = self.store.import_csv(path)
        if skipped == -1:
            messagebox.showerror("导入失败", "不是 ccBar 导出的明细 CSV（表头不符）")
            return
        messagebox.showinfo("导入完成",
                            f"共读取 {read} 行 · 新增 {inserted} 行 · 跳过 {skipped} 行（重复或非法）")

    def quit_app(self, icon, item):
        """退出应用"""
        icon.stop()

    def update_icon(self):
        """更新图标和标题"""
        while True:
            time.sleep(self.settings["refresh_interval"])
            # 懒惰补账：历史（昨天及更早）落后就同步进自建库
            self.store.sync_if_needed()
            # 按用量更新图标颜色
            self.update_icon_color()
            # 检查里程碑（每1000万token冒泡通知）
            today = self.query_day_stats(0)
            if today:
                self.check_token_milestone(today["total"])
            # 每天首次刷新自动备份统计库（滚动保留 7 份）
            self.maybe_auto_backup()
            # 静默检查更新（3 天一次，只有真有新版才提示）
            self.maybe_check_updates_silently()
            # 更新菜单
            if self.icon:
                menu = pystray.Menu(*self.build_menu())
                self.icon.menu = menu
                self.icon.title = self.get_tooltip_text()

    def maybe_check_updates_silently(self):
        """静默检查更新：3 天一次，只在真有新版时提示（与 macOS 版同款节流）"""
        today = datetime.now().strftime("%Y-%m-%d")
        last = self.settings.get("last_update_check_date") or ""
        if last:
            try:
                if (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days < 3:
                    return
            except ValueError:
                pass
        self.settings.set("last_update_check_date", today)
        self.settings.save()

        def worker():
            latest = fetch_latest_version()
            if latest and is_newer_version(latest, APP_VERSION):
                self._ui(lambda: self._show_update_result(latest))

        threading.Thread(target=worker, daemon=True).start()

    def get_tooltip_text(self):
        """获取托盘标题文本（悬停时显示）"""
        today = self.query_day_stats(0)
        if not today:
            return "ccBar - 未找到数据"
        total_str = self.fmt_tokens(today["total"])
        return total_str

    def run(self):
        """运行应用"""
        # 先起 GUI 线程，后续所有窗口都投递到它上面
        self._start_gui()

        # 初始化自建统计库并 ATTACH 各数据源（首次自动全量回填）
        self.connect_store()

        # 创建图标（按当前用量着色：浅绿 → 黄 → 橙，超过 8000万 才红）
        today = self.query_day_stats(0)
        initial_color = (today_usage_color(today["total"])
                         if today else Design.BRAND)
        self._last_icon_color = initial_color
        image = self.create_icon(initial_color)

        # 初始菜单
        menu = pystray.Menu(*self.build_menu())

        # 创建托盘图标
        self.icon = pystray.Icon(
            "ccBar",
            image,
            self.get_tooltip_text(),
            menu
        )

        # 启动更新线程
        update_thread = threading.Thread(target=self.update_icon, daemon=True)
        update_thread.start()

        # 运行
        self.icon.run()

if __name__ == "__main__":
    app = CcBarTray()
    app.run()