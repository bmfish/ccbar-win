"""托盘左键弹出的自绘面板（从 main.py 拆出）。

只在 GUI 线程上操作，外部调用一律经 CcBarTray._ui() 投递。
"""
import random
import time
from datetime import datetime, timedelta

import l10n
from l10n import L
from ui_kit import (Design, blend, usage_color, today_usage_color,
                    model_colors, gradient_color, ChartCanvas, ChartReadout,
                    work_area, center_geometry, card_underlay, rounded_card,
                    card_title, section_title, muted_line, flat_button,
                    semantic_color, glow_text, format_credits,
                    TODAY_RED_THRESHOLD, _hex_to_rgb, _rgb_to_hex)


class PopoverWindow:
    """托盘左键弹出的自绘面板（对齐 macOS 版 popover 的观感）

    只在 GUI 线程上操作，外部调用一律经 CcBarTray._ui() 投递。
    """

    WIDTH = 300          # 默认宽；设置里开了宽版弹窗后为 380
    PAD = 14             # mac 内容左右内边距 14
    RADIUS = 10
    BTN_HEIGHT = 40
    BTN_GAP = 4

    def __init__(self, app):
        self.app = app
        self.win = None
        self._armed = False
        self._btn_down = False
        self._last_close = 0.0
        # 昨日/近7天/近30天/历史总量只在当天第一次打开时查库，之后读缓存
        self._trend_cache = {"date": None, "data": None}

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

        # 宽版弹窗（macOS 同款设置）：布局是纵向堆叠，加宽只是给模型名更多空间
        self.WIDTH = 380 if self.app.settings.get("popover_wide") else PopoverWindow.WIDTH

        # 打开面板时拉一次 Trae（交互档 1 分钟节流）：拿不到新数据也不影响建面板，
        # 同步在线程里跑，下一次刷新会把结果带进来
        try:
            self.app.sync_trae(interactive=True)
        except Exception:
            pass

        win = tk.Toplevel(self.app._ui_root)
        win.overrideredirect(True)
        # 1px 主题色描边（外框底色从内层透出来），比纯灰边框更有辨识度
        win.configure(bg=blend(Design.BRAND, Design.BACKGROUND, 0.40))
        win.attributes("-topmost", True)
        self.win = win
        self._armed = False
        self._btn_down = False

        inner = tk.Frame(win, bg=Design.BACKGROUND)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        body = tk.Frame(inner, bg=Design.BACKGROUND)
        body.pack(fill=tk.BOTH, expand=True, padx=self.PAD, pady=(10, 6))

        self._build(body)

        # 定位：贴光标上方，钳制在工作区内
        win.update_idletasks()
        height = win.winfo_reqheight()
        x, y = self._position(win, height)
        win.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

        self._round_corners(win, self.RADIUS)
        self._fade_in(win)
        win.lift()
        win.focus_force()
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", self._on_focus_out)
        win.after(250, self._arm)
        win.after(150, self._poll_outside_click)

    @staticmethod
    def _fade_in(win, steps=8, interval=14):
        """淡入（Windows 支持 -alpha）；属性设置失败就保持不透明，绝不因此不显示"""
        try:
            win.attributes("-alpha", 0.0)
        except Exception:
            return
        for i in range(1, steps + 1):
            win.after(interval * i,
                      lambda a=i / steps: win.attributes("-alpha", a))

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

    def _position(self, win, height):
        """面板贴在光标上方（托盘在右下角），并钳制在工作区内

        必须是实例方法：WIDTH 在 show() 里按宽版弹窗设置改成 380，
        而且 staticmethod 里没有 self，原来那行 self.WIDTH 直接 NameError，
        geometry 设不上，窗口就停在默认的左上角 (0,0)。
        """
        cx, cy = win.winfo_pointerxy()
        left, top, right, bottom = PopoverWindow._work_area(win)
        width = self.WIDTH

        x = min(max(cx - width // 2, left + 8), right - width - 8)
        y = cy - height - 12
        if y < top + 8:
            y = min(cy + 12, bottom - height - 8)
        return x, y

    @staticmethod
    def _work_area(win):
        """工作区（排除任务栏）"""
        try:
            return work_area()
        except Exception:
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
        today = app.query_day_stats(0)          # 今日实时，每次打开都查
        models = app.query_model_breakdown()
        work_hours = app.query_work_hours()

        # 问候语
        greeting = L(random.choice(app.GREETINGS))
        if len(greeting) > 20:
            greeting = greeting[:19] + "…"
        tk.Label(parent, text=greeting, fg=Design.TEXT_SECONDARY,
                 bg=Design.BACKGROUND, font=Design.FONT_UI).pack(fill=tk.X, pady=(0, 2))

        # 今日 Hero 卡：淡淡的品牌色底，大数字 + 指标列 + 24h 曲线都收进同一张卡
        self._hero_card(parent, today, work_hours)

        # 趋势 2×2 迷你卡（昨日 / 近7天 / 近30天 / 历史总量）
        trend = self._trend_data()
        if any((trend["yesterday"], trend["week"], trend["month"], trend["total"])):
            self._trend_grid(parent, trend)

        # 模型分布
        if models:
            self._section_header(parent, L("🤖 模型分布"),
                                 self._open(app.show_model_detail))
            max_total = models[0]["total"] if models else 1
            colors = model_colors()
            for idx, m in enumerate(models[:3]):
                name = m["model"]
                if len(name) > 14:
                    name = name[:14] + "…"
                self._model_row(parent, name, m["total"], max_total,
                                colors[idx % len(colors)])

        # 按钮栏（流水 / 洞察 / 设置 / 退出：4 格等宽，300px 面板放得下）
        self._button_bar(parent, [
            ("flow", L("流水"), self._on_flow),
            ("insight", L("洞察"), self._on_insights),
            ("settings", L("设置"), self._on_settings),
            ("quit", L("退出"), self._on_quit),
        ])

    def _trend_data(self):
        """趋势四卡的数据：每天第一次打开面板时查一次，当天后续直接复用。

        这些窗口含今天，缓存后数字会停在当天首次打开那一刻；今日实时数据
        仍由 Hero 卡单独查询，不受影响。跨天后自动重算。
        """
        day = datetime.now().strftime("%Y-%m-%d")
        if self._trend_cache["date"] == day and self._trend_cache["data"] is not None:
            return self._trend_cache["data"]

        app = self.app
        yesterday = app.query_day_stats(1)
        week = app.query_day_stats(7)
        month = app.query_day_stats(30)
        total = app.query_total_stats()
        data = {
            "yesterday": yesterday,
            "week": week,
            "month": month,
            "total": total,
            "prev_day": app.query_day_stats(2),
            "prev_week": self._previous_window_total(7, week),
            "prev_month": self._previous_window_total(30, month),
            "credits": self._window_credits(),
            "series_day": self._series_for_day(1),
            "series_week": self._daily_series(7),
            "series_month": self._daily_series(30),
        }
        self._trend_cache["date"] = day
        self._trend_cache["data"] = data
        return data

    # ---------------- 今日 Hero 卡 ----------------

    def _hero_card(self, parent, today, work_hours):
        """今日用量整块收进一张带品牌底色的圆角卡（替代原来的分隔线拼贴）"""
        import tkinter as tk

        fill = blend(Design.BRAND, Design.BACKGROUND, 0.10)
        card, body = rounded_card(parent, padx=12, pady=10, fill=fill)
        card.pack(fill=tk.X, pady=(4, 6))

        head = tk.Frame(body, bg=fill)
        head.pack(fill=tk.X)
        tk.Label(head, text=L("📊 今日用量"), fg=Design.TEXT_PRIMARY, bg=fill,
                 font=Design.FONT_SECTION, anchor='w').pack(side=tk.LEFT)
        tk.Label(head, text="›", fg=Design.TEXT_MUTED, bg=fill,
                 font=Design.FONT_CHEVRON).pack(side=tk.RIGHT)

        if today:
            # 大数字 + 今日积分（mac TodayCard：大数字后透出当日积分）
            big_row = tk.Frame(body, bg=fill)
            big_row.pack(fill=tk.X, pady=(4, 2))
            tk.Label(big_row, text=Design.fmt_tokens(today["total"]),
                     fg=today_usage_color(today["total"]), bg=fill,
                     font=Design.FONT_BIG, anchor='w').pack(side=tk.LEFT)
            credits = self._today_credits()
            if credits > 0:
                tk.Label(big_row, text=L("%s 积分") % format_credits(credits),
                         fg=Design.TEXT_SECONDARY, bg=fill,
                         font=Design.FONT_UI_SMALL, anchor='w').pack(
                             side=tk.LEFT, padx=(8, 2), pady=(0, 2))
            self._stat_columns(body, today, work_hours, bg=fill)

            # 今日每小时曲线（卡片内垫底，颜色按卡片底色混）
            self._hourly_sparkline(body, bg=fill)

            # 今日会话小字已按需求移除；页脚改为多账号积分拆分（见下）

            # 页脚：双账号且余额总额非 0 → 各账号今日积分拆分；单账号回落速率预测
            by_account = self._today_credits_by_account()
            if len(by_account) > 1:
                arow = tk.Frame(body, bg=fill)
                arow.pack(fill=tk.X, pady=(4, 0))
                for slot, val in by_account:
                    tk.Label(arow, text="●", fg=Design.SUCCESS if slot == 1 else Design.BRAND,
                             bg=fill, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                    tk.Label(arow, text=format_credits(val), fg=Design.TEXT_SECONDARY,
                             bg=fill, font=Design.FONT_UI_TINY).pack(
                        side=tk.LEFT, padx=(2, 10))
            else:
                # 速率预测（mac predictionText：工时 > 0.2h 才给）
                prediction = self._prediction_text(today, work_hours)
                if prediction:
                    tk.Label(body, text=prediction, fg=Design.TEXT_MUTED,
                             bg=fill, font=Design.FONT_UI_TINY,
                             anchor='w').pack(fill=tk.X, pady=(2, 0))

            self._bind_card(card, self._open(self.app.show_hourly_detail_today))
        elif self.app.store.attached:
            tk.Label(body, text=L("📊 今日暂无数据"), fg=Design.TEXT_MUTED, bg=fill,
                     font=Design.FONT_UI).pack(fill=tk.X, pady=8)
        else:
            self._stat_row(body, "🌶️", L("未找到数据源"), L("去设置"),
                           self._open(self.app.show_settings), bg=fill)

    # ---------------- 趋势 2×2 迷你卡 ----------------

    def _trend_grid(self, parent, trend):
        """四个窗口的迷你卡：数值 + 同比徽章 + 迷你曲线（历史卡用占比条）"""
        import tkinter as tk

        grid = tk.Frame(parent, bg=Design.BACKGROUND)
        grid.pack(fill=tk.X, pady=(2, 6))
        for col in range(2):
            grid.grid_columnconfigure(col, weight=1, uniform="trend")

        yesterday, week = trend["yesterday"], trend["week"]
        month, total = trend["month"], trend["total"]
        month_ratio = None
        if total and total.get("total"):
            month_ratio = (month or {}).get("total", 0) / total["total"]
        credits = trend["credits"]

        self._trend_card(grid, 0, 0, L("昨日"), yesterday, trend["prev_day"],
                         series=trend["series_day"],
                         credits=credits.get("day"),
                         command=self._open(self.app.show_hourly_detail_yesterday))
        self._trend_card(grid, 0, 1, L("近7天"), week, trend["prev_week"],
                         series=trend["series_week"],
                         credits=credits.get("week"),
                         command=self._open(self.app.show_weekly_detail))
        self._trend_card(grid, 1, 0, L("近30天"), month, trend["prev_month"],
                         series=trend["series_month"],
                         credits=credits.get("month"),
                         command=self._open(self.app.show_monthly_detail))
        self._trend_card(grid, 1, 1, L("历史总量"), total, None,
                         ratio=month_ratio,
                         credits=credits.get("total"),
                         command=self._open(self.app.show_all_time_detail))

    def _window_credits(self):
        """四个窗口的积分消耗（拿不到就返回空，卡片不显示积分行）"""
        try:
            store = self.app.store
            return {
                "day": store.query_credits_day(1),
                "week": store.query_credits_sum(7),
                "month": store.query_credits_sum(30),
                "total": store.query_credits_total(),
            }
        except Exception:
            return {}

    def _trend_card(self, grid, row, col, title, stats, prev=None,
                    series=None, ratio=None, credits=None, command=None):
        """单张趋势卡：标题 + 数值 + 积分小字 + 同比 + 26px 的迷你可视化"""
        import tkinter as tk

        value = int((stats or {}).get("total", 0) or 0)
        delta = self._delta_percent(value, prev)

        card, body = rounded_card(grid, padx=12, pady=8)
        card.grid(row=row, column=col, sticky="nsew", padx=4, pady=3)

        head = tk.Frame(body, bg=Design.CARD_FILL)
        head.pack(fill=tk.X)
        tk.Label(head, text=title, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(side=tk.LEFT)
        badge = None
        if delta is not None:
            badge = (delta[0], delta[1])
        elif ratio is not None:
            badge = (L("近30天 %d%%") % round(ratio * 100), Design.TEXT_MUTED)
        if badge:
            tk.Label(head, text=badge[0], fg=badge[1], bg=Design.CARD_FILL,
                     font=Design.FONT_UI_TINY, anchor='e').pack(side=tk.RIGHT)

        tk.Label(body, text=Design.fmt_tokens(value) if stats else "-",
                 fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                 font=Design.FONT_VALUE, anchor='w').pack(fill=tk.X, pady=(2, 0))

        try:
            credits = float(credits or 0)
        except (TypeError, ValueError):
            credits = 0.0
        if credits > 0:
            tk.Label(body, text=L("%s 积分") % format_credits(credits),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_TINY, anchor='w').pack(fill=tk.X)

        visual = tk.Canvas(body, height=26, bg=Design.CARD_FILL,
                           highlightthickness=0)
        visual.pack(fill=tk.X)

        if series and len(series) >= 2:
            def draw(_event=None, c=visual, v=list(series)):
                w = c.winfo_width()
                if w > 1:
                    ChartCanvas.draw_sparkline(c, v, w, 26, use_gradient=False,
                                               padding=3,
                                               background=Design.CARD_FILL)
            visual.bind("<Configure>", draw)
        elif ratio is not None:
            def draw_ratio(_event=None, c=visual, r=ratio):
                w = c.winfo_width()
                if w <= 1:
                    return
                c.delete("all")
                ChartCanvas.round_rect(c, 0, 11, w, 17, 3, fill=Design.CARD_BORDER)
                fill_w = w * min(max(r, 0.0), 1.0)
                if fill_w > 1:
                    ChartCanvas.round_rect(c, 0, 11, fill_w, 17, 3, fill=Design.BRAND)
            visual.bind("<Configure>", draw_ratio)

        if command is not None:
            self._bind_card(card, command)

    @staticmethod
    def _delta_percent(value, prev):
        """与上一等长窗口比较的百分比徽章：(文案, 颜色)；上一窗口没数据返回 None"""
        try:
            prev = int(prev or 0)
        except (TypeError, ValueError):
            return None
        if prev <= 0:
            return None
        pct = (int(value or 0) - prev) / prev * 100
        if abs(pct) < 0.5:
            return ("±0%", Design.TEXT_MUTED)
        if pct > 0:
            return ("▲ %d%%" % round(pct), Design.WARNING)
        return ("▼ %d%%" % round(-pct), Design.SUCCESS)

    def _previous_window_total(self, days, current):
        """前一个等长窗口的总量：2N 天总量减掉近 N 天"""
        try:
            longer = self.app.query_day_stats(days * 2) or {}
            current = current or {}
            return max(int(longer.get("total", 0)) - int(current.get("total", 0)), 0)
        except Exception:
            return None

    def _series_for_day(self, days_ago):
        """某一天的逐时序列（复用今日曲线的时间轴口径）"""
        try:
            hourly = self.app.query_hourly_stats(days_ago) or {}
        except Exception:
            return []
        return self._hour_values(hourly)

    def _daily_series(self, days):
        """近 N 天每日总量（缺数据的日期补 0），给迷你趋势线用"""
        try:
            rows = self.app.store.query_daily_tokens(days) or []
        except Exception:
            return []
        by_date = dict(rows)
        today = datetime.now()
        series = [by_date.get((today - timedelta(days=ago)).strftime("%Y-%m-%d"), 0)
                  for ago in range(days - 1, -1, -1)]
        return series if any(series) else []

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

    # ---------------- 今日卡补充（mac TodayCard / hourPoints / predictionText） ----------------

    def _today_credits(self):
        """今日积分消耗（Trae 口径；取不到按 0 处理）"""
        try:
            return float(self.app.store.query_today_credits() or 0)
        except Exception:
            return 0.0

    def _session_text(self):
        """今日会话汇总文案：N 个 · 平均 N 分钟 · 最长 N 分钟（口径同 mac sessionStats）"""
        try:
            rows = self.app.store.query_timeline() or []
        except Exception:
            return ""
        count, avg, longest = self.app._session_stats(rows)
        if count <= 0:
            return ""
        return L("今日会话 %d 个 · 均 %d 分 · 最长 %d 分") % (count, avg, longest)

    def _today_credits_by_account(self):
        """今日各账号积分（按槽位；仅双账号已认领且余额总额非 0 时返回 2 元素）"""
        try:
            accounts = self.app.store.query_today_credits_by_account() or []
        except Exception:
            return []
        if len(accounts) < 2:
            return []
        try:
            ent = self.app.store.trae_ent_summary()
        except Exception:
            ent = None
        if not ent or ent[1] <= 0:
            return []
        return accounts

    @staticmethod
    def _prediction_text(today, work_hours):
        """按当前速率折算到 24:00 的预计用量（工时 ≤ 0.2h 不外推，避免清早爆表）"""
        if not today or not work_hours:
            return None
        try:
            hours = float(work_hours)
        except (TypeError, ValueError):
            return None
        if hours <= 0.2:
            return None
        predicted = today["total"] / hours * 24
        return L("按当前速率到 24:00 约 %s") % Design.fmt_tokens(int(predicted))

    @staticmethod
    def _hour_values(hourly):
        """今日逐时 token 序列（mac hourPoints 口径）

        起点 = min(首个有数据的小时, 9)（9 点是稳定的工作日锚点），
        终点 = 最后一个有数据的小时；展开后不足两个点就不画（1 个点连不成线）。
        """
        def token_of(hour):
            d = (hourly or {}).get(hour) or {}
            return (d.get("output", 0) + d.get("input", 0)
                    + d.get("cache_read", 0) + d.get("cache_create", 0))

        hours = sorted(h for h in (hourly or {}) if token_of(h) > 0)
        if not hours:
            return []
        values = [token_of(h) for h in range(min(hours[0], 9), hours[-1] + 1)]
        return values if len(values) >= 2 else []

    def _hourly_sparkline(self, parent, bg=None):
        """今日每小时折线（画不出来时返回 None，绝不抛）；bg 传卡片底色"""
        import tkinter as tk

        try:
            hourly = self.app.query_hourly_stats(0) or {}
        except Exception:
            return None

        values = self._hour_values(hourly)
        if not values:
            return None

        bg = bg or Design.BACKGROUND
        canvas = tk.Canvas(parent, height=42, bg=bg, highlightthickness=0)
        canvas.pack(fill=tk.X, pady=(4, 0))

        def draw(_event=None):
            width = canvas.winfo_width()
            if width > 1:
                ChartCanvas.draw_sparkline(canvas, values, width, 42,
                                           use_gradient=False, background=bg)

        canvas.bind("<Configure>", draw)
        return canvas

    def _stat_columns(self, parent, today, work_hours, bg=None):
        """三列指标：请求数 / 缓存命中 / 时长"""
        import tkinter as tk

        bg = bg or Design.BACKGROUND
        total_input = today["input"] + today["cache_create"] + today["cache_read"]
        cache_rate = (today["cache_read"] / total_input * 100) if total_input > 0 else 0

        columns = [
            (L("请求数"), L("%d次") % today['reqs'], Design.TEXT_PRIMARY),
            (L("缓存命中"), f"{cache_rate:.0f}%",
             Design.SUCCESS if cache_rate > 80 else Design.WARNING),
        ]
        if work_hours:
            columns.append((L("时长"), f"{work_hours}h", Design.TEXT_PRIMARY))

        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, pady=(0, 4))
        for i, (label, value, color) in enumerate(columns):
            row.grid_columnconfigure(i, weight=1, uniform="stat")
            cell = tk.Frame(row, bg=bg)
            cell.grid(row=0, column=i, sticky="ew")
            tk.Label(cell, text=label, fg=Design.TEXT_MUTED, bg=bg,
                     font=Design.FONT_UI_SMALL).pack()
            tk.Label(cell, text=value, fg=color, bg=bg,
                     font=Design.FONT_VALUE).pack(pady=(2, 0))

    def _stat_row(self, parent, icon, title, value, command, bg=None):
        import tkinter as tk
        bg = bg or Design.BACKGROUND
        row = tk.Frame(parent, bg=bg, height=28)
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        tk.Label(row, text=icon, bg=bg,
                 font=Design.FONT_UI).pack(side=tk.LEFT)
        tk.Label(row, text=title, fg=Design.TEXT_PRIMARY, bg=bg,
                 font=Design.FONT_UI).pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(row, text="›", fg=Design.TEXT_MUTED, bg=bg,
                 font=Design.FONT_CHEVRON).pack(side=tk.RIGHT)
        tk.Label(row, text=value, fg=Design.TEXT_SECONDARY, bg=bg,
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

    @staticmethod
    def _draw_button_icon(canvas, kind, cx, cy, color):
        """按钮栏矢量图标：emoji 在不同机器上字形差异太大，这里统一画线条"""
        if kind == "flow":
            for dy in (-5, 0, 5):
                canvas.create_line(cx - 7, cy + dy, cx + 7, cy + dy,
                                   fill=color, width=1.4, capstyle="round")
        elif kind == "insight":
            for i, height in enumerate((5, 9, 13)):
                x = cx - 7 + i * 5
                canvas.create_rectangle(x, cy + 6 - height, x + 3, cy + 6,
                                        fill=color, outline="")
        elif kind == "settings":
            for dy, knob in ((-5, -2), (4, 3)):
                canvas.create_line(cx - 8, cy + dy, cx + 8, cy + dy,
                                   fill=color, width=1.4, capstyle="round")
                canvas.create_oval(cx + knob - 2, cy + dy - 2,
                                   cx + knob + 2, cy + dy + 2,
                                   fill=color, outline="")
        elif kind == "quit":
            canvas.create_arc(cx - 7, cy - 7, cx + 7, cy + 7, start=35, extent=290,
                              style="arc", outline=color, width=1.6)
            canvas.create_line(cx, cy - 9, cx, cy - 1, fill=color, width=1.6,
                               capstyle="round")
        else:
            canvas.create_text(cx, cy, text=kind, fill=color,
                               font=Design.FONT_BTN_ICON)

    def _button_bar(self, parent, buttons):
        """四宫格按钮：等宽圆角 + hover 高亮"""
        import tkinter as tk

        self._buttons = list(buttons)
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
                fill = Design.BTN_BG_HOVER if i == state["index"] else Design.CARD_FILL
                # 边框靠双层叠出来：round_rect 不支持描边（outline 会横穿中间）
                ChartCanvas.round_rect(bar, x1, 0, x2, self.BTN_HEIGHT, 8,
                                       fill=Design.CARD_BORDER)
                ChartCanvas.round_rect(bar, x1 + 1, 1, x2 - 1, self.BTN_HEIGHT - 1, 7,
                                       fill=fill)
                cx = (x1 + x2) / 2
                # mac BarActionButton：图标与文字都是 textSecondary
                color = Design.TEXT_PRIMARY if i == state["index"] else Design.TEXT_SECONDARY
                self._draw_button_icon(bar, icon, cx, 13, color)
                bar.create_text(cx, 28, text=label, fill=Design.TEXT_SECONDARY,
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
        """点开别的窗口前先收起面板（目标命令挂在闭包上，便于自测断言用对了窗口）"""
        def run():
            self.close()
            command()
        run._target = command
        return run

    def _bind_row(self, row, command):
        """整行可点：绑到行和它所有子控件，带 hover 高亮（命令留在行上便于自测）"""
        row._row_command = command

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

    def _bind_card(self, card, command):
        """卡片整块可点，但不加 hover 高亮（mac 的 TodayCard/ModelCard 只有 onTapGesture）。

        不能复用 _bind_row：它会把 hover 背景刷到所有后代上，而卡片的圆角垫底是
        place 管理的 Canvas，一染色就把圆角糊成方块。
        """
        card._row_command = command
        for widget in self._descendants(card):
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
            # place 管理的是卡片圆角垫底 Canvas，染色会把圆角糊成方块，跳过
            try:
                if w.winfo_manager() == "place":
                    continue
            except Exception:
                pass
            try:
                w.configure(bg=color)
            except Exception:
                pass

    # ---------------- 按钮动作 ----------------

    def _on_flow(self):
        """从面板打开洞察中心的「流水」页：先收起面板"""
        self.close()
        self.app.show_insights(initial_page=L("流水"))

    def _on_insights(self):
        """从面板打开洞察中心：先收起面板"""
        self.close()
        self.app.show_insights()

    def _on_settings(self):
        self.close()
        self.app.show_settings()

    def _on_quit(self):
        self.close()
        self.app.quit_app(self.app.icon, None)
