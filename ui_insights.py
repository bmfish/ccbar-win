"""洞察中心（费用 / 洞察 / 分享 / 渠道 / 流水 / 积分 / WorkBuddy），从 main.py 拆出。

作为 CcBarTray 的 mixin 使用：卡片、图表、分享卡等公共原语仍在 main，
通过 MRO 以 self._xxx 访问。
"""
from datetime import datetime, timedelta
import os
import subprocess
import time

import l10n
import share_card
import weekly_report
from l10n import L
from ui_dispatch import _on_gui
from ui_tabs import PillTabs
from ui_kit import (Design, blend, usage_color, today_usage_color,
                    model_colors, gradient_color, ChartCanvas, ChartReadout,
                    work_area, center_geometry, card_underlay, rounded_card,
                    card_title, section_title, muted_line, flat_button,
                    semantic_color, glow_text, format_credits,
                    TODAY_RED_THRESHOLD, _hex_to_rgb, _rgb_to_hex)


class InsightsWindowsMixin:
    @_on_gui
    def show_insights(self, icon=None, item=None, initial_page=None):
        """洞察中心：费用 / 洞察 / 分享 / 渠道 / 流水 / 积分 + WorkBuddy 七页。

        前六页与 macOS 版同口径；WorkBuddy 是 Windows 独有的独立源页签，
        数据来自独立表，绝不混进其他页。
        initial_page 覆盖页签记忆（面板「流水」按钮直达流水页）。

        这一页一次性查 20+ 项，全部丢后台线程；查完再回 GUI 线程建窗口，
        历史数据变长后也不会把界面堵死。
        """
        if self._reuse_window("insights"):
            return
        # 查询在后台跑、窗口还没建：先登记 pending，连点时由 _reuse_window 挡下
        self._window_registry()["insights"] = {"win": None, "pending": True}

        def failed(e):
            self._window_registry().pop("insights", None)
            from tkinter import messagebox
            messagebox.showerror(L("洞察中心加载失败"), str(e))

        self._run_async(self._collect_insights_data,
                        lambda data: self._render_insights(data, initial_page),
                        failed)

    def _collect_insights_data(self):
        """洞察中心的全量查询（后台线程执行，返回渲染所需的纯数据元组）"""
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
        # 积分页（Trae 口径）：0/7/30 天消耗 + 官方账单余额 + 近 30 天走势
        credits_today = store.query_credits_sum(0)
        credits7 = store.query_credits_sum(7)
        credits30 = store.query_credits_sum(30)
        credits_daily = store.query_credits_daily(30)
        try:
            credits_ent = store.trae_ent_summary()
        except Exception:
            credits_ent = None
        # WorkBuddy 页：独立表，只在这里出现。必须同步等一次交互档同步，
        # 否则本次打开读到的是上一轮数据（这里已在后台线程，可以安全阻塞）。
        self._prepare_workbuddy_sync()
        try:
            wb_today = store.workbuddy_credit_sum(0)
            wb7 = store.workbuddy_credit_sum(7)
            wb30 = store.workbuddy_credit_sum(30)
            wb_total_credit, wb_total_reqs, wb_first_epoch = store.workbuddy_total()
            wb_daily = store.workbuddy_credit_daily(30)
            wb_clients = store.workbuddy_client_breakdown(30)
            wb_models = store.workbuddy_model_breakdown(30, limit=10)
            wb_today_reqs = store.workbuddy_today_requests()
            wb_synced_at = store.workbuddy_synced_at()
            wb_status = store.source_status.get("workbuddy", "") or ""
        except Exception as e:
            logger.warning("WorkBuddy insights failed: %s", e)
            wb_today = wb7 = wb30 = wb_total_credit = 0.0
            wb_total_reqs = wb_today_reqs = wb_first_epoch = wb_synced_at = 0
            wb_daily, wb_clients, wb_models, wb_status = [], [], [], ""
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

        return (cost_today, cost7, cost30, cost_daily, cost_models, streak,
                this_week, last_week, peak, top, mtd, elapsed, dim, projected,
                total_all, channels, apps, comp, today_timeline, credits_today,
                credits7, credits30, credits_daily, credits_ent, wb_today, wb7,
                wb30, wb_total_credit, wb_total_reqs, wb_first_epoch, wb_daily,
                wb_clients, wb_models, wb_today_reqs, wb_synced_at, wb_status,
                budget, price, cost_mtd, mtd_elapsed, month_days, daily_budget,
                est, tokens91, weekday_totals, session_count, session_avg,
                session_longest, top_channels)

    @_on_gui
    def _render_insights(self, data, initial_page=None):
        """洞察中心渲染（GUI 线程；数据已由 _collect_insights_data 备好）"""
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        store = self.store
        (cost_today, cost7, cost30, cost_daily, cost_models, streak,
         this_week, last_week, peak, top, mtd, elapsed, dim, projected,
         total_all, channels, apps, comp, today_timeline, credits_today,
         credits7, credits30, credits_daily, credits_ent, wb_today, wb7,
         wb30, wb_total_credit, wb_total_reqs, wb_first_epoch, wb_daily,
         wb_clients, wb_models, wb_today_reqs, wb_synced_at, wb_status,
         budget, price, cost_mtd, mtd_elapsed, month_days, daily_budget,
         est, tokens91, weekday_totals, session_count, session_avg,
         session_longest, top_channels) = data

        root = tk.Toplevel(self._ui_root)
        root.title(L("洞察中心"))
        root.geometry(center_geometry("760x600"))
        root.minsize(700, 480)
        root.configure(bg=Design.BACKGROUND)

        style = ttk.Style(root)
        style.theme_use("default")
        style.configure("Insights.Treeview", background=Design.CARD_FILL, fieldbackground=Design.CARD_FILL,
                        foreground=Design.TEXT_PRIMARY, rowheight=24, borderwidth=0)
        style.configure("Insights.Treeview.Heading", background=Design.BACKGROUND,
                        foreground=Design.TEXT_MUTED, borderwidth=0)
        style.map("Insights.Treeview", background=[("selected", Design.CARD_BORDER)])
        style.configure("Insights.TCombobox", fieldbackground=Design.CARD_FILL,
                        background=Design.CARD_FILL, foreground=Design.TEXT_PRIMARY)

        nb = PillTabs(root)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 12))

        def tab(title):
            # 必须显式 nb.add()——PillTabs 和 Notebook 一样只认登记过的页
            f = tk.Frame(nb, bg=Design.BACKGROUND)
            nb.add(f, text=title)
            return f

        def png_label(frame, img):
            """图表卡片：圆角卡里放 PIL 图。

            图的底色必须和卡片填充同为 CARD_FILL，否则圆角处会露出一块方角深色。
            padx 必须 ≥ 圆角半径，否则内容 Frame 的方角会盖住卡片圆角。
            """
            card, body = rounded_card(frame, padx=12, pady=8)
            card.pack(fill=tk.X, padx=16, pady=(10, 4))
            lbl = tk.Label(body, bg=Design.CARD_FILL)
            photo = ImageTk.PhotoImage(img)
            lbl.configure(image=photo)
            lbl.image = photo
            lbl.pack(fill=tk.X)
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

        # 三个助手收敛到模块级原语，和设置页共用一套（原来两处各自实现、参数还不一样）
        def section(parent, text):
            section_title(parent, text, padx=16, pady=(12, 2))

        def muted(parent, text):
            muted_line(parent, text, padx=20)

        def btn(parent, text, cmd, primary=False):
            return flat_button(parent, text, cmd, primary)

        money = lambda v: f"${v:.2f}"

        # ============ 费用 ============
        f_cost = tab(L("费用"))
        cost_page, cost_wheel = scroll_page(f_cost)

        card_row = tk.Frame(cost_page, bg=Design.BACKGROUND)
        card_row.pack(fill=tk.X, padx=10, pady=(8, 0))
        for i, (title, value, estimate) in enumerate(
                [(L("今日费用"), cost_today, est[0]), (L("近 7 天"), cost7, est[7]),
                 (L("近 30 天"), cost30, est[30])]):
            card, body = rounded_card(card_row, padx=16, pady=12)
            card.grid(row=0, column=i, sticky="nsew", padx=6, pady=4)
            card_row.grid_columnconfigure(i, weight=1)
            tk.Label(body, text=title, fg=semantic_color("secondary"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            tk.Label(body, text=money(value + estimate), fg=Design.BIG_NUMBER, bg=Design.CARD_FILL,
                     font=Design.FONT_INSIGHT_VALUE, anchor='w').pack(fill=tk.X)
            if estimate > 0:
                # mac 版卡片小字：实测 $a · 估算 $b
                tk.Label(body, text=L("实测 %s · 估算 %s") % (money(value), money(estimate)),
                         fg=semantic_color("muted"), bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_TINY, anchor='w').pack(fill=tk.X)

        if budget > 0:
            # 月度预算卡（mac CostPage.budgetCard 同款文案与字段）
            ratio = cost_mtd / budget
            over = cost_mtd > budget
            projected_cost = cost_mtd / mtd_elapsed * month_days if mtd_elapsed else 0.0
            bcard, bbody = rounded_card(cost_page, padx=16, pady=12)
            bcard.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(bbody, text=L("本月预算 $%.2f") % budget, fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_TITLE, anchor='w').pack(fill=tk.X)
            # 预算进度条：胶囊形（圆角 = 半高），同 mac
            bar_h = Design.BAR_HEIGHT
            bar = tk.Canvas(bbody, height=bar_h, bg=Design.CARD_FILL, highlightthickness=0)
            bar.pack(fill=tk.X, pady=(8, 6))
            fill_ratio = min(max(ratio, 0.0), 1.0)

            def _draw_budget(event, c=bar, fr=fill_ratio):
                c.delete("all")
                w = max(event.width, 1)
                r = bar_h / 2
                ChartCanvas.round_rect(c, 0, 0, w, bar_h, r, fill=Design.BTN_BG)
                if fr > 0:
                    ChartCanvas.round_rect(c, 0, 0, max(w * fr, 2), bar_h, r,
                                           fill=usage_color(fr))

            bar.bind("<Configure>", _draw_budget)
            brow = tk.Frame(bbody, bg=Design.CARD_FILL)
            brow.pack(fill=tk.X)
            tk.Label(brow, text=L("本月已花 ") + money(cost_mtd), fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
            tk.Label(brow,
                     text=(L("已超预算 ") + money(cost_mtd - budget)) if over
                     else (L("剩余 ") + money(budget - cost_mtd)),
                     fg=Design.ERROR if over else Design.SUCCESS, bg=Design.CARD_FILL,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
            tk.Label(bbody,
                     text=L("按当前速率预计 %s · 已用预算 %.0f%%") % (money(projected_cost), ratio * 100),
                     fg=semantic_color("muted"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X, pady=(4, 0))

        section(cost_page, L("近 30 天费用走势"))
        cost_series = [(d[5:], {L("费用"): c}) for d, c in cost_daily]
        png_label(cost_page, self._bar_chart_png(
            cost_series, {L("费用"): Design.BRAND},
            budget_line=daily_budget if budget > 0 else None, value_fmt=money))

        section(cost_page, L("模型费用排行（近 30 天）"))
        if not cost_models:
            muted(cost_page, L("暂无数据"))
        for m, c, tok in cost_models:
            row = tk.Frame(cost_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=m, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=f"{money(c)} · {self.fmt_tokens(tok)}",
                     fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        # 性价比榜：token / $（cost > 0.005 才参与，避免除零噪声）
        section(cost_page, L("性价比榜（近 30 天）"))
        ranked = sorted(((m, tok / c) for m, c, tok in cost_models if c > 0.005),
                        key=lambda kv: kv[1], reverse=True)
        if not ranked:
            muted(cost_page, L("暂无数据"))
        for i, (m, per_dollar) in enumerate(ranked):
            row = tk.Frame(cost_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=f"{i + 1}. {m}", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=self.fmt_tokens(int(per_dollar)) + " / $1", fg=Design.DATA,
                     bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        foot = L("费用按 cc-switch 记录的单价折算；未计费渠道可在设置里配默认单价估算")
        if price > 0:
            foot += L("（当前按 $%g/M tokens 估算）") % price
        tk.Label(cost_page, text=foot, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                 wraplength=690).pack(fill=tk.X, padx=16, pady=(14, 8))
        cost_wheel(cost_page)

        # ============ 洞察 ============
        f_ins = tab(L("洞察"))
        ins_page, ins_wheel = scroll_page(f_ins)

        def export_long_image():
            """导出长图：把各页要点汇总成一张 PNG（mac「导出长图」）"""
            from tkinter import filedialog, messagebox
            blocks = [
                (L("费用"), [(L("今日费用"), money(cost_today + est[0])),
                          (L("近 7 天"), money(cost7 + est[7])),
                          (L("近 30 天"), money(cost30 + est[30])),
                          (L("本月已花"), money(cost_mtd))]),
                (L("用量"), [(L("连续使用"), L("%d 天") % streak),
                          (L("今日会话"), L("%d 个 · 平均 %d 分钟 · 最长 %d 分钟")
                           % (session_count, session_avg, session_longest)),
                          (L("本周用量"), self.fmt_tokens(this_week)),
                          (L("历史总量"), self.fmt_tokens(total_all)),
                          (L("单日峰值（近 30 天）"), self.fmt_tokens(peak[1]) if peak else "-"),
                          (L("预计本月消耗"), self.fmt_tokens(projected))]),
                (L("渠道（近 30 天）"), [(store.source_display_name(s), self.fmt_tokens(t))
                                      for s, t in top_channels] or [(L("暂无数据"), "-")]),
            ]
            path = filedialog.asksaveasfilename(
                title=L("导出洞察长图"), defaultextension=".png",
                initialfile="ccbar-insights-%s.png" % datetime.now().strftime("%Y%m%d"),
                filetypes=[("PNG", "*.png")])
            if not path:
                return
            try:
                share_card.insight_long_image(blocks, width=720, theme=self.theme).save(path)
            except Exception as e:
                messagebox.showerror(L("导出失败"), str(e))
                return
            messagebox.showinfo(L("导出完成"), L("已保存到：\n%s") % path)

        tools = tk.Frame(ins_page, bg=Design.BACKGROUND)
        tools.pack(fill=tk.X, padx=16, pady=(8, 0))
        btn(tools, L("导出长图"), export_long_image).pack(side=tk.RIGHT)

        grid = tk.Frame(ins_page, bg=Design.BACKGROUND)
        grid.pack(fill=tk.X, padx=10, pady=(6, 0))
        for c in range(2):
            grid.grid_columnconfigure(c, weight=1)
        delta_txt = ""
        if last_week > 0:
            delta_txt = f"{(this_week - last_week) / last_week * 100:+.0f}%"
        delta_badge = None
        if delta_txt:
            pct = (this_week - last_week) / last_week * 100
            if abs(pct) < 0.5:
                delta_badge = ("±0%", Design.TEXT_MUTED)
            elif pct > 0:
                delta_badge = (delta_txt, Design.WARNING)
            else:
                delta_badge = (delta_txt, Design.SUCCESS)
        peak_h = peak[0] if peak else "-"
        peak_v = self.fmt_tokens(peak[1]) if peak else "-"
        hour_rows = store.query_one("""
            SELECT ((created_at + ?) % 86400) / 3600 AS h, SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens)
            FROM usage_all WHERE created_at >= ? AND created_at < ? GROUP BY h ORDER BY 2 DESC LIMIT 1""",
            (int(datetime.now().utcoffset().total_seconds()) if datetime.now().utcoffset() else 28800,
             store.local_midnight(30), store.local_midnight(-1)))
        peak_hour_txt = f"{hour_rows[0]:02d}:00 – {(hour_rows[0] + 1) % 24:02d}:00" if hour_rows else "-"
        cards = [
            (L("🔥 连续使用"), L("%d 天") % streak, "", None),
            (L("今日会话"), L("%d 个") % session_count,
             L("平均 %d 分钟 · 最长 %d 分钟") % (session_avg, session_longest), None),
            (L("本周用量"), self.fmt_tokens(this_week), "", delta_badge),
            (L("日均用量（近 30 天）"),
             self.fmt_tokens((self.query_day_stats(30) or {"total": 0})["total"] // 30), "", None),
            (L("历史总量"), self.fmt_tokens(total_all), "", None),
            (L("单日峰值（近 30 天）"), peak_v, peak_h, None),
            (L("最活跃时段（近 30 天）"), peak_hour_txt, "", None),
            (L("使用量最大的模型（近 30 天）"), self.fmt_tokens(top[1]) if top else "-",
             top[0] if top else "", None),
            (L("预计本月消耗"), self.fmt_tokens(projected),
             L("按当前速率 · 本月已用 ") + self.fmt_tokens(mtd), None),
        ]
        for i, (t, v, sub, badge) in enumerate(cards):
            self._insight_card(grid, i // 2, i % 2, t, v, sub, badge=badge)

        # 星期分布（近 90 天）
        section(ins_page, L("星期分布（近 90 天）"))
        weekday_labels = [L("周一"), L("周二"), L("周三"), L("周四"), L("周五"), L("周六"), L("周日")]
        if not any(weekday_totals):
            muted(ins_page, L("暂无数据"))
        else:
            wd_series = [(weekday_labels[i], {weekday_labels[i]: weekday_totals[i]})
                         for i in range(7)]
            wd_colors = {weekday_labels[i]: Design.MODEL_COLORS[i % len(Design.MODEL_COLORS)]
                         for i in range(7)}
            png_label(ins_page, self._bar_chart_png(wd_series, wd_colors))

        # 近 90 天用量热力图（7 行 × 周数，首列按起始日 weekday 补位）
        section(ins_page, L("近 90 天用量热力图"))
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
        cell_size, gap = 15, 3
        left = 22
        canvas_w = max(left + weeks * (cell_size + gap) + 6, 120)
        rows_h = 7 * (cell_size + gap) + 8

        heat_card, heat_body = rounded_card(ins_page, padx=12, pady=10)
        heat_card.pack(fill=tk.X, padx=16, pady=(4, 6))
        heat = tk.Canvas(heat_body, bg=Design.CARD_FILL, highlightthickness=0,
                         width=canvas_w, height=rows_h)
        heat.pack(fill=tk.X)
        tip = tk.Label(heat, text="", bg=Design.BTN_BG, fg=Design.TEXT_PRIMARY,
                       font=Design.FONT_UI_TINY, padx=6, pady=2)

        def on_leave(_event=None):
            tip.place_forget()

        def make_enter(date_text, token, cx, cy):
            def enter(_event=None):
                tip.configure(text="%s · %s" % (date_text, self.fmt_tokens(token)))
                tip.place(x=min(cx + 12, max(canvas_w - 160, 12)), y=max(cy - 26, 2))
            return enter

        # 星期轴：只标周一/三/五，和 GitHub 热力图一样不挤
        for row, name in ((0, L("一")), (2, L("三")), (4, L("五"))):
            heat.create_text(6, 4 + row * (cell_size + gap) + cell_size / 2,
                             text=name, fill=Design.TEXT_MUTED,
                             font=Design.FONT_UI_TINY, anchor='w')

        for idx, (date_text, token) in enumerate(cells):
            row, col = idx % 7, idx // 7
            x1 = left + col * (cell_size + gap)
            y1 = 4 + row * (cell_size + gap)
            if token < 0:
                color = blend("#FFFFFF", Design.CARD_FILL, 0.03)      # 首周补位格
            elif token == 0:
                color = blend("#FFFFFF", Design.CARD_FILL, 0.07)      # 无用量
            else:
                # 主题品牌色由浅到浓，比固定红绿阶更贴合当前主题
                ratio = min(token / max_token, 1.0)
                color = blend(Design.BRAND, Design.CARD_FILL, 0.18 + 0.72 * ratio)
            tag = "cell%d" % idx
            ChartCanvas.round_rect(heat, x1, y1, x1 + cell_size, y1 + cell_size, 4,
                                   fill=color, outline="", tags=tag)
            if date_text:
                heat.tag_bind(tag, "<Enter>", make_enter(date_text, token, x1, y1))
                heat.tag_bind(tag, "<Leave>", on_leave)

        # 模型编年史（默认前 10 行，可展开/收起；模型治理菜单）
        section(ins_page, L("模型编年史"))
        chron_wrap = tk.Frame(ins_page, bg=Design.BACKGROUND)
        chron_wrap.pack(fill=tk.X, padx=16, pady=(4, 4))
        chron_state = {"expanded": False}
        # 编年史是全表 GROUP BY，一次打开只查一次；合并后再强制刷新
        chron_cache = {}

        def chronicle_rows(force=False):
            if force or "rows" not in chron_cache:
                chron_cache["rows"] = store.query_model_history() or []
            return chron_cache["rows"]

        def auto_merge_models():
            from tkinter import messagebox
            groups, changed = store.auto_merge_models()
            render_chronicle(force=True)
            if groups == 0:
                messagebox.showinfo(L("没有需要合并的模型"), L("大小写、厂商前缀不同的同名模型都已一致"))
            else:
                messagebox.showinfo(L("合并完成"), L("已合并 %d 组 · 改写 %d 行明细") % (groups, changed))

        def manual_merge_models():
            from tkinter import messagebox
            names = [m for m, _f, _l, _t in chronicle_rows()]
            if len(names) < 2:
                messagebox.showinfo(L("手动合并模型"), L("模型不足两个，无需合并"))
                return
            dlg = tk.Toplevel(root)
            dlg.title(L("手动合并模型"))
            dlg.configure(bg=Design.BACKGROUND)
            dlg.geometry("470x230")
            dlg.transient(root)
            tk.Label(dlg, text=L("手动合并模型"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor='w', padx=18, pady=(16, 8))
            from_var = tk.StringVar(value=names[0])
            to_var = tk.StringVar(value=names[0])
            for label, var in ((L("从"), from_var), (L("合并到"), to_var)):
                row = tk.Frame(dlg, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, padx=18, pady=3)
                tk.Label(row, text=label, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         width=6, anchor='w', font=Design.FONT_UI).pack(side=tk.LEFT)
                ttk.Combobox(row, textvariable=var, values=names, state="readonly",
                             width=32, style="Insights.TCombobox").pack(side=tk.LEFT)
            tk.Label(dlg, text=L("「从」模型的所有明细行会并入「到」模型，操作不可撤销"),
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
                render_chronicle(force=True)
                messagebox.showinfo(L("合并完成"), L("已改写 %d 行明细") % changed)

            btn(btns, L("取消"), dlg.destroy).pack(side=tk.RIGHT)
            btn(btns, L("合并"), do_merge, primary=True).pack(side=tk.RIGHT, padx=(0, 8))

        def render_chronicle(force=False):
            for w in chron_wrap.winfo_children():
                w.destroy()
            history = chronicle_rows(force)
            if not history:
                muted(chron_wrap, L("暂无数据"))
                return
            shown = history if chron_state["expanded"] else history[:10]
            colors = model_colors()
            rail_line = blend("#FFFFFF", Design.BACKGROUND, 0.16)
            for idx, (model, first_epoch, last_epoch, token) in enumerate(shown):
                row = tk.Frame(chron_wrap, bg=Design.BACKGROUND)
                row.pack(fill=tk.X, pady=1)
                # 时间轴：竖线 + 节点，首行从节点起、末行到节点止
                rail = tk.Canvas(row, width=16, height=24, bg=Design.BACKGROUND,
                                 highlightthickness=0)
                rail.pack(side=tk.LEFT)
                top = 12 if idx == 0 else 0
                bottom = 12 if idx == len(shown) - 1 else 24
                rail.create_line(8, top, 8, bottom, fill=rail_line, width=1)
                rail.create_oval(4, 8, 12, 16,
                                 fill=colors[idx % len(colors)], outline="")
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
                          text=(L("收起") if chron_state["expanded"]
                                else L("展开全部 %d 个模型") % len(history)),
                          command=toggle, bg=Design.BACKGROUND, fg=Design.BRAND,
                          activebackground=Design.BACKGROUND, activeforeground=Design.BRAND,
                          relief="flat", bd=0, cursor="hand2",
                          font=Design.FONT_UI_SMALL).pack(anchor='w', pady=(4, 0))
            menu_row = tk.Frame(chron_wrap, bg=Design.BACKGROUND)
            menu_row.pack(fill=tk.X, pady=(6, 0))
            mb = tk.Menubutton(menu_row, text=L("整理模型 ▾"), bg=Design.CARD_FILL,
                               fg=Design.TEXT_PRIMARY, activebackground=Design.BTN_BG_HOVER,
                               activeforeground=Design.TEXT_PRIMARY, relief="flat", bd=0,
                               cursor="hand2", font=Design.FONT_UI_SMALL, padx=10, pady=3)
            menu = tk.Menu(mb, tearoff=0, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                           activebackground=Design.BRAND, activeforeground=Design.TEXT_PRIMARY,
                           font=Design.FONT_UI_SMALL)
            menu.add_command(label=L("自动合并同名模型"), command=auto_merge_models)
            menu.add_command(label=L("手动合并…"), command=manual_merge_models)
            mb.configure(menu=menu)
            mb.pack(side=tk.RIGHT)
            ins_wheel(chron_wrap)

        render_chronicle()
        ins_wheel(ins_page)

        # ============ 分享 ============
        f_share = tab(L("分享"))
        share_page, share_wheel = scroll_page(f_share)

        def save_card_image(factory, prefix):
            from tkinter import filedialog, messagebox
            path = filedialog.asksaveasfilename(
                title=L("保存为图片"), defaultextension=".png",
                initialfile="%s-%s.png" % (prefix, datetime.now().strftime("%Y%m%d")),
                filetypes=[("PNG", "*.png")])
            if not path:
                return
            try:
                factory().save(path)
            except Exception as e:
                messagebox.showerror(L("保存失败"), str(e))
                return
            messagebox.showinfo(L("保存完成"), L("已保存到：\n%s") % path)

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
                messagebox.showerror(L("复制失败"), str(e))
                return
            if os.name == "nt":
                try:
                    subprocess.run(["powershell", "-c", "Set-Clipboard -Path '%s'" % path],
                                   check=True, capture_output=True, timeout=15)
                    messagebox.showinfo(L("已复制"), L("已复制到剪贴板"))
                    return
                except Exception:
                    pass
            messagebox.showinfo(L("已复制"), L("剪贴板不可用，已保存到临时文件：\n%s") % path)

        # 战报卡（今日 / 近 7 天 / 近 30 天 / 累计 + 近 7 天趋势）
        png_label(share_page, self._share_card_png())
        share_btns = tk.Frame(share_page, bg=Design.BACKGROUND)
        share_btns.pack(fill=tk.X, padx=16, pady=(2, 8))
        btn(share_btns, L("保存为图片"),
            lambda: save_card_image(self._share_card_png, "ccbar-share"),
            primary=True).pack(side=tk.LEFT)
        btn(share_btns, L("复制到剪贴板"),
            lambda: copy_card_image(self._share_card_png, "ccbar-share")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(share_btns, text=L("晒用量就是最好的宣传 ✨"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))

        tk.Frame(share_page, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=16, pady=6)
        wk_head = tk.Frame(share_page, bg=Design.BACKGROUND)
        wk_head.pack(fill=tk.X, padx=16, pady=(2, 0))
        tk.Label(wk_head, text=L("AI 用量周报（上周）"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
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

        btn(wk_head, L("打开周报目录"), open_weekly_dir).pack(side=tk.RIGHT)
        png_label(share_page, self._weekly_card_png())
        wk_btns = tk.Frame(share_page, bg=Design.BACKGROUND)
        wk_btns.pack(fill=tk.X, padx=16, pady=(2, 10))
        btn(wk_btns, L("保存为图片"),
            lambda: save_card_image(self._weekly_card_png, "ccbar-weekly"),
            primary=True).pack(side=tk.LEFT)
        btn(wk_btns, L("复制到剪贴板"),
            lambda: copy_card_image(self._weekly_card_png, "ccbar-weekly")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(wk_btns, text=L("每周一自动生成到周报目录 🗓"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))
        share_wheel(share_page)

        # ============ 渠道 ============
        f_ch = tab(L("渠道"))
        ch_page, ch_wheel = scroll_page(f_ch)
        section(ch_page, L("近 30 天渠道用量（堆叠）"))
        # 按显示名归并：cc-switch-rollup 是 cc-switch 的历史聚合源，
        # 直接按 source 分键会拆成两条都叫「cc-switch」的序列。
        by_date = {}
        for d, src, tok in channels:
            name = store.source_display_name(src)
            by_date.setdefault(d, {}).setdefault(name, 0)
            by_date[d][name] += tok
        src_names = sorted({n for v in by_date.values() for n in v})
        src_colors = {"cc-switch": "#4A90E2", "ZCode": "#34C759", "Trae": "#AF52DE"}
        # 兜底色按序号取；hash() 随 PYTHONHASHSEED 变，不能拿来定颜色
        palette = {n: src_colors.get(n, Design.MODEL_COLORS[i % len(Design.MODEL_COLORS)])
                   for i, n in enumerate(src_names)}
        ch_series = [(d[5:], by_date[d]) for d in sorted(by_date)]
        png_label(ch_page, self._bar_chart_png(ch_series, palette))

        section(ch_page, L("今日各渠道"))
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_map = {}
        for r in channels:
            if r[0] == today_str:
                n = store.source_display_name(r[1])
                today_map[n] = today_map.get(n, 0) + r[2]
        if not today_map:
            muted(ch_page, L("暂无数据"))
        for name, tok in sorted(today_map.items(), key=lambda x: -x[1]):
            row = tk.Frame(ch_page, bg=Design.BACKGROUND)
            row.pack(fill=tk.X, padx=20)
            tk.Label(row, text=f"● {name}", fg=Design.BRAND,
                     bg=Design.BACKGROUND, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(row, text=self.fmt_tokens(tok), fg=Design.DATA, bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        section(ch_page, L("近 30 天应用分布（堆叠）"))
        app_names = ["Claude Code", "Codex", "OpenCode", "ZCode"]
        app_map = {"claude": "Claude Code", "claude-desktop": "Claude Desktop", "codex": "Codex",
                   "opencode": "OpenCode", "zcode": "ZCode", "unknown": L("未知")}

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
        for i, n in enumerate(used_names):
            app_palette.setdefault(n, Design.MODEL_COLORS[i % len(Design.MODEL_COLORS)])
        app_series = [(d[5:], app_by_date[d]) for d in sorted(app_by_date)]
        png_label(ch_page, self._bar_chart_png(app_series, app_palette))

        section(ch_page, L("近 30 天 Token 构成"))
        comp_palette = {L("输入"): "#4A90E2", L("输出"): "#34C759", L("缓存读"): "#FF9500", L("缓存创建"): "#AF52DE"}
        comp_series = [(d, {L("输入"): i, L("输出"): o, L("缓存读"): cr, L("缓存创建"): cc})
                       for d, i, o, cr, cc in comp]
        png_label(ch_page, self._bar_chart_png(comp_series, comp_palette))

        section(ch_page, L("缓存命中率（近 30 天）"))
        hit_pts = [(d, cr / max(i + o + cr + cc, 1) * 100) for d, i, o, cr, cc in comp]
        png_label(ch_page, self._line_chart_png(hit_pts, Design.BRAND, suffix="%"))
        ch_wheel(ch_page)

        # ============ 流水 ============
        f_tl = tab(L("流水"))
        cols = (L("时间"), L("渠道"), L("模型"), "Token", L("费用"))
        tree = ttk.Treeview(f_tl, columns=cols, show="headings", height=19, style="Insights.Treeview")
        widths = (90, 90, 220, 110, 90)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor='w' if c in (L("渠道"), L("模型")) else 'e')
        vsb = ttk.Scrollbar(f_tl, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=10)
        tree.pack(fill=tk.BOTH, expand=True, padx=(12, 0), pady=(6, 10))

        filter_bar = tk.Frame(f_tl, bg=Design.BACKGROUND)
        filter_bar.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(filter_bar, text=L("筛选"), fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)

        tl_state = {"day": datetime.now().date(), "rows": []}

        def is_today(day):
            return day == datetime.now().date()

        def day_label(day):
            today = datetime.now().date()
            if day == today:
                return L("今天")
            if day == today - timedelta(days=1):
                return L("昨天")
            return L("%d月%d日") % (day.month, day.day)

        export_btn = btn(filter_bar, L("导出 CSV"), lambda: export_timeline_csv())
        export_btn.pack(side=tk.RIGHT)
        model_box = ttk.Combobox(filter_bar, values=[L("全部模型")], state="readonly",
                                 width=18, style="Insights.TCombobox")
        model_box.set(L("全部模型"))
        model_box.pack(side=tk.RIGHT, padx=(0, 8))
        src_box = ttk.Combobox(filter_bar, values=[L("全部渠道")], state="readonly",
                               width=12, style="Insights.TCombobox")
        src_box.set(L("全部渠道"))
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
                L("选择日期"), L("输入日期（YYYY-MM-DD）："),
                initialvalue=tl_state["day"].strftime("%Y-%m-%d"), parent=root)
            if not text:
                return
            try:
                day = datetime.strptime(text.strip(), "%Y-%m-%d").date()
            except ValueError:
                messagebox.showerror(L("日期格式不对"), L("请按 YYYY-MM-DD 输入，例如 2026-08-01"))
                return
            set_day(min(day, datetime.now().date()))

        prev_btn = btn(filter_bar, L("‹ 前一天"), lambda: step_day(-1))
        prev_btn.configure(padx=8)
        prev_btn.pack(side=tk.LEFT)
        day_btn = btn(filter_bar, day_label(tl_state["day"]), choose_day)
        day_btn.configure(padx=10)
        day_btn.pack(side=tk.LEFT, padx=(4, 0))
        next_btn = btn(filter_bar, L("后一天 ›"), lambda: step_day(1))
        next_btn.configure(padx=8)
        next_btn.pack(side=tk.LEFT, padx=(4, 0))
        today_chip = btn(filter_bar, L("今天"), lambda: set_day(datetime.now().date()))
        today_chip.configure(fg=Design.BRAND)

        def filtered_rows():
            src_sel, model_sel = src_box.get(), model_box.get()
            out = []
            for r in tl_state["rows"]:
                if src_sel != L("全部渠道") and store.source_display_name(r[2]) != src_sel:
                    continue
                if model_sel != L("全部模型") and r[1] != model_sel:
                    continue
                out.append(r)
            return out

        def refill(*_):
            tree.delete(*tree.get_children())
            for ts, model, source, token, cost, credits in filtered_rows():
                t = datetime.fromtimestamp(ts)
                if credits > 0:
                    amount = L("%s 积分") % self._fmt_credits(credits)
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
            src_box.configure(values=[L("全部渠道")] + sorted(
                {store.source_display_name(r[2]) for r in tl_state["rows"]}))
            model_box.configure(values=[L("全部模型")] + sorted({r[1] for r in tl_state["rows"]}))
            src_box.set(L("全部渠道"))
            model_box.set(L("全部模型"))
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
                title=L("导出流水"), defaultextension=".csv",
                initialfile="ccbar-timeline-%s.csv" % tl_state["day"].strftime("%Y%m%d"),
                filetypes=[("CSV", "*.csv")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([L("时间"), L("模型"), L("渠道"), L("总 Token"), L("费用/积分")])
                    for ts, model, source, token, cost, credits in rows:
                        # 与流水页一致：Trae 行导积分，其他源导美元费用
                        if source == "trae":
                            amount = L("%s 积分") % self._fmt_credits(credits)
                        else:
                            amount = "%.4f" % cost
                        writer.writerow([
                            datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            model, source, token, amount])
            except OSError as e:
                messagebox.showerror(L("导出失败"), str(e))
                return
            messagebox.showinfo(L("导出完成"), L("已导出 %d 行到：\n%s") % (len(rows), path))

        src_box.bind("<<ComboboxSelected>>", refill)
        model_box.bind("<<ComboboxSelected>>", refill)
        refresh_day()

        # ============ 积分 ============
        # 顺序与 mac InsightsPage 枚举一致：费用 / 洞察 / 分享 / 渠道 / 流水 / 积分
        f_cred = tab(L("积分"))
        cred_page, cred_wheel = scroll_page(f_cred)

        cred_row = tk.Frame(cred_page, bg=Design.BACKGROUND)
        cred_row.pack(fill=tk.X, padx=10, pady=(8, 0))
        for i, (title, value) in enumerate([(L("今日积分"), credits_today),
                                            (L("近 7 天"), credits7),
                                            (L("近 30 天"), credits30)]):
            card, body = rounded_card(cred_row, padx=16, pady=12)
            card.grid(row=0, column=i, sticky="nsew", padx=6, pady=4)
            cred_row.grid_columnconfigure(i, weight=1)
            tk.Label(body, text=title, fg=semantic_color("secondary"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            tk.Label(body, text=format_credits(value), fg=Design.BIG_NUMBER,
                     bg=Design.CARD_FILL, font=Design.FONT_INSIGHT_VALUE,
                     anchor='w').pack(fill=tk.X)

        if credits_ent:
            consumed, ent_total = credits_ent
            ecard, ebody = rounded_card(cred_page, padx=16, pady=12)
            ecard.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(ebody, text=L("积分余额（官方账单）"), fg=semantic_color("secondary"),
                     bg=Design.CARD_FILL, font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            erow = tk.Frame(ebody, bg=Design.CARD_FILL)
            erow.pack(fill=tk.X, pady=(4, 0))
            tk.Label(erow, text=format_credits(consumed), fg=Design.DATA,
                     bg=Design.CARD_FILL, font=Design.FONT_INSIGHT_VALUE,
                     anchor='w').pack(side=tk.LEFT)
            tk.Label(erow, text=L("已用 / 共 %s") % format_credits(ent_total),
                     fg=semantic_color("secondary"), bg=Design.CARD_FILL,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT, padx=(10, 0))
            ent_ratio = (consumed / ent_total) if ent_total > 0 else 0.0
            if ent_total > 0:
                tk.Label(erow, text="%.0f%%" % (ent_ratio * 100), fg=Design.BRAND,
                         bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                # 胶囊进度条
                bar_h = Design.BAR_HEIGHT
                bar = tk.Canvas(ebody, height=bar_h, bg=Design.CARD_FILL, highlightthickness=0)
                bar.pack(fill=tk.X, pady=(8, 0))
                fill_ratio = min(max(ent_ratio, 0.0), 1.0)

                def _draw_ent(event, c=bar, fr=fill_ratio):
                    c.delete("all")
                    w = max(event.width, 1)
                    r = bar_h / 2
                    ChartCanvas.round_rect(c, 0, 0, w, bar_h, r, fill=Design.BTN_BG)
                    if fr > 0:
                        ChartCanvas.round_rect(c, 0, 0, max(w * fr, 2), bar_h, r,
                                               fill=blend(Design.BRAND, Design.CARD_FILL, 0.75))

                bar.bind("<Configure>", _draw_ent)

        section(cred_page, L("近 30 天积分走势"))
        if credits30 <= 0:
            muted(cred_page, L("接入 Trae 并产生用量后展示积分消耗"))
        elif not credits_daily:
            muted(cred_page, L("暂无数据"))
        else:
            # 逐柱取主题模型色板（与 mac CreditsPage / 模型分布卡同一机制）
            palette = model_colors(max(len(credits_daily), 1))
            cred_series = [(d[5:], {d: v}) for d, v in credits_daily]
            cred_colors = {d: palette[i % len(palette)]
                           for i, (d, _v) in enumerate(credits_daily)}
            png_label(cred_page, self._bar_chart_png(cred_series, cred_colors,
                                                     value_fmt=format_credits))

        tk.Label(cred_page,
                 text=L("积分为 Trae 官方计费口径；历史数据自接入起最多回溯 90 天"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w', justify=tk.LEFT,
                 wraplength=690).pack(fill=tk.X, padx=16, pady=(14, 8))
        cred_wheel(cred_page)

        # ============ WorkBuddy（独立页签，不与其他页面共享口径）============
        f_wb = tab(L("WorkBuddy"))
        wb_page, wb_wheel = scroll_page(f_wb)

        wb_row_cards = tk.Frame(wb_page, bg=Design.BACKGROUND)
        wb_row_cards.pack(fill=tk.X, padx=10, pady=(8, 0))
        for i, (title, value) in enumerate([(L("今日积分"), wb_today),
                                            (L("近 7 天"), wb7),
                                            (L("近 30 天"), wb30),
                                            (L("历史总积分"), wb_total_credit)]):
            card, body = rounded_card(wb_row_cards, padx=16, pady=12)
            card.grid(row=0, column=i, sticky="nsew", padx=6, pady=4)
            wb_row_cards.grid_columnconfigure(i, weight=1)
            tk.Label(body, text=title, fg=semantic_color("secondary"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            tk.Label(body, text=format_credits(value), fg=Design.BIG_NUMBER,
                     bg=Design.CARD_FILL, font=Design.FONT_INSIGHT_VALUE,
                     anchor='w').pack(fill=tk.X)

        # 明细口径说明：请求数 / 首次接入 / 上次同步（都是本页专属，不进其他页）
        detail_line = L("今日请求 %d · 历史请求 %d") % (wb_today_reqs, wb_total_reqs)
        if wb_first_epoch:
            detail_line += L(" · 首次 %s") % time.strftime(
                "%Y-%m-%d", time.localtime(wb_first_epoch))
        if wb_synced_at:
            detail_line += L(" · 上次同步 %s") % time.strftime(
                "%H:%M", time.localtime(wb_synced_at))
        tk.Label(wb_page, text=detail_line, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X, padx=16, pady=(8, 0))

        section(wb_page, L("按客户端（近 30 天）"))
        if not wb_clients:
            muted(wb_page, L("接入 WorkBuddy 并产生用量后展示积分消耗"))
        else:
            client_max = max(c[1] for c in wb_clients) or 1.0
            for name, credit, reqs in wb_clients:
                line = tk.Frame(wb_page, bg=Design.BACKGROUND)
                line.pack(fill=tk.X, padx=16, pady=2)
                tk.Label(line, text=name, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=Design.FONT_UI, width=16, anchor='w').pack(side=tk.LEFT)
                tk.Label(line, text=format_credits(credit), fg=Design.DATA,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL,
                         width=12, anchor='e').pack(side=tk.LEFT)
                tk.Label(line, text=L("%d 次请求") % reqs, fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                         anchor='w').pack(side=tk.LEFT, padx=(12, 0))
                bar = tk.Canvas(line, height=Design.BAR_HEIGHT, bg=Design.BACKGROUND,
                                highlightthickness=0, width=180)
                bar.pack(side=tk.RIGHT)

                def _draw_client(event, c=bar, ratio=min(credit / client_max, 1.0)):
                    c.delete("all")
                    r = Design.BAR_HEIGHT / 2
                    ChartCanvas.round_rect(c, 0, 0, max(event.width, 1), Design.BAR_HEIGHT,
                                           r, fill=Design.BTN_BG)
                    if ratio > 0:
                        ChartCanvas.round_rect(c, 0, 0, max(event.width * ratio, 2),
                                               Design.BAR_HEIGHT, r,
                                               fill=blend(Design.BRAND, Design.CARD_FILL, 0.75))

                bar.bind("<Configure>", _draw_client)

        section(wb_page, L("模型排行（近 30 天）"))
        if not wb_models:
            muted(wb_page, L("暂无数据"))
        else:
            for name, credit, reqs in wb_models:
                line = tk.Frame(wb_page, bg=Design.BACKGROUND)
                line.pack(fill=tk.X, padx=16, pady=2)
                tk.Label(line, text=name, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                         font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
                tk.Label(line, text=L("%d 次请求") % reqs, fg=Design.TEXT_MUTED,
                         bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                         anchor='w').pack(side=tk.LEFT, padx=(12, 0))
                tk.Label(line, text=format_credits(credit), fg=Design.DATA,
                         bg=Design.BACKGROUND, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

        section(wb_page, L("近 30 天积分走势"))
        if wb30 <= 0 or not wb_daily:
            muted(wb_page, L("接入 WorkBuddy 并产生用量后展示积分消耗"))
        else:
            wb_palette = model_colors(max(len(wb_daily), 1))
            wb_series = [(d[5:], {d: v}) for d, v in wb_daily]
            wb_colors = {d: wb_palette[i % len(wb_palette)]
                         for i, (d, _v) in enumerate(wb_daily)}
            png_label(wb_page, self._bar_chart_png(wb_series, wb_colors,
                                                   value_fmt=format_credits))

        hint = L("WorkBuddy 数据独立统计，不计入费用 / 渠道 / 流水 / 积分等页面")
        if wb_status:
            hint = L(wb_status) + " · " + hint
        tk.Label(wb_page, text=hint, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                 wraplength=690).pack(fill=tk.X, padx=16, pady=(14, 8))
        wb_wheel(wb_page)

        # ============ 页签记忆 ============
        titles = [nb.tab(t, "text") for t in nb.tabs()]
        last_page = initial_page or self.settings.get("insights_last_page") or L("费用")
        if last_page not in titles:
            last_page = L("费用")
        try:
            nb.select(titles.index(last_page))
        except Exception:
            pass

        def select_page(name):
            labels = [nb.tab(t, "text") for t in nb.tabs()]
            if name in labels:
                nb.select(labels.index(name))

        # 已开着时再次点「流水」等入口：直接切页签，不重复建窗
        self._register_window(
            "insights", root,
            on_reuse=(lambda: select_page(initial_page)) if initial_page else None)

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
