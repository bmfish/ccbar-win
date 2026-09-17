import sys
import os
import sqlite3
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

# 数据库路径
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")


# ============================================================
# 设计系统（与 macOS 版保持一致）
# ============================================================

class Design:
    """统一的设计令牌"""

    # 品牌色
    BRAND = "#E86D45"          # 品牌橙
    BACKGROUND = "#1C1C1C"     # 深色背景
    CARD_FILL = "#2A2A2A"      # 卡片填充
    CARD_BORDER = "#3D3D3D"    # 卡片边框

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
        """格式化 token 数量"""
        if n >= 100_000_000:
            return f"{n / 100_000_000:.2f}亿"
        elif n >= 10_000:
            return f"{n // 10_000}万"
        else:
            return str(int(n))


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


def usage_color_for_total(total, threshold_wan):
    """根据今日用量和预警阈值计算颜色

    色阶锚点（以预警阈值为参照）：
    - 0.25x 阈值 → 浅绿
    - 0.75x 阈值 → 黄
    - 1.00x 阈值 → 橙红（刚好达到预警线）
    - >=1.2x 阈值 → 正红
    """
    threshold = float(threshold_wan) * 10_000
    if threshold <= 0:
        return Design.USAGE_STOPS[0][1]
    # 阈值对应 0.83 进度，1.2x 阈值对应满格红色
    return usage_color(min((total / threshold) / 1.2, 1.0))


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
                     fg=Design.BRAND, bg=Design.BACKGROUND, font=Design.FONT_BIG,
                     anchor='w').pack(fill=tk.X, pady=(2, 6))
            self._stat_columns(parent, today, work_hours)
        else:
            if os.path.exists(app.settings["db_path"]):
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
        self.settings = {
            "refresh_interval": 60,
            "db_path": DB_PATH,
            "warning_threshold": 50,
            "warning_enabled": True,
        }
        self.last_notification_date = None
        self.last_history_backup_date = None

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
        """加载设置"""
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "settings.txt"

        if config_file.exists():
            with open(config_file, "r") as f:
                for line in f:
                    if "=" in line:
                        key, value = line.strip().split("=", 1)
                        if key == "refresh_interval":
                            self.settings["refresh_interval"] = int(value)
                        elif key == "db_path":
                            self.settings["db_path"] = value
                        elif key == "warning_threshold":
                            self.settings["warning_threshold"] = int(value)
                        elif key == "warning_enabled":
                            self.settings["warning_enabled"] = value.lower() == "true"

        # 加载上次备份日期
        backup_file = config_dir / "last_backup.txt"
        if backup_file.exists():
            with open(backup_file, "r") as f:
                self.last_history_backup_date = f.read().strip()

    def save_settings(self):
        """保存设置"""
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "settings.txt"

        with open(config_file, "w") as f:
            f.write(f"refresh_interval={self.settings['refresh_interval']}\n")
            f.write(f"db_path={self.settings['db_path']}\n")
            f.write(f"warning_threshold={self.settings['warning_threshold']}\n")
            f.write(f"warning_enabled={self.settings['warning_enabled']}\n")

    def save_backup_date(self, date_str):
        """保存备份日期"""
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        backup_file = config_dir / "last_backup.txt"
        with open(backup_file, "w") as f:
            f.write(date_str)
        self.last_history_backup_date = date_str

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

        color = usage_color_for_total(today["total"], self.settings["warning_threshold"])
        if color == getattr(self, "_last_icon_color", None):
            return  # 颜色没变就不重绘

        self._last_icon_color = color
        if self.icon:
            try:
                self.icon.icon = self.create_icon(color)
                self.icon.title = f"{self.fmt_tokens(today['total'])}"
            except Exception:
                pass

    def init_history_table(self):
        """初始化历史备份表"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_request_logs_history (
                    request_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_model TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    input_cost_usd TEXT NOT NULL DEFAULT '0',
                    output_cost_usd TEXT NOT NULL DEFAULT '0',
                    cache_read_cost_usd TEXT NOT NULL DEFAULT '0',
                    cache_creation_cost_usd TEXT NOT NULL DEFAULT '0',
                    total_cost_usd TEXT NOT NULL DEFAULT '0',
                    latency_ms INTEGER NOT NULL,
                    first_token_ms INTEGER,
                    duration_ms INTEGER,
                    status_code INTEGER NOT NULL,
                    error_message TEXT,
                    session_id TEXT,
                    provider_type TEXT,
                    is_streaming INTEGER NOT NULL DEFAULT 0,
                    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                    created_at INTEGER NOT NULL,
                    data_source TEXT NOT NULL DEFAULT 'proxy',
                    pricing_model TEXT,
                    input_token_semantics INTEGER NOT NULL DEFAULT 0,
                    backed_up_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
            """)

            conn.commit()
            conn.close()
            print("历史备份表已就绪")
        except Exception as e:
            print(f"创建历史备份表失败: {e}")

    def backup_history(self):
        """备份历史数据"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # 获取上次备份日期
            last_backup = self.last_history_backup_date or "2000-01-01"

            # 计算昨天的日期
            yesterday = datetime.now() - timedelta(days=1)
            yesterday_str = yesterday.strftime("%Y-%m-%d")

            # 如果已经备份过昨天，跳过
            if last_backup >= yesterday_str:
                print(f"历史数据已是最新（上次备份: {last_backup}）")
                conn.close()
                return

            # 备份从上次备份日期到昨天的数据
            cursor.execute("""
                INSERT OR IGNORE INTO proxy_request_logs_history
                SELECT
                    request_id, provider_id, app_type, model, request_model,
                    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
                    total_cost_usd, latency_ms, first_token_ms, duration_ms,
                    status_code, error_message, session_id, provider_type,
                    is_streaming, cost_multiplier, created_at, data_source,
                    pricing_model, input_token_semantics,
                    strftime('%s', 'now') as backed_up_at
                FROM proxy_request_logs
                WHERE date(created_at, 'unixepoch', 'localtime') > ?
                  AND date(created_at, 'unixepoch', 'localtime') <= ?
            """, (last_backup, yesterday_str))

            changes = cursor.rowcount
            conn.commit()
            conn.close()

            print(f"历史备份完成: 新增 {changes} 条记录（{last_backup} ~ {yesterday_str}）")
            self.save_backup_date(yesterday_str)

        except Exception as e:
            print(f"历史备份失败: {e}")

    def check_and_run_scheduled_backup(self):
        """检查是否需要执行定时备份（11:00 或 20:00）"""
        now = datetime.now()
        hour = now.hour
        minute = now.minute

        # 检查是否是备份时间（11:00 或 20:00）
        is_backup_time = (hour == 11 and minute == 0) or (hour == 20 and minute == 0)

        if not is_backup_time:
            return

        # 获取今天是否已经备份过
        last_backup = self.last_history_backup_date or "2000-01-01"
        yesterday = datetime.now() - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y-%m-%d")

        # 如果20:00检查，且已经备份到昨天，跳过
        if hour == 20 and last_backup >= yesterday_str:
            print("20:00 检查：历史数据已是最新，跳过备份")
            return

        # 执行备份
        print(f"执行定时备份（{hour}:00）")
        self.backup_history()

    def query_day_stats(self, days=0):
        """查询统计（包含历史表）"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            now = datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_timestamp = int(start_of_day.timestamp())

            if days == 0:
                # 今日
                cursor.execute("""
                    SELECT
                        COUNT(*) as reqs,
                        COALESCE(SUM(input_tokens), 0) as input,
                        COALESCE(SUM(output_tokens), 0) as output,
                        COALESCE(SUM(cache_creation_tokens), 0) as cache_create,
                        COALESCE(SUM(cache_read_tokens), 0) as cache_read
                    FROM proxy_request_logs
                    WHERE created_at >= ?
                """, (start_timestamp,))
            elif days == 1:
                # 昨日
                yesterday_start = start_of_day - timedelta(days=1)
                cursor.execute("""
                    SELECT
                        COUNT(*) as reqs,
                        COALESCE(SUM(input_tokens), 0) as input,
                        COALESCE(SUM(output_tokens), 0) as output,
                        COALESCE(SUM(cache_creation_tokens), 0) as cache_create,
                        COALESCE(SUM(cache_read_tokens), 0) as cache_read
                    FROM proxy_request_logs
                    WHERE created_at >= ? AND created_at < ?
                """, (int(yesterday_start.timestamp()), start_timestamp))
            else:
                # 近N天（包含历史表）
                start_date = start_of_day - timedelta(days=days)
                cursor.execute("""
                    SELECT SUM(reqs), SUM(input), SUM(output), SUM(cache_create), SUM(cache_read) FROM (
                        SELECT
                            COUNT(*) as reqs,
                            COALESCE(SUM(input_tokens), 0) as input,
                            COALESCE(SUM(output_tokens), 0) as output,
                            COALESCE(SUM(cache_creation_tokens), 0) as cache_create,
                            COALESCE(SUM(cache_read_tokens), 0) as cache_read
                        FROM proxy_request_logs
                        WHERE created_at >= ?
                        UNION ALL
                        SELECT
                            COALESCE(SUM(request_count), 0) as reqs,
                            COALESCE(SUM(input_tokens), 0) as input,
                            COALESCE(SUM(output_tokens), 0) as output,
                            COALESCE(SUM(cache_creation_tokens), 0) as cache_create,
                            COALESCE(SUM(cache_read_tokens), 0) as cache_read
                        FROM usage_daily_rollups
                        WHERE date >= date(?, 'unixepoch', 'localtime')
                          AND date < (SELECT date(MIN(created_at), 'unixepoch', 'localtime') FROM proxy_request_logs)
                    )
                """, (int(start_date.timestamp()), int(start_date.timestamp())))

            row = cursor.fetchone()
            conn.close()

            if row and row[0]:
                return {
                    "reqs": row[0],
                    "input": row[1] or 0,
                    "output": row[2] or 0,
                    "cache_create": row[3] or 0,
                    "cache_read": row[4] or 0,
                    "total": (row[1] or 0) + (row[2] or 0) + (row[3] or 0) + (row[4] or 0)
                }
        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_hourly_stats(self, days_ago=0):
        """查询每小时统计"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    strftime('%H', created_at, 'unixepoch', 'localtime') as hour,
                    COUNT(*) as reqs,
                    COALESCE(SUM(output_tokens), 0) as output,
                    COALESCE(SUM(input_tokens), 0) as input,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read
                FROM proxy_request_logs
                WHERE date(created_at, 'unixepoch', 'localtime') = date('now', 'localtime', '-' || ? || ' days')
                GROUP BY hour
                ORDER BY hour
            """, (days_ago,))

            hourly_data = {}
            for row in cursor.fetchall():
                hour = int(row[0])
                hourly_data[hour] = {
                    "reqs": row[1],
                    "output": row[2],
                    "input": row[3],
                    "cache_read": row[4]
                }

            conn.close()
            return hourly_data

        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_daily_stats_for_range(self, start_date, end_date):
        """查询日期范围内的每日统计"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # 查询原始日志
            cursor.execute("""
                SELECT
                    date(created_at, 'unixepoch', 'localtime') as day,
                    COUNT(*) as reqs,
                    COALESCE(SUM(output_tokens), 0) as output,
                    COALESCE(SUM(input_tokens), 0) as input,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read
                FROM proxy_request_logs
                WHERE date(created_at, 'unixepoch', 'localtime') >= ?
                  AND date(created_at, 'unixepoch', 'localtime') <= ?
                GROUP BY day
            """, (start_date, end_date))

            daily_data = {}
            for row in cursor.fetchall():
                daily_data[row[0]] = {
                    "reqs": row[1],
                    "output": row[2],
                    "input": row[3],
                    "cache_read": row[4]
                }

            # 查询历史汇总（30天前的数据）
            cursor.execute("""
                SELECT
                    date,
                    COALESCE(SUM(request_count), 0) as reqs,
                    COALESCE(SUM(output_tokens), 0) as output,
                    COALESCE(SUM(input_tokens), 0) as input,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read
                FROM usage_daily_rollups
                WHERE date >= ? AND date <= ?
                  AND date < (SELECT date(MIN(created_at), 'unixepoch', 'localtime') FROM proxy_request_logs)
                GROUP BY date
            """, (start_date, end_date))

            for row in cursor.fetchall():
                if row[0] not in daily_data:
                    daily_data[row[0]] = {
                        "reqs": row[1],
                        "output": row[2],
                        "input": row[3],
                        "cache_read": row[4]
                    }

            conn.close()
            return daily_data

        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_model_breakdown(self):
        """查询模型分布"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            now = datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_timestamp = int(start_of_day.timestamp())

            cursor.execute("""
                SELECT
                    model,
                    COALESCE(SUM(input_tokens), 0) as input,
                    COALESCE(SUM(output_tokens), 0) as output,
                    COALESCE(SUM(input_tokens + output_tokens), 0) as total
                FROM proxy_request_logs
                WHERE created_at >= ?
                GROUP BY model
                ORDER BY total DESC
                LIMIT 5
            """, (start_timestamp,))

            breakdown = []
            for row in cursor.fetchall():
                breakdown.append({
                    "model": row[0],
                    "input": row[1],
                    "output": row[2],
                    "total": row[3]
                })

            conn.close()
            return breakdown if breakdown else None
        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_work_hours(self):
        """查询工作时长"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            now = datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_timestamp = int(start_of_day.timestamp())

            cursor.execute("""
                SELECT MIN(created_at)
                FROM proxy_request_logs
                WHERE created_at >= ?
            """, (start_timestamp,))

            row = cursor.fetchone()
            conn.close()

            if row and row[0]:
                start = datetime.fromtimestamp(row[0])
                hours = (datetime.now() - start).total_seconds() / 3600
                if hours > 0:
                    return f"{hours:.1f}"

        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_total_stats(self):
        """查询历史总量

        总量 = proxy_request_logs 全部 + usage_daily_rollups 中更早的部分
        （只取 rollup 里早于最早日志日期的行，避免和日志重复计数）
        """
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT SUM(reqs), SUM(total) FROM (
                    SELECT COUNT(*) as reqs,
                           COALESCE(SUM(input_tokens + output_tokens
                                        + cache_creation_tokens + cache_read_tokens), 0) as total
                    FROM proxy_request_logs
                    UNION ALL
                    SELECT COALESCE(SUM(request_count), 0) as reqs,
                           COALESCE(SUM(input_tokens + output_tokens
                                        + cache_creation_tokens + cache_read_tokens), 0) as total
                    FROM usage_daily_rollups
                    WHERE date < (SELECT date(MIN(created_at), 'unixepoch', 'localtime')
                                  FROM proxy_request_logs)
                )
            """)

            row = cursor.fetchone()
            conn.close()

            if row and row[1]:
                return {"reqs": row[0] or 0, "total": row[1] or 0}

        except Exception as e:
            print(f"查询失败: {e}")

        return None

    def query_model_breakdown_by_day(self, days_ago=0):
        """查询某天的模型分布"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    model,
                    COUNT(*) as reqs,
                    COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) as total_token,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read
                FROM proxy_request_logs
                WHERE date(created_at, 'unixepoch', 'localtime') = date('now', 'localtime', '-' || ? || ' days')
                GROUP BY model
                ORDER BY total_token DESC
            """, (days_ago,))

            models = []
            for row in cursor.fetchall():
                models.append({
                    "model": row[0],
                    "reqs": row[1],
                    "total_token": row[2],
                    "cache_read": row[3]
                })

            conn.close()
            return models

        except Exception as e:
            print(f"查询失败: {e}")

        return None

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
            if os.path.exists(self.settings["db_path"]):
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

        # 复制统计
        menu_items.append(pystray.MenuItem("📋 复制今日统计", self.copy_stats))

        # 刷新
        menu_items.append(pystray.MenuItem("🔄 刷新", self.refresh_data))

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
                chart_canvas.delete("all")
                return

            hours_with_data = [h for h, d in hourly_data.items() if d["reqs"] > 0]
            if not hours_with_data:
                tk.Label(inner, text="暂无数据", fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
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
            chart_canvas.update_idletasks()
            w = chart_canvas.winfo_width() or 460
            ChartCanvas.draw_bar_chart(chart_canvas, values, w, 110,
                                       labels=labels, use_gradient=True,
                                       hue_offset=hue_offset)

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

            # 环形图（取前 6 个模型）
            top_models = models[:6]
            items = []
            for idx, m in enumerate(top_models):
                color = colors[idx % len(colors)]
                items.append((m["total_token"], color, m["model"]))

            ChartCanvas.draw_donut(donut_canvas, items, 160)

            # 图例
            for idx, m in enumerate(top_models):
                color = colors[idx % len(colors)]
                pct = (m["total_token"] / total_token * 100) if total_token > 0 else 0
                short_name = m["model"][:16] + "…" if len(m["model"]) > 16 else m["model"]

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

            # 合计行
            total_row = tk.Frame(inner, bg=Design.BACKGROUND)
            total_row.pack(fill=tk.X)
            cells = [("合计", 22, 'w'), (f"{total_reqs}次", 9, 'e'),
                     (Design.fmt_tokens(total_token), 11, 'e'),
                     (Design.fmt_tokens(total_cache), 11, 'e')]
            for text, width, anchor in cells:
                tk.Label(total_row, text=text, width=width, anchor=anchor,
                         fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            # 每个模型（带专属颜色圆点）
            for idx, m in enumerate(models):
                row = tk.Frame(inner, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, pady=1)

                # 颜色圆点（前6个有色，其余灰色）
                color = (colors[idx % len(colors)]
                         if idx < 6 else Design.TEXT_MUTED)
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
                chart_canvas.delete("all")
                return

            sorted_dates = sorted(daily_data.keys())

            total_reqs = sum(d["reqs"] for d in daily_data.values())
            total_token = sum(d["output"] + d["input"] + d["cache_read"] for d in daily_data.values())
            total_cache = sum(d["cache_read"] for d in daily_data.values())

            # 绘制折线图
            values = [d["output"] + d["input"] + d["cache_read"] for d in daily_data.values()]
            chart_canvas.update_idletasks()
            w = chart_canvas.winfo_width() or 500
            ChartCanvas.draw_sparkline(chart_canvas, values, w, 70,
                                       use_gradient=True, hue_offset=hue_offset)

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

        text = "ccSwitch 今日用量统计\n"
        text += "==================\n"

        if today:
            text += f"Token 总量: {self.fmt_tokens(today['total'])}\n"
            text += f"请求数量: {today['reqs']}\n"
            text += f"输入 Token: {self.fmt_tokens(today['input'])}\n"
            text += f"输出 Token: {self.fmt_tokens(today['output'])}\n"

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

    @_on_gui
    def show_settings(self, icon=None, item=None):
        """显示设置窗口（统一深色风格）"""
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Toplevel(self._ui_root)
        root.title("ccBar 设置")
        root.geometry("520x460")
        root.configure(bg=Design.BACKGROUND)
        root.resizable(False, False)

        # 标题
        tk.Label(root, text="⚙️ 设置", font=("Microsoft YaHei UI", 15, "bold"),
                 fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND).pack(anchor='w', padx=24, pady=(20, 12))

        tk.Frame(root, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, padx=24)

        def make_row(label_text, hint_text=None):
            """创建一行：标签 + 输入框 + 提示"""
            row = tk.Frame(root, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=24, pady=(14, 0))

            tk.Label(row, text=label_text, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10), width=14, anchor='w').pack(side=tk.LEFT)

            entry = tk.Entry(row, width=12, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             insertbackground=Design.TEXT_PRIMARY, relief="flat",
                             font=Design.FONT_MONO, highlightthickness=1,
                             highlightbackground=Design.CARD_BORDER,
                             highlightcolor=Design.BRAND)
            entry.pack(side=tk.LEFT, ipady=4)

            if hint_text:
                tk.Label(row, text=hint_text, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))

            return entry

        # 刷新间隔
        interval_entry = make_row("刷新间隔 (秒):", "范围: 5 - 3000")
        interval_entry.insert(0, str(self.settings["refresh_interval"]))

        # 数据库路径
        path_row = tk.Frame(root, bg=Design.BACKGROUND)
        path_row.pack(fill=tk.X, padx=24, pady=(14, 0))

        tk.Label(path_row, text="数据库路径:", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 10), width=14, anchor='w').pack(side=tk.LEFT)

        path_entry = tk.Entry(path_row, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                              insertbackground=Design.TEXT_PRIMARY, relief="flat",
                              font=Design.FONT_MONO_SMALL, highlightthickness=1,
                              highlightbackground=Design.CARD_BORDER,
                              highlightcolor=Design.BRAND)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        path_entry.insert(0, self.settings["db_path"])

        def browse():
            filename = filedialog.askopenfilename(
                title="选择数据库文件",
                filetypes=[("SQLite", "*.db"), ("All", "*.*")]
            )
            if filename:
                path_entry.delete(0, tk.END)
                path_entry.insert(0, filename)

        tk.Button(path_row, text="浏览", command=browse,
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.CARD_BORDER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(root, text="默认: ~/.cc-switch/cc-switch.db", fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(anchor='w', padx=(170, 0), pady=(4, 0))

        # 预警阈值
        warning_entry = make_row("预警阈值 (万):", "超过此值将弹出通知提醒")
        warning_entry.insert(0, str(self.settings["warning_threshold"]))

        # 分隔线
        tk.Frame(root, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, padx=24, pady=(20, 12))

        # 复选框
        warning_var = tk.BooleanVar(value=self.settings["warning_enabled"])
        tk.Checkbutton(root, text="启用用量预警", variable=warning_var,
                       bg=Design.BACKGROUND, fg=Design.TEXT_PRIMARY,
                       activebackground=Design.BACKGROUND, activeforeground=Design.TEXT_PRIMARY,
                       selectcolor=Design.CARD_FILL, bd=0, highlightthickness=0,
                       font=("Microsoft YaHei UI", 10), cursor="hand2").pack(anchor='w', padx=24, pady=3)

        # 按钮栏
        btn_bar = tk.Frame(root, bg=Design.BACKGROUND)
        btn_bar.pack(fill=tk.X, padx=24, pady=(20, 0))

        def make_btn(parent, text, cmd, primary=False):
            bg = Design.BRAND if primary else Design.CARD_FILL
            return tk.Button(parent, text=text, command=cmd, bg=bg, fg=Design.TEXT_PRIMARY,
                             activebackground=Design.CARD_BORDER, activeforeground=Design.TEXT_PRIMARY,
                             relief="flat", bd=0, padx=24, pady=8, cursor="hand2",
                             font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"))

        def reset():
            interval_entry.delete(0, tk.END)
            interval_entry.insert(0, "30")
            path_entry.delete(0, tk.END)
            path_entry.insert(0, os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db"))
            warning_entry.delete(0, tk.END)
            warning_entry.insert(0, "50")
            warning_var.set(True)

        def save():
            try:
                interval = int(interval_entry.get())
                if interval < 5 or interval > 3000:
                    messagebox.showerror("错误", "刷新间隔需在 5 - 3000 秒之间")
                    return
                threshold = int(warning_entry.get())
                if threshold <= 0:
                    messagebox.showerror("错误", "预警阈值需大于 0")
                    return

                self.settings["refresh_interval"] = interval
                self.settings["db_path"] = path_entry.get()
                self.settings["warning_threshold"] = threshold
                self.settings["warning_enabled"] = warning_var.get()
                self.save_settings()
                messagebox.showinfo("成功", "设置已保存，将在下次刷新时生效")
                root.destroy()
            except ValueError:
                messagebox.showerror("错误", "请输入有效的数字")

        make_btn(btn_bar, "重置", reset).pack(side=tk.RIGHT)
        make_btn(btn_bar, "保存", save, primary=True).pack(side=tk.RIGHT, padx=(0, 10))

        self._bring_to_front(root)

    def quit_app(self, icon, item):
        """退出应用"""
        icon.stop()

    def update_icon(self):
        """更新图标和标题"""
        while True:
            time.sleep(self.settings["refresh_interval"])
            # 检查定时备份
            self.check_and_run_scheduled_backup()
            # 按用量更新图标颜色
            self.update_icon_color()
            # 更新菜单
            if self.icon:
                menu = pystray.Menu(*self.build_menu())
                self.icon.menu = menu
                self.icon.title = self.get_tooltip_text()

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

        # 初始化历史备份表
        self.init_history_table()

        # 启动时执行一次备份
        self.backup_history()

        # 创建图标（按当前用量着色：浅绿 → 黄 → 橙 → 红）
        today = self.query_day_stats(0)
        initial_color = (usage_color_for_total(today["total"], self.settings["warning_threshold"])
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