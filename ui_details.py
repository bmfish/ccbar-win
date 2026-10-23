"""详情窗口（每小时 / 模型 / 近7天 / 近30天 / 历史总量），从 main.py 拆出。

作为 CcBarTray 的 mixin 使用：统计卡、表格、图表等公共原语仍在 main 的
UiHelpers 区（self._detail_* / self._bar_chart_png 等），通过 MRO 访问。
"""
from datetime import datetime, timedelta

import l10n
from l10n import L
from ui_dispatch import _on_gui
from ui_kit import (Design, blend, usage_color, today_usage_color,
                    model_colors, gradient_color, ChartCanvas, ChartReadout,
                    work_area, center_geometry, card_underlay, rounded_card,
                    card_title, section_title, muted_line, flat_button,
                    semantic_color, glow_text, format_credits,
                    TODAY_RED_THRESHOLD, _hex_to_rgb, _rgb_to_hex)


class DetailWindowsMixin:
    def show_hourly_detail_today(self, icon=None, item=None):
        """显示今日每小时详情"""
        self.show_hourly_detail(days_ago=0)

    def show_hourly_detail_yesterday(self, icon=None, item=None):
        """显示昨日每小时详情"""
        self.show_hourly_detail(days_ago=1)

    @_on_gui
    def show_hourly_detail(self, days_ago=0, date_str=None):
        """显示每小时详情窗口

        对齐 mac HourlyDetailWindowController：统计卡（总 Token / 请求数 / 峰值时段）、
        峰值高亮柱状图（带拖选读数）、‹ / 日期 / › / 回到今天 导航、CSV 导出与位置记忆。
        """
        if self._reuse_window("hourly"):
            return
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title(L("每小时用量详情"))
        self._apply_window_geometry(root, "hourly", "520x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(460, 400)
        self._remember_window_geometry(root, "hourly")

        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                target_date = datetime.now()
        else:
            target_date = datetime.now() - timedelta(days=days_ago)
        state = {"date": target_date.replace(hour=0, minute=0, second=0, microsecond=0),
                 "days_ago": days_ago, "export_rows": []}

        # 顶部导航：‹ / 日期文本 / › / 回到今天（仅在非今天显示，› 在今天禁用）
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def shift(delta):
            state["date"] += timedelta(days=delta)
            state["days_ago"] = (datetime.now().date() - state["date"].date()).days
            refresh()

        def prev_day():
            shift(-1)

        def next_day():
            if state["days_ago"] > 0:
                shift(1)

        def go_today():
            state["date"] = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            state["days_ago"] = 0
            refresh()

        def export_csv():
            self._export_detail_csv(L("导出每小时用量"), L("ccbar-每小时.csv"),
                                    [L("时间"), L("请求数"), L("总Token"), L("缓存读")],
                                    state["export_rows"])

        self._detail_nav_button(nav, "‹", prev_day).pack(side=tk.LEFT)
        date_label = tk.Label(nav, text="", font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                              bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)
        self._detail_export_button(nav, export_csv).pack(side=tk.RIGHT)
        today_chip = self._detail_today_chip(nav, go_today)
        today_chip.pack(side=tk.RIGHT, padx=(0, 6))
        next_btn = self._detail_nav_button(nav, "›", next_day)
        next_btn.pack(side=tk.RIGHT)

        # 统计卡行
        _holder, set_stats = self._detail_stats_holder(root)

        # 柱状图（canvas 要等布局完成才有真实宽度，一建好就画会全挤在左边）
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))
        chart_canvas = tk.Canvas(chart_frame, height=110, bg=Design.BACKGROUND,
                                 highlightthickness=0)
        chart_canvas.pack(fill=tk.X)
        chart_state = {}
        ChartReadout(chart_canvas, chart_state, kind="bars")

        def draw_chart(_event=None):
            if not chart_state:
                chart_canvas.delete("all")
                return
            width = chart_canvas.winfo_width()
            if width <= 1:
                return
            ChartCanvas.draw_bar_chart(chart_canvas, chart_state["values"], width, 110,
                                       labels=chart_state["labels"], use_gradient=True,
                                       hue_offset=chart_state["hue_offset"],
                                       highlight_index=chart_state.get("peak"))

        chart_canvas.bind("<Configure>", draw_chart)

        # 表头 + 可滚动数据区
        self._detail_table_header(root, [(L("时间"), 7, 'w'), (L("请求数"), 9, 'e'),
                                         (L("总token"), 11, 'e'), (L("缓存读"), 11, 'e')])
        inner, bind_wheel = self._detail_scroll_area(root)

        def refresh():
            date_label.config(text=state["date"].strftime("%y-%m-%d"))
            if state["days_ago"] == 0:
                today_chip.pack_forget()
                next_btn.configure(state=tk.DISABLED)
            else:
                today_chip.pack(side=tk.RIGHT, before=next_btn, padx=(0, 6))
                next_btn.configure(state=tk.NORMAL)

            for widget in inner.winfo_children():
                widget.destroy()
            state["export_rows"] = []

            hourly_data = self.query_hourly_stats(state["days_ago"])
            hours_with_data = [h for h, d in (hourly_data or {}).items() if d["reqs"] > 0]
            if not hourly_data or not hours_with_data:
                set_stats([])
                chart_state.clear()
                chart_canvas.delete("all")
                tk.Label(inner, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                return

            hours = list(range(min(hours_with_data), max(hours_with_data) + 1))

            def hour_token(hour):
                d = hourly_data.get(hour) or {}
                return (d.get("output", 0) + d.get("input", 0)
                        + d.get("cache_read", 0) + d.get("cache_create", 0))

            total_reqs = sum(d["reqs"] for d in hourly_data.values())
            total_token = sum(hour_token(h) for h in hours)
            total_cache = sum((hourly_data.get(h) or {}).get("cache_read", 0) for h in hours)

            # 峰值时段按 token 取（mac 同口径），图表也按 token 画
            values = [hour_token(h) for h in hours]
            peak_index = values.index(max(values)) if max(values) > 0 else None
            peak_hour = hours[peak_index] if peak_index is not None else None

            day_of_year = state["date"].timetuple().tm_yday
            chart_state.clear()
            chart_state.update(values=values, labels=hours,
                               hue_offset=(day_of_year % 6) / 6.0, peak=peak_index)
            draw_chart()

            set_stats([
                (L("总 Token"), Design.fmt_tokens(total_token), None),
                (L("请求数"), "%d" % total_reqs, None),
                (L("峰值时段"), (L("%d时") % peak_hour) if peak_hour is not None else "-",
                 Design.WARNING),
            ])

            # 合计行
            self._detail_total_row(inner, [(L("合计"), 7, 'w'), (L("%d次") % total_reqs, 9, 'e'),
                                           (Design.fmt_tokens(total_token), 11, 'e'),
                                           (Design.fmt_tokens(total_cache), 11, 'e')])
            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            for hour in hours:
                d = hourly_data.get(hour) or {"reqs": 0, "output": 0, "input": 0,
                                              "cache_read": 0, "cache_create": 0}
                token = hour_token(hour)
                cache_read = d.get("cache_read", 0)
                state["export_rows"].append([L("%d时") % hour, d["reqs"], token, cache_read])
                self._detail_data_row(inner, [
                    (L("%d时") % hour, 7, 'w', Design.GRADIENT[0]),
                    (L("%d次") % d["reqs"] if d["reqs"] > 0 else "-", 9, 'e',
                     Design.TEXT_PRIMARY if d["reqs"] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(token), 11, 'e',
                     Design.TEXT_PRIMARY if token > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(cache_read), 11, 'e',
                     Design.TEXT_SECONDARY if cache_read > 0 else Design.TEXT_MUTED),
                ], highlight=(peak_hour is not None and hour == peak_hour))

        refresh()
        bind_wheel()
        self._register_window("hourly", root)
        self._bring_to_front(root)

    def show_weekly_detail(self, icon=None, item=None):
        """显示近7天详情"""
        self.show_daily_detail(days=7, title=L("近7天用量"))

    def show_monthly_detail(self, icon=None, item=None):
        """显示近30天详情"""
        self.show_daily_detail(days=30, title=L("近30天用量"))

    @_on_gui
    def show_model_detail(self, icon=None, item=None):
        """显示模型分布详情（环形图 + 分渠道表格）

        对齐 mac ModelDetailWindowController：统计卡（总 Token / 模型数 / Top1 占比）、
        环形图与图例、‹ / 日期 / › / 回到今天 导航、CSV 导出与位置记忆。
        """
        if self._reuse_window("model"):
            return
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title(L("模型分布详情"))
        self._apply_window_geometry(root, "model", "620x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(520, 420)
        self._remember_window_geometry(root, "model")

        state = {"date": datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
                 "days_ago": 0, "export_rows": []}

        # 顶部导航：‹ / 日期文本 / › / 回到今天
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def shift(delta):
            state["date"] += timedelta(days=delta)
            state["days_ago"] = (datetime.now().date() - state["date"].date()).days
            refresh_model()

        def prev_day():
            shift(-1)

        def next_day():
            if state["days_ago"] > 0:
                shift(1)

        def go_today():
            state["date"] = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            state["days_ago"] = 0
            refresh_model()

        def export_csv():
            self._export_detail_csv(L("导出模型分布"), L("ccbar-模型分布.csv"),
                                    [L("模型"), L("请求数"), L("总Token"), L("缓存读")],
                                    state["export_rows"])

        self._detail_nav_button(nav, "‹", prev_day).pack(side=tk.LEFT)
        date_label = tk.Label(nav, text="", font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                              bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)
        self._detail_export_button(nav, export_csv).pack(side=tk.RIGHT)
        today_chip = self._detail_today_chip(nav, go_today)
        today_chip.pack(side=tk.RIGHT, padx=(0, 6))
        next_btn = self._detail_nav_button(nav, "›", next_day)
        next_btn.pack(side=tk.RIGHT)

        _holder, set_stats = self._detail_stats_holder(root)

        # 图表区：左侧环形图 + 右侧图例
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))

        donut_canvas = tk.Canvas(chart_frame, width=160, height=160,
                                 bg=Design.BACKGROUND, highlightthickness=0)
        donut_canvas.pack(side=tk.LEFT)

        legend_frame = tk.Frame(chart_frame, bg=Design.BACKGROUND)
        legend_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        # 表头 + 可滚动数据区
        self._detail_table_header(root, [(L("模型"), 22, 'w'), (L("请求数"), 9, 'e'),
                                         (L("总token"), 11, 'e'), (L("缓存读"), 11, 'e')])
        inner, bind_wheel = self._detail_scroll_area(root)

        def refresh_model():
            for widget in inner.winfo_children():
                widget.destroy()
            for widget in legend_frame.winfo_children():
                widget.destroy()
            state["export_rows"] = []

            date_label.config(text=state["date"].strftime("%y-%m-%d"))
            if state["days_ago"] == 0:
                today_chip.pack_forget()
                next_btn.configure(state=tk.DISABLED)
            else:
                today_chip.pack(side=tk.RIGHT, before=next_btn, padx=(0, 6))
                next_btn.configure(state=tk.NORMAL)

            models = [m for m in (self.query_model_breakdown_by_day(state["days_ago"]) or [])
                      if m["total_token"] > 0 or m["reqs"] > 0]
            if not models:
                set_stats([])
                donut_canvas.delete("all")
                tk.Label(inner, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                return

            total_reqs = sum(m["reqs"] for m in models)
            total_token = sum(m["total_token"] for m in models)
            total_cache = sum(m["cache_read"] for m in models)

            # 模型配色（按小时轮换，同一小时内稳定）
            colors = model_colors()

            # 环形图：跨渠道按模型合并，展示整体分布（取前 6）
            merged = {}
            for m in models:
                merged[m["model"]] = merged.get(m["model"], 0) + m["total_token"]
            merged_list = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)

            items = [(token, colors[idx % len(colors)], name)
                     for idx, (name, token) in enumerate(merged_list[:6])]
            ChartCanvas.draw_donut(donut_canvas, items, 160)

            top_share = (merged_list[0][1] / total_token * 100) if total_token > 0 else 0
            set_stats([
                (L("总 Token"), Design.fmt_tokens(total_token), None),
                (L("模型数"), "%d" % len(merged_list), None),
                (L("Top1 占比"), "%.0f%%" % top_share, Design.WARNING),
            ])

            # 图例
            for idx, (model_name, token) in enumerate(merged_list[:6]):
                color = colors[idx % len(colors)]
                pct = (token / total_token * 100) if total_token > 0 else 0
                short_name = model_name[:16] + "…" if len(model_name) > 16 else model_name

                item_frame = tk.Frame(legend_frame, bg=Design.BACKGROUND)
                item_frame.pack(fill=tk.X, pady=2)

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

                    color = (colors[idx % len(colors)]
                             if gi == 0 and idx < 6 else Design.TEXT_MUTED)
                    dot = tk.Canvas(row, width=10, height=10,
                                    bg=Design.BACKGROUND, highlightthickness=0)
                    dot.create_oval(1, 1, 9, 9, fill=color, outline="")
                    dot.pack(side=tk.LEFT, padx=(0, 4))

                    short_name = (m["model"][:18] + "…"
                                  if len(m["model"]) > 18 else m["model"])
                    tk.Label(row, text=short_name, width=20, anchor='w',
                             fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                             font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                    tk.Label(row, text=L("%d次") % m['reqs'], width=9, anchor='e',
                             fg=Design.TEXT_PRIMARY if m['reqs'] > 0 else Design.TEXT_MUTED,
                             bg=Design.BACKGROUND,
                             font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                    tk.Label(row, text=Design.fmt_tokens(m['total_token']), width=11,
                             anchor='e',
                             fg=(Design.TEXT_PRIMARY if m['total_token'] > 0
                                 else Design.TEXT_MUTED),
                             bg=Design.BACKGROUND,
                             font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                    tk.Label(row, text=Design.fmt_tokens(m['cache_read']), width=11,
                             anchor='e',
                             fg=(Design.TEXT_SECONDARY if m['cache_read'] > 0
                                 else Design.TEXT_MUTED),
                             bg=Design.BACKGROUND,
                             font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                    state["export_rows"].append([m["model"], m["reqs"], m["total_token"],
                                                 m["cache_read"]])

            # 合计行（mac 同款收口）
            self._detail_total_row(inner, [(L("合计"), 20, 'w'), (L("%d次") % total_reqs, 9, 'e'),
                                           (Design.fmt_tokens(total_token), 11, 'e'),
                                           (Design.fmt_tokens(total_cache), 11, 'e')])

        refresh_model()
        bind_wheel()
        self._register_window("model", root)
        self._bring_to_front(root)

    @_on_gui
    def show_daily_detail(self, days=7, title=None):
        """显示每日详情窗口（近7天 / 近30天）

        对齐 mac DetailWindowController / MonthDetailWindowController：统计卡
        （总 Token / 日均 / 请求数 / 缓存读）、折线图（峰值点高亮 + 拖选读数）、
        ‹ / 日期文本 / › / 回到今天 导航、CSV 导出与位置记忆。
        """
        if self._reuse_window("daily-%d" % days):
            return
        import tkinter as tk

        title = L(title) if title else L("近7天用量")
        root = tk.Toplevel(self._ui_root)
        root.title(title)
        geom_key = "week" if days == 7 else "month"
        self._apply_window_geometry(root, geom_key, "560x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(480, 400)
        self._remember_window_geometry(root, geom_key)

        state = {"date": datetime.now(), "export_rows": []}

        # 顶部导航：‹ / 日期文本 / › / 回到今天（周期可能是周一到周日或自然月）
        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))

        def can_go_next():
            now = datetime.now()
            if days == 7:
                return state["date"] + timedelta(days=7) <= now
            return self._add_months(state["date"], 1) <= now

        def is_current_period():
            now = datetime.now()
            if days == 7:
                return (state["date"].date() - timedelta(days=state["date"].weekday())
                        == now.date() - timedelta(days=now.weekday()))
            return (state["date"].year, state["date"].month) == (now.year, now.month)

        def prev_period():
            state["date"] = (state["date"] - timedelta(days=7) if days == 7
                             else self._add_months(state["date"], -1))
            refresh_daily()

        def next_period():
            if can_go_next():
                state["date"] = (state["date"] + timedelta(days=7) if days == 7
                                 else self._add_months(state["date"], 1))
                refresh_daily()

        def go_current():
            state["date"] = datetime.now()
            refresh_daily()

        def export_csv():
            name = L("ccbar-近7天.csv") if days == 7 else L("ccbar-近30天.csv")
            self._export_detail_csv(L("导出%s") % title, name,
                                    [L("日期"), L("请求数"), L("总Token"), L("缓存读")],
                                    state["export_rows"])

        self._detail_nav_button(nav, "‹", prev_period).pack(side=tk.LEFT)
        date_label = tk.Label(nav, text="", font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                              bg=Design.BACKGROUND)
        date_label.pack(side=tk.LEFT, expand=True)
        self._detail_export_button(nav, export_csv).pack(side=tk.RIGHT)
        today_chip = self._detail_today_chip(nav, go_current)
        today_chip.pack(side=tk.RIGHT, padx=(0, 6))
        next_btn = self._detail_nav_button(nav, "›", next_period)
        next_btn.pack(side=tk.RIGHT)

        _holder, set_stats = self._detail_stats_holder(root)

        # 折线图（理由同每小时详情：等布局完成有宽度再画）
        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))
        chart_canvas = tk.Canvas(chart_frame, height=90, bg=Design.BACKGROUND,
                                 highlightthickness=0)
        chart_canvas.pack(fill=tk.X)
        chart_state = {}
        ChartReadout(chart_canvas, chart_state, kind="line")

        def draw_chart(_event=None):
            if not chart_state:
                chart_canvas.delete("all")
                return
            width = chart_canvas.winfo_width()
            if width <= 1:
                return
            ChartCanvas.draw_sparkline(chart_canvas, chart_state["values"], width, 90,
                                       use_gradient=True,
                                       hue_offset=chart_state["hue_offset"],
                                       highlight_index=chart_state.get("peak"))

        chart_canvas.bind("<Configure>", draw_chart)

        # 表头 + 可滚动数据区
        self._detail_table_header(root, [(L("日期"), 8, 'w'), (L("请求数"), 9, 'e'),
                                         (L("总token"), 11, 'e'), (L("缓存读"), 11, 'e')])
        inner, bind_wheel = self._detail_scroll_area(root)

        def refresh_daily():
            for widget in inner.winfo_children():
                widget.destroy()
            state["export_rows"] = []

            if is_current_period():
                today_chip.pack_forget()
            else:
                today_chip.pack(side=tk.RIGHT, before=next_btn, padx=(0, 6))
            next_btn.configure(state=tk.NORMAL if can_go_next() else tk.DISABLED)

            if days == 7:
                # 周窗口：周一到周日
                week_start = state["date"] - timedelta(days=state["date"].weekday())
                week_end = week_start + timedelta(days=6)
                start_date = week_start.strftime("%Y-%m-%d")
                end_date = week_end.strftime("%Y-%m-%d")
                date_label.config(text="%s ~ %s" % (week_start.strftime("%y-%m-%d"),
                                                    week_end.strftime("%y-%m-%d")))
                hue_offset = (week_start.isocalendar()[1] % 8) / 8.0
            else:
                # 月窗口：自然月
                month_start = state["date"].replace(day=1)
                month_end = self._add_months(month_start, 1) - timedelta(days=1)
                start_date = month_start.strftime("%Y-%m-%d")
                end_date = min(month_end, datetime.now()).strftime("%Y-%m-%d")
                date_label.config(text=state["date"].strftime("%y-%m"))
                hue_offset = ((state["date"].month * 3) % 8) / 8.0

            daily_data = self.query_daily_stats_for_range(start_date, end_date) or {}
            sorted_dates = sorted(daily_data.keys())
            if not sorted_dates:
                set_stats([])
                chart_state.clear()
                chart_canvas.delete("all")
                tk.Label(inner, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                return

            def day_token(key):
                d = daily_data[key]
                return d.get("output", 0) + d.get("input", 0) + d.get("cache_read", 0)

            total_reqs = sum(d["reqs"] for d in daily_data.values())
            total_token = sum(day_token(key) for key in sorted_dates)
            total_cache = sum(d["cache_read"] for d in daily_data.values())
            days_with_data = sum(1 for key in sorted_dates if day_token(key) > 0)
            daily_avg = total_token // days_with_data if days_with_data > 0 else 0

            values = [day_token(key) for key in sorted_dates]
            peak_token = max(values) if values else 0
            peak_index = values.index(peak_token) if peak_token > 0 else None
            chart_state.clear()
            chart_state.update(values=values,
                               labels=[key[5:].replace("-", "/") for key in sorted_dates],
                               hue_offset=hue_offset, peak=peak_index)
            draw_chart()

            set_stats([
                (L("总 Token"), Design.fmt_tokens(total_token), None),
                (L("日均"), Design.fmt_tokens(daily_avg), None),
                (L("请求数"), "%d" % total_reqs, None),
                (L("缓存读"), Design.fmt_tokens(total_cache), None),
            ])

            # 合计行
            self._detail_total_row(inner, [(L("合计"), 8, 'w'), (L("%d次") % total_reqs, 9, 'e'),
                                           (Design.fmt_tokens(total_token), 11, 'e'),
                                           (Design.fmt_tokens(total_cache), 11, 'e')])
            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            for key in sorted_dates:
                d = daily_data[key]
                token = day_token(key)
                cache_read = d["cache_read"]
                state["export_rows"].append([key, d["reqs"], token, cache_read])
                self._detail_data_row(inner, [
                    (key[5:].replace("-", "/"), 8, 'w', Design.GRADIENT[0]),
                    (L("%d次") % d["reqs"] if d["reqs"] > 0 else "-", 9, 'e',
                     Design.TEXT_PRIMARY if d["reqs"] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(token), 11, 'e',
                     Design.TEXT_PRIMARY if token > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(cache_read), 11, 'e',
                     Design.TEXT_SECONDARY if cache_read > 0 else Design.TEXT_MUTED),
                ], highlight=(peak_token > 0 and token == peak_token))

        refresh_daily()
        bind_wheel()
        self._register_window("daily-%d" % days, root)
        self._bring_to_front(root)

    @_on_gui
    def show_all_time_detail(self, icon=None, item=None):
        """显示历史总量窗口（按月汇总）

        对齐 mac AllTimeDetailWindowController：历史总量 / 月均 / 最佳月 / 请求数
        四张统计卡、峰值月高亮柱状图（最早 → 最新）、CSV 导出与位置记忆；
        今日实时用量并入当前月（daily_agg 不含今天）。
        """
        if self._reuse_window("alltime"):
            return
        import tkinter as tk

        root = tk.Toplevel(self._ui_root)
        root.title(L("历史总量"))
        self._apply_window_geometry(root, "alltime", "560x620")
        root.configure(bg=Design.BACKGROUND)
        root.minsize(480, 420)
        self._remember_window_geometry(root, "alltime")

        state = {"export_rows": []}

        nav = tk.Frame(root, bg=Design.BACKGROUND)
        nav.pack(fill=tk.X, padx=16, pady=(12, 4))
        tk.Label(nav, text=L("按月汇总"), font=Design.FONT_MONO, fg=Design.TEXT_PRIMARY,
                 bg=Design.BACKGROUND).pack(side=tk.LEFT, expand=True)

        def export_csv():
            self._export_detail_csv(L("导出历史总量"), L("ccbar-按月汇总.csv"),
                                    [L("月份"), L("请求数"), L("总Token"), L("缓存读")],
                                    state["export_rows"])

        self._detail_export_button(nav, export_csv).pack(side=tk.RIGHT)

        _holder, set_stats = self._detail_stats_holder(root)

        chart_frame = tk.Frame(root, bg=Design.BACKGROUND)
        chart_frame.pack(fill=tk.X, padx=16, pady=(8, 4))
        chart_canvas = tk.Canvas(chart_frame, height=100, bg=Design.BACKGROUND,
                                 highlightthickness=0)
        chart_canvas.pack(fill=tk.X)
        chart_state = {}
        ChartReadout(chart_canvas, chart_state, kind="bars")

        def draw_chart(_event=None):
            if not chart_state:
                chart_canvas.delete("all")
                return
            width = chart_canvas.winfo_width()
            if width <= 1:
                return
            ChartCanvas.draw_bar_chart(chart_canvas, chart_state["values"], width, 100,
                                       labels=chart_state["labels"], use_gradient=True,
                                       hue_offset=chart_state["hue_offset"],
                                       highlight_index=chart_state.get("peak"))

        chart_canvas.bind("<Configure>", draw_chart)

        self._detail_table_header(root, [(L("月份"), 8, 'w'), (L("请求数"), 9, 'e'),
                                         (L("总token"), 11, 'e'), (L("缓存读"), 11, 'e')])
        inner, bind_wheel = self._detail_scroll_area(root)

        def refresh_all():
            for widget in inner.winfo_children():
                widget.destroy()
            state["export_rows"] = []

            rows = list(self.store.query_monthly_totals(limit=36) or [])
            # 今日实时并入当前月（daily_agg 不含今天）
            today = self.query_day_stats(0)
            month = self._day_str(0)[:7]
            if today and (today["reqs"] > 0 or today["total"] > 0):
                for i, r in enumerate(rows):
                    if r[0] == month:
                        rows[i] = (r[0], r[1] + today["reqs"], r[2] + today["total"],
                                   r[3] + today["cache_read"])
                        break
                else:
                    rows.insert(0, (month, today["reqs"], today["total"],
                                    today["cache_read"]))

            if not rows:
                set_stats([])
                chart_state.clear()
                chart_canvas.delete("all")
                tk.Label(inner, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI).pack(pady=20)
                return

            total_reqs = sum(r[1] for r in rows)
            total_token = sum(r[2] for r in rows)
            total_cache = sum(r[3] for r in rows)
            peak_token = max(r[2] for r in rows)
            best_month = max(rows, key=lambda r: r[2])[0]

            set_stats([
                (L("历史总量"), Design.fmt_tokens(total_token), None),
                (L("月均"), Design.fmt_tokens(total_token // max(len(rows), 1)), None),
                (L("最佳月"), best_month, Design.WARNING),
                (L("请求数"), "%d" % total_reqs, None),
            ])

            # 图表：最近月份在最前，倒过来让时间从左到右
            ordered = list(reversed(rows))
            values = [r[2] for r in ordered]
            chart_state.clear()
            chart_state.update(values=values, labels=[r[0] for r in ordered],
                               hue_offset=0.35,
                               peak=values.index(peak_token) if peak_token > 0 else None)
            draw_chart()

            # 合计行
            self._detail_total_row(inner, [(L("合计"), 8, 'w'), (L("%d次") % total_reqs, 9, 'e'),
                                           (Design.fmt_tokens(total_token), 11, 'e'),
                                           (Design.fmt_tokens(total_cache), 11, 'e')])
            tk.Frame(inner, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=3)

            for r in rows:
                state["export_rows"].append([r[0], r[1], r[2], r[3]])
                self._detail_data_row(inner, [
                    (r[0], 8, 'w', Design.GRADIENT[0]),
                    (L("%d次") % r[1] if r[1] > 0 else "-", 9, 'e',
                     Design.TEXT_PRIMARY if r[1] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(r[2]), 11, 'e',
                     Design.TEXT_PRIMARY if r[2] > 0 else Design.TEXT_MUTED),
                    (Design.fmt_tokens(r[3]), 11, 'e',
                     Design.TEXT_SECONDARY if r[3] > 0 else Design.TEXT_MUTED),
                ], highlight=(peak_token > 0 and r[2] == peak_token))

        refresh_all()
        bind_wheel()
        self._register_window("alltime", root)
        self._bring_to_front(root)
