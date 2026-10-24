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

        # 洞察页「全部」范围：最多回溯 730 天的逐日 token + 首用时间 + 近 30 天时段分布
        ins_daily = store.query_daily_tokens(730)
        ins_first = store.query_first_usage_epoch()
        peak_hour_map = store.query_hour_histogram(30)
        # 费用 / 渠道 / 积分页的 90 天范围切换（窄窗口在页面里按日期切片）
        cost90 = store.query_cost(90)
        cost_daily90 = store.query_cost_daily(90)
        channels90 = store.query_channel_daily(90)
        apps90 = store.query_app_daily(90)
        comp90 = store.query_composition_daily(90)
        credits_daily90 = store.query_credits_daily(90)
        credits_yday = store.query_credits_day(1)
        # 渠道页今日构成 + 费用页单次会话费用
        channel_today = store.query_channel_today()
        session30, session_cost30 = store.query_session_cost(30)

        return (cost_today, cost7, cost30, cost_daily, cost_models, streak,
                this_week, last_week, peak, top, mtd, elapsed, dim, projected,
                total_all, channels, apps, comp, today_timeline, credits_today,
                credits7, credits30, credits_daily, credits_ent, wb_today, wb7,
                wb30, wb_total_credit, wb_total_reqs, wb_first_epoch, wb_daily,
                wb_clients, wb_models, wb_today_reqs, wb_synced_at, wb_status,
                budget, price, cost_mtd, mtd_elapsed, month_days, daily_budget,
                est, tokens91, weekday_totals, session_count, session_avg,
                session_longest, top_channels, ins_daily, ins_first, peak_hour_map,
                cost90, cost_daily90, channels90, apps90, comp90, credits_daily90,
                channel_today, session30, session_cost30, credits_yday)

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
         session_longest, top_channels, ins_daily, ins_first, peak_hour_map,
         cost90, cost_daily90, channels90, apps90, comp90, credits_daily90,
         channel_today, session30, session_cost30, credits_yday) = data

        root = tk.Toplevel(self._ui_root)
        root.title(L("洞察中心"))
        root.geometry(center_geometry("1000x700"))
        root.minsize(900, 560)
        root.configure(bg=Design.BACKGROUND)

        style = ttk.Style(root)
        style.theme_use("default")
        style.configure("Insights.Treeview", background=Design.CARD_FILL, fieldbackground=Design.CARD_FILL,
                        foreground=Design.TEXT_PRIMARY, rowheight=24, borderwidth=0)
        style.configure("Insights.Treeview.Heading", background=Design.BACKGROUND,
                        foreground=Design.TEXT_MUTED, borderwidth=0)
        style.map("Insights.Treeview", background=[("selected", Design.CARD_BORDER)])
        style.configure("Insights.TCombobox", fieldbackground=Design.CARD_FILL,
                        background=Design.CARD_FILL, foreground=Design.TEXT_PRIMARY,
                        arrowcolor=Design.TEXT_SECONDARY,
                        selectbackground=Design.CARD_FILL,
                        selectforeground=Design.TEXT_PRIMARY)
        # 只读态默认会用白色 fieldbackground，必须显式映射，否则筛选框是白块
        style.map("Insights.TCombobox",
                  fieldbackground=[("readonly", Design.CARD_FILL)],
                  background=[("readonly", Design.CARD_FILL)],
                  foreground=[("readonly", Design.TEXT_PRIMARY)],
                  arrowcolor=[("readonly", Design.TEXT_SECONDARY)],
                  selectbackground=[("readonly", Design.CARD_FILL)],
                  selectforeground=[("readonly", Design.TEXT_PRIMARY)])

        nb = PillTabs(root, vertical=True)
        nb.pack(fill=tk.BOTH, expand=True, padx=(4, 12), pady=(8, 12))

        # 左侧导航图标（页序：洞察/积分/流水/渠道/费用/分享，
        # 之后是 Windows 独有的 WorkBuddy，最底部是设置入口）
        TAB_ICONS = {
            L("费用"): "◷",
            L("洞察"): "∿",
            L("分享"): "⇧",
            L("渠道"): "▤",
            L("流水"): "≡",
            L("积分"): "⚡",
            L("WorkBuddy"): "◈",
            L("设置"): "⚙",
        }

        def tab(title):
            # 必须显式 nb.add()——PillTabs 和 Notebook 一样只认登记过的页
            f = tk.Frame(nb, bg=Design.BACKGROUND)
            nb.add(f, text=title, icon=TAB_ICONS.get(title, "•"))
            return f

        def png_label(frame, img, padx=16, pady=(10, 4)):
            """图表卡片：圆角卡里放 PIL 图。

            图的底色必须和卡片填充同为 CARD_FILL，否则圆角处会露出一块方角深色。
            padx 必须 ≥ 圆角半径，否则内容 Frame 的方角会盖住卡片圆角。
            """
            card, body = rounded_card(frame, padx=12, pady=8)
            card.pack(fill=tk.X, padx=padx, pady=pady)
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

        def range_pills(parent, specs, initial, on_pick):
            """右上角分段切换（费用/渠道/积分页共用）。specs: [(key, label)]"""
            card, body = rounded_card(parent, padx=4, pady=4, radius=14)
            btns = {}

            def set_active(key):
                for k, b in btns.items():
                    sel = k == key
                    b.configure(bg=Design.BTN_BG if sel else Design.CARD_FILL,
                                fg=Design.TEXT_PRIMARY if sel else Design.TEXT_SECONDARY)

            for key, label in specs:
                b = tk.Button(body, text=label, bd=0, relief=tk.FLAT, cursor="hand2",
                              bg=Design.CARD_FILL, fg=Design.TEXT_SECONDARY,
                              activebackground=Design.BTN_BG_HOVER,
                              activeforeground=Design.TEXT_PRIMARY,
                              font=Design.FONT_UI_SMALL, padx=10, pady=2,
                              command=lambda k=key: (set_active(k), on_pick(k)))
                b.pack(side=tk.LEFT)
                btns[key] = b
            set_active(initial)
            return card, set_active

        def stat_row(parent, items):
            """卡片底部的「标题 + 数值」横排（费用卡片用）"""
            row = tk.Frame(parent, bg=Design.CARD_FILL)
            row.pack(fill=tk.X, pady=(8, 0))
            for i, (title, value) in enumerate(items):
                cell = tk.Frame(row, bg=Design.CARD_FILL)
                cell.grid(row=0, column=i, sticky="w")
                row.grid_columnconfigure(i, weight=1, uniform="stat")
                tk.Label(cell, text=title, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)
                tk.Label(cell, text=value, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                         font=Design.FONT_TREND_VALUE, anchor='w').pack(fill=tk.X)
            return row

        def share_bar(parent, ratio, fill=None):
            """模型费用行下方的细进度条"""
            c = tk.Canvas(parent, height=4, bg=Design.CARD_FILL, highlightthickness=0)
            c.pack(fill=tk.X, pady=(2, 0))
            fr = min(max(ratio, 0.0), 1.0)

            def draw(event, cv=c):
                cv.delete("all")
                w = max(event.width, 1)
                ChartCanvas.round_rect(cv, 0, 0, w, 4, 2, fill=Design.BTN_BG)
                if fr > 0:
                    ChartCanvas.round_rect(cv, 0, 0, max(w * fr, 2), 4, 2,
                                           fill=fill or Design.SUCCESS)
            c.bind("<Configure>", draw)
            return c

        money = lambda v: f"${v:.2f}"

        # 先按导航顺序登记页签：洞察 → 积分 → 流水 → 渠道 → 费用 → 分享
        # → WorkBuddy → 设置。页面内容仍按下方代码区块顺序往里填，互不影响。
        page_frame = {t: tab(t) for t in (
            L("洞察"), L("积分"), L("流水"), L("渠道"),
            L("费用"), L("分享"), L("WorkBuddy"), L("设置"))}

        # ============ 费用 ============
        f_cost = page_frame[L("费用")]
        cost_page, cost_wheel = scroll_page(f_cost)
        cost_body = tk.Frame(cost_page, bg=Design.BACKGROUND)
        cost_body.pack(fill=tk.X)

        cost_head = tk.Frame(cost_body, bg=Design.BACKGROUND)
        cost_head.pack(fill=tk.X, padx=18, pady=(10, 2))
        clog = tk.Frame(cost_head, bg=Design.BACKGROUND)
        clog.pack(side=tk.LEFT)
        tk.Label(clog, text=L("费用"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(clog, text=L("按官方计费口径 · 美元"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(anchor='w')

        cost_range = {"days": 30}

        def on_cost_range(days):
            cost_range["days"] = days
            render_cost()

        cost_pills, _cost_set = range_pills(
            cost_head, [(7, L("7 天")), (30, L("30 天")), (90, L("90 天"))],
            30, on_cost_range)
        cost_pills.pack(side=tk.RIGHT)

        cost_area = tk.Frame(cost_body, bg=Design.BACKGROUND)
        cost_area.pack(fill=tk.X, padx=10, pady=(4, 10))

        def render_cost():
            for w in cost_area.winfo_children():
                w.destroy()
            yday_text = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            cost_map = dict(cost_daily)
            cost_map90 = dict(cost_daily90)
            yday_cost = cost_map.get(yday_text, 0.0)
            # 近 7 天迷你走势（缺失日补 0）
            spark = []
            for i in range(6, -1, -1):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                spark.append(cost_map.get(day, cost_map90.get(day, 0.0)))
            top_row = tk.Frame(cost_area, bg=Design.BACKGROUND)
            top_row.pack(fill=tk.X, padx=6)
            top_row.grid_columnconfigure(0, weight=3, uniform="costtop")
            top_row.grid_columnconfigure(1, weight=2, uniform="costtop")

            # ---- 左：今日费用 ----
            hcard, hbody = rounded_card(top_row, padx=16, pady=14)
            hcard.grid(row=0, column=0, sticky="nsew", padx=6, pady=4)
            hhead = tk.Frame(hbody, bg=Design.CARD_FILL)
            hhead.pack(fill=tk.X)
            tk.Label(hhead, text=L("今日费用"), fg=Design.TEXT_SECONDARY,
                     bg=Design.CARD_FILL, font=Design.FONT_UI).pack(side=tk.LEFT)
            if yday_cost > 0:
                pct = (cost_today - yday_cost) / yday_cost * 100
                tk.Label(hhead, text=L("较昨日 %s%.0f%%") % ("▲ " if pct >= 0 else "▼ ", abs(pct)),
                         fg=Design.WARNING if pct >= 0 else Design.SUCCESS,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            tk.Label(hbody, text=money(cost_today + est[0]), fg=Design.BIG_NUMBER,
                     bg=Design.CARD_FILL, font=Design.FONT_BIG,
                     anchor='w').pack(fill=tk.X, pady=(2, 0))
            spark_photo = ImageTk.PhotoImage(
                self._sparkline_png(spark, Design.SUCCESS, width=300, height=64))
            png_label_body = tk.Label(hbody, bg=Design.CARD_FILL, image=spark_photo)
            png_label_body.image = spark_photo
            png_label_body.pack(fill=tk.X, pady=(6, 4))
            tk.Frame(hbody, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=(2, 6))
            avg30 = cost30 / 30.0
            stat_row(hbody, [(L("近 7 天"), money(cost7 + est[7])),
                             (L("近 30 天"), money(cost30 + est[30])),
                             (L("日均"), money(avg30))])

            # ---- 右：本月 ----
            mcard, mbody = rounded_card(top_row, padx=16, pady=14)
            mcard.grid(row=0, column=1, sticky="nsew", padx=6, pady=4)
            tk.Label(mbody, text=L("本月"), fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            mrow = tk.Frame(mbody, bg=Design.CARD_FILL)
            mrow.pack(fill=tk.X, pady=(2, 0))
            tk.Label(mrow, text=money(cost_mtd), fg=Design.BIG_NUMBER, bg=Design.CARD_FILL,
                     font=("Microsoft YaHei UI", 22, "bold"), anchor='w').pack(side=tk.LEFT)
            if budget > 0:
                tk.Label(mrow, text=L("已用 %.0f%%") % (cost_mtd / budget * 100),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))
            else:
                tk.Label(mrow, text=L("未设置额度"), fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))
            mx = tk.Canvas(mbody, height=Design.BAR_HEIGHT, bg=Design.CARD_FILL,
                           highlightthickness=0)
            if budget > 0:
                mx.pack(fill=tk.X, pady=(8, 4))
            mratio = min(cost_mtd / budget, 1.0) if budget > 0 else 0.0

            def draw_mx(event, cv=mx, fr=mratio):
                cv.delete("all")
                w = max(event.width, 1)
                r = Design.BAR_HEIGHT / 2
                ChartCanvas.round_rect(cv, 0, 0, w, Design.BAR_HEIGHT, r, fill=Design.BTN_BG)
                if fr > 0:
                    ChartCanvas.round_rect(cv, 0, 0, max(w * fr, 2), Design.BAR_HEIGHT, r,
                                           fill=usage_color(fr))
            mx.bind("<Configure>", draw_mx)
            proj_text = money(cost_mtd / mtd_elapsed * month_days) if mtd_elapsed else money(cost_mtd)
            prow = tk.Frame(mbody, bg=Design.CARD_FILL)
            prow.pack(fill=tk.X)
            tk.Label(prow, text=L("预计全月 ") + proj_text, fg=Design.TEXT_MUTED,
                     bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
            tk.Label(prow, text=(L("额度 ") + money(budget)) if budget > 0 else "",
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            tk.Frame(mbody, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=(10, 6))
            peak_cost = max(cost_daily, key=lambda kv: kv[1]) if cost_daily else ("", 0.0)
            stat_row(mbody, [(L("单次会话"), money(session_cost30)),
                             (L("最贵一天"), money(peak_cost[1]))])

            # ---- 走势 ----
            days = cost_range["days"]
            # 按自然日补零，横轴才是连续的；不足 90 天也能正确算日均
            daily_map = dict(cost_daily90)
            daily = []
            for i in range(days - 1, -1, -1):
                day = datetime.now() - timedelta(days=i)
                daily.append((day.strftime("%m-%d"),
                              daily_map.get(day.strftime("%Y-%m-%d"), 0.0)))
            series = [(label, {L("费用"): c}) for label, c in daily]
            sec = tk.Frame(cost_area, bg=Design.BACKGROUND)
            sec.pack(fill=tk.X, padx=6, pady=(12, 0))
            tk.Label(sec, text=L("近 %d 天费用走势") % days, fg=Design.TEXT_PRIMARY,
                     bg=Design.BACKGROUND, font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            avg_daily = sum(c for _, c in daily) / max(days, 1)
            note = L("日均 %s") % money(avg_daily)
            if mtd > 0:
                note += L(" · 本月共 %s") % money(cost_mtd)
            tk.Label(sec, text=note, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            png_label(cost_area, self._bar_chart_png(
                series, {L("费用"): Design.SUCCESS},
                budget_line=daily_budget if budget > 0 else None, value_fmt=money))

            # ---- 底部：模型费用 + 性价比榜 ----
            bottom = tk.Frame(cost_area, bg=Design.BACKGROUND)
            bottom.pack(fill=tk.X, padx=6, pady=(12, 0))
            bottom.grid_columnconfigure(0, weight=1, uniform="costbot")
            bottom.grid_columnconfigure(1, weight=1, uniform="costbot")

            lcard, lbody = rounded_card(bottom, padx=14, pady=12)
            lcard.grid(row=0, column=0, sticky="nsew", padx=6, pady=4)
            lhead = tk.Frame(lbody, bg=Design.CARD_FILL)
            lhead.pack(fill=tk.X)
            tk.Label(lhead, text=L("模型费用"), fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                     font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            tk.Label(lhead, text=L("近 30 天 · 合计 %s") % money(cost30),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            shown_models = cost_models[:4]
            rest_cost = sum(c for _m, c, _t in cost_models[4:])
            rest_count = len(cost_models[4:])
            for m, c, _tok in shown_models:
                r = tk.Frame(lbody, bg=Design.CARD_FILL)
                r.pack(fill=tk.X, pady=(6, 0))
                tk.Label(r, text=m, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                         font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
                share = (c / cost30 * 100) if cost30 > 0 else 0.0
                tk.Label(r, text="%.0f%%" % share, fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                tk.Label(r, text=money(c), fg=Design.DATA, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT, padx=(0, 8))
                share_bar(lbody, (c / cost30) if cost30 > 0 else 0.0, Design.SUCCESS)
            if rest_count:
                r = tk.Frame(lbody, bg=Design.CARD_FILL)
                r.pack(fill=tk.X, pady=(8, 0))
                tk.Label(r, text=L("其他 %d 个模型") % rest_count, fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
                tk.Label(r, text=money(rest_cost), fg=Design.TEXT_SECONDARY,
                         bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
            tk.Frame(lbody, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=(10, 0))
            foot = L("费用按 cc-switch 记录的单价折算；未计费渠道可在设置里配默认单价估算")
            if price > 0:
                foot += L("（当前按 $%g/M tokens 估算）") % price
            tk.Label(lbody, text=foot, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_TINY, anchor='w', justify=tk.LEFT,
                     wraplength=300).pack(fill=tk.X, pady=(6, 0))

            vcard, vbody = rounded_card(bottom, padx=14, pady=12)
            vcard.grid(row=0, column=1, sticky="nsew", padx=6, pady=4)
            vhead = tk.Frame(vbody, bg=Design.CARD_FILL)
            vhead.pack(fill=tk.X)
            tk.Label(vhead, text=L("性价比榜"), fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                     font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            tk.Label(vhead, text=L("每 $1 换多少 token"), fg=Design.TEXT_MUTED,
                     bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            ranked = sorted(((m, tok / c) for m, c, tok in cost_models if c > 0.005),
                            key=lambda kv: kv[1], reverse=True)[:4]
            best = ranked[0][1] if ranked else 1
            for i, (m, per_dollar) in enumerate(ranked):
                r = tk.Frame(vbody, bg=Design.CARD_FILL)
                r.pack(fill=tk.X, pady=(6, 0))
                badge = tk.Label(r, text=str(i + 1), width=2, bg=Design.BTN_BG,
                                 fg=Design.SUCCESS if i == 0 else Design.TEXT_SECONDARY,
                                 font=Design.FONT_MONO_SMALL)
                badge.pack(side=tk.LEFT, padx=(0, 8))
                tk.Label(r, text=m, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                         font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
                tk.Label(r, text=self.fmt_tokens(int(per_dollar)) + " / $1",
                         fg=Design.DATA, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                share_bar(vbody, per_dollar / best, Design.SUCCESS)
            if not ranked:
                tk.Label(vbody, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(anchor='w')
            elif top and top[0] in [m for m, _p in ranked]:
                rank = [m for m, _p in ranked].index(top[0]) + 1
                tk.Label(vbody,
                         text=L("主力模型性价比排第 %d，第 1 名每 $1 可换 %s token")
                              % (rank, self.fmt_tokens(int(best))),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_TINY, anchor='w', justify=tk.LEFT,
                         wraplength=300).pack(fill=tk.X, pady=(8, 0))

        render_cost()
        cost_wheel(cost_page)

        # ============ 洞察（使用通查） ============
        f_ins = page_frame[L("洞察")]
        ins_page, ins_wheel = scroll_page(f_ins)

        INS_GREEN = Design.SUCCESS
        raw_by_date = {d: t for d, t in (ins_daily or [])}
        day_zero = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        def zero_filled(days):
            """近 days 天（含今天）逐日 token，缺失日补 0，日期升序"""
            return [((day_zero - timedelta(days=i)).strftime("%Y-%m-%d"),
                     raw_by_date.get((day_zero - timedelta(days=i)).strftime("%Y-%m-%d"), 0))
                    for i in range(days - 1, -1, -1)]

        # 「全部」范围起点：首条明细那天，最多回溯 730 天（与 mac dayTokensAll 一致）
        span_days = 1
        if ins_first:
            try:
                first_day = datetime.fromtimestamp(int(ins_first)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                span_days = (day_zero - first_day).days + 1
            except (ValueError, OSError, OverflowError):
                span_days = 1
        span_days = min(max(span_days, 1), 730)

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

        # ---- 页头：标题 + 数据截至 + 右上「7 天 / 30 天 / 90 天 / 全部」切换 ----
        head = tk.Frame(ins_page, bg=Design.BACKGROUND)
        head.pack(fill=tk.X, padx=18, pady=(10, 2))
        hleft = tk.Frame(head, bg=Design.BACKGROUND)
        hleft.pack(side=tk.LEFT)
        tk.Label(hleft, text=L("使用通查"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(hleft, text=L("数据截至 %s") % datetime.now().strftime("%m-%d %H:%M"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(anchor='w')

        hright = tk.Frame(head, bg=Design.BACKGROUND)
        hright.pack(side=tk.RIGHT)
        btn(hright, L("导出长图"), export_long_image).pack(side=tk.RIGHT, padx=(8, 0))
        pill_card, pill_body = rounded_card(hright, padx=4, pady=4, radius=14)
        pill_card.pack(side=tk.RIGHT)
        range_state = {"key": "30"}
        seg_btns = {}
        seg_specs = [("7", L("7 天")), ("30", L("30 天")),
                     ("90", L("90 天")), ("all", L("全部"))]
        for key, label in seg_specs:
            b = tk.Button(pill_body, text=label, bd=0, relief=tk.FLAT, cursor="hand2",
                          bg=Design.CARD_FILL, fg=Design.TEXT_SECONDARY,
                          activebackground=Design.BTN_BG_HOVER,
                          activeforeground=Design.TEXT_PRIMARY,
                          font=Design.FONT_UI_SMALL, padx=10, pady=2,
                          command=lambda k=key: (range_state.update(key=k), render_range()))
            b.pack(side=tk.LEFT)
            seg_btns[key] = b

        # 注意：不要叫 body——下面积分/WorkBuddy 卡片也用了 `card, body =` 的写法，
        # 同一个函数作用域里会把名字覆盖掉，范围切换就会重建到别的页签上去。
        ins_body = tk.Frame(ins_page, bg=Design.BACKGROUND)
        ins_body.pack(fill=tk.X, padx=10, pady=(2, 10))
        chron_state = {"expanded": False}
        chron_cache = {}

        def flat_canvas(parent, height=6, fill=INS_GREEN, ratio=0.0):
            """胶囊进度条（本月已用比例）"""
            c = tk.Canvas(parent, height=height, bg=Design.CARD_FILL, highlightthickness=0)
            c.pack(fill=tk.X, pady=(6, 4))
            r = height / 2

            def draw(event, cv=c, fr=ratio):
                cv.delete("all")
                w = max(event.width, 1)
                ChartCanvas.round_rect(cv, 0, 0, w, height, r, fill=Design.BTN_BG)
                if fr > 0:
                    ChartCanvas.round_rect(cv, 0, 0, max(w * min(fr, 1.0), 2), height, r,
                                           fill=fill)

            c.bind("<Configure>", draw)
            return c

        def metric_card(parent, col, title, value, sub, value_font=None, icon=""):
            card, cb = rounded_card(parent, padx=14, pady=12)
            card.grid(row=0, column=col, sticky="nsew", padx=6, pady=4)
            trow = tk.Frame(cb, bg=Design.CARD_FILL)
            trow.pack(fill=tk.X)
            if icon:
                tk.Label(trow, text=icon, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=("Segoe UI Emoji", 10)).pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(trow, text=title, fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
            tk.Label(cb, text=value, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                     font=value_font or Design.FONT_INSIGHT_VALUE,
                     anchor='w').pack(fill=tk.X, pady=(2, 0))
            tk.Label(cb, text=sub, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)

        def render_range():
            for w in ins_body.winfo_children():
                w.destroy()
            key = range_state["key"]
            for k, b in seg_btns.items():
                sel = (k == key)
                b.configure(bg=blend(INS_GREEN, Design.CARD_FILL, 0.22) if sel
                            else Design.CARD_FILL,
                            fg=INS_GREEN if sel else Design.TEXT_SECONDARY)
            if key == "all":
                days = span_days
                title = L("全部用量")
            else:
                days = int(key)
                title = L("近 %d 天用量") % days

            series = zero_filled(days)
            total = sum(v for _, v in series)
            daily_avg = total // max(days, 1)
            peak_idx = max(range(len(series)), key=lambda i: series[i][1]) if series else 0
            peak_day, peak_v = series[peak_idx] if series else ("-", 0)
            prev_total = None
            if key != "all":
                prev_total = sum(
                    raw_by_date.get((day_zero - timedelta(days=i)).strftime("%Y-%m-%d"), 0)
                    for i in range(days, days * 2))

            # ---- Hero：范围总量 + 环比 + 面积走势 + 日均/峰值/历史总量 ----
            # 原型图里「预计本月消耗」内联在 Hero 卡右上角，不再是独立卡片
            hero_row = tk.Frame(ins_body, bg=Design.BACKGROUND)
            hero_row.pack(fill=tk.X)
            hero_row.grid_columnconfigure(0, weight=1)
            hero, hero_body = rounded_card(hero_row, padx=16, pady=14)
            hero.grid(row=0, column=0, sticky="nsew", padx=(6, 6), pady=4)

            hrow = tk.Frame(hero_body, bg=Design.CARD_FILL)
            hrow.pack(fill=tk.X)
            tk.Label(hrow, text=title, fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                     font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
            if prev_total:
                pct = (total - prev_total) / prev_total * 100
                if abs(pct) < 0.5:
                    badge_txt, badge_color = "±0%", Design.TEXT_MUTED
                elif pct > 0:
                    badge_txt, badge_color = "▲ %.0f%%" % pct, Design.WARNING
                else:
                    badge_txt, badge_color = "▼ %.0f%%" % -pct, Design.SUCCESS
                tk.Label(hrow, text=badge_txt, fg=badge_color, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))

            ratio = (mtd / projected) if projected else 0.0
            pred_block = tk.Frame(hrow, bg=Design.CARD_FILL)
            pred_block.pack(side=tk.RIGHT)
            tk.Label(pred_block, text=L("预计本月消耗"), fg=Design.TEXT_MUTED,
                     bg=Design.CARD_FILL, font=Design.FONT_UI_TINY,
                     anchor='e').pack(anchor='e')
            tk.Label(pred_block, text=self.fmt_tokens(projected), fg=INS_GREEN,
                     bg=Design.CARD_FILL, font=Design.FONT_VALUE,
                     anchor='e').pack(anchor='e')
            tk.Label(pred_block, text=L("已用 %s · %.0f%%") % (self.fmt_tokens(mtd),
                                                            ratio * 100),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_TINY, anchor='e').pack(anchor='e')

            vrow = tk.Frame(hero_body, bg=Design.CARD_FILL)
            vrow.pack(fill=tk.X, pady=(2, 0))
            tk.Label(vrow, text=self.fmt_tokens(total), fg=INS_GREEN, bg=Design.CARD_FILL,
                     font=("Microsoft YaHei UI", 26, "bold")).pack(side=tk.LEFT)
            tk.Label(vrow, text=L("单位 tokens"), fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(6, 0), pady=(10, 0))

            chart_pts = [(d[5:] if days <= 90 else d[2:], v) for d, v in series]
            hero_img = self._area_chart_png(chart_pts, INS_GREEN, width=560, height=128)
            photo = ImageTk.PhotoImage(hero_img)
            lbl = tk.Label(hero_body, image=photo, bg=Design.CARD_FILL, bd=0)
            lbl.image = photo
            lbl.pack(fill=tk.X, pady=(6, 2))

            stats_row = tk.Frame(hero_body, bg=Design.CARD_FILL)
            stats_row.pack(fill=tk.X, pady=(4, 0))
            for i in range(3):
                stats_row.grid_columnconfigure(i, weight=1, uniform="hero")
            for i, (lab, val, sub) in enumerate([
                    (L("日均"), self.fmt_tokens(daily_avg), L("近 %d 天") % days),
                    (L("单日峰值"), self.fmt_tokens(peak_v), peak_day),
                    (L("历史总量"), self.fmt_tokens(total_all), L("全部时间"))]):
                col = tk.Frame(stats_row, bg=Design.CARD_FILL)
                col.grid(row=0, column=i, sticky="w", padx=(0, 10))
                tk.Label(col, text=lab, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_TINY, anchor='w').pack(anchor='w')
                tk.Label(col, text=val, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL, anchor='w').pack(anchor='w')
                tk.Label(col, text=sub, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_TINY, anchor='w').pack(anchor='w')

            # ---- 四张指标卡：连续使用 / 今日会话 / 最活跃时段 / 主力模型 ----
            cards_row = tk.Frame(ins_body, bg=Design.BACKGROUND)
            cards_row.pack(fill=tk.X, pady=(4, 0))
            for i in range(4):
                cards_row.grid_columnconfigure(i, weight=1, uniform="ins")

            hour_txt = "-"
            if peak_hour_map:
                h = max(peak_hour_map.items(), key=lambda kv: kv[1])[0]
                hour_txt = "%02d:00 – %02d:00" % (h, (h + 1) % 24)
            share_base = total_all or 1
            top_share = (top[1] / share_base * 100) if top else 0.0
            top_name = top[0] if top else "-"
            if len(top_name) > 12:
                top_name = top_name[:12] + "…"
            metric_card(cards_row, 0, L("连续使用"), L("%d 天") % streak, L("当前连续"),
                        icon="🔥")
            metric_card(cards_row, 1, L("今日会话"), L("%d 个") % session_count,
                        L("平均 %d 分钟 · 最长 %d 分钟") % (session_avg, session_longest),
                        icon="▤")
            metric_card(cards_row, 2, L("最活跃时段"), hour_txt, L("近 30 天"), icon="◷")
            metric_card(cards_row, 3, L("主力模型"), top_name,
                        (self.fmt_tokens(top[1]) + " · " + L("占 %.0f%%") % top_share)
                        if top else L("暂无数据"),
                        value_font=("Microsoft YaHei UI", 13, "bold"), icon="◆")

            # ---- 星期分布：近 91 天按星期几求日均 ----
            wd_sum = [0] * 7
            wd_cnt = [0] * 7
            for i in range(91):
                d = day_zero - timedelta(days=i)
                key_d = d.strftime("%Y-%m-%d")
                wd_sum[d.weekday()] += raw_by_date.get(key_d, 0)
                wd_cnt[d.weekday()] += 1
            wd_avg = [wd_sum[i] / max(wd_cnt[i], 1) for i in range(7)]
            wd_labels = [L("周一"), L("周二"), L("周三"), L("周四"), L("周五"),
                         L("周六"), L("周日")]
            wd_overall = int(sum(wd_avg) / 7) if any(wd_cnt) else 0
            workday_avg = int(sum(wd_avg[:5]) / 5)
            weekend_avg = int(sum(wd_avg[5:]) / 2)

            # ---- 星期分布 + 近 90 天热力图：并排两栏（与 mac 原型一致） ----
            duo = tk.Frame(ins_body, bg=Design.BACKGROUND)
            duo.pack(fill=tk.X, padx=6, pady=(10, 0))
            duo.grid_columnconfigure(0, weight=1, uniform="duo")
            duo.grid_columnconfigure(1, weight=1, uniform="duo")
            duo.grid_rowconfigure(0, weight=1)

            # 左：星期分布（七根柱，柱顶数值 + 全星期标签）
            wsec = tk.Frame(duo, bg=Design.BACKGROUND)
            wsec.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            whead = tk.Frame(wsec, bg=Design.BACKGROUND)
            whead.pack(fill=tk.X)
            tk.Label(whead, text=L("星期分布"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_CARD_TITLE, anchor='w').pack(side=tk.LEFT)
            tk.Label(whead, text=(L("近 90 天均值") + " " + self.fmt_tokens(wd_overall) + L("/天")),
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)

            wcard, wbody = rounded_card(wsec, padx=14, pady=10)
            wcard.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
            if not any(wd_sum):
                tk.Label(wbody, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(anchor='w')
            else:
                wchart = tk.Canvas(wbody, height=132, bg=Design.CARD_FILL,
                                   highlightthickness=0)
                wchart.pack(fill=tk.X)
                wd_max = max(wd_avg) or 1

                def draw_weekday(_event=None, cv=wchart):
                    cv.delete("all")
                    width = max(cv.winfo_width(), 1)
                    slot = width / 7.0
                    bar_w = max(min(slot * 0.52, 24), 6)
                    top_y, base_y = 24, 110
                    for i in range(7):
                        cx = slot * (i + 0.5)
                        value = wd_avg[i]
                        bar_h = max((value / wd_max) * (base_y - top_y), 2)
                        color = INS_GREEN if i < 5 else blend(INS_GREEN, Design.CARD_FILL, 0.45)
                        cv.create_text(cx, 10, text=self.fmt_tokens(int(value)),
                                       fill=Design.TEXT_MUTED, font=Design.FONT_UI_TINY)
                        ChartCanvas.round_rect(cv, cx - bar_w / 2, base_y - bar_h,
                                               cx + bar_w / 2, base_y, 3, fill=color)
                        cv.create_text(cx, base_y + 11, text=wd_labels[i],
                                       fill=Design.TEXT_MUTED, font=Design.FONT_UI_TINY)

                wchart.bind("<Configure>", draw_weekday)
                wfoot = tk.Frame(wbody, bg=Design.CARD_FILL)
                wfoot.pack(fill=tk.X, pady=(4, 0))
                tk.Label(wfoot, text=L("工作日日均") + " " + self.fmt_tokens(workday_avg),
                         fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_TINY).pack(side=tk.LEFT)
                tk.Label(wfoot, text=L("周末日均") + " " + self.fmt_tokens(weekend_avg),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_TINY).pack(side=tk.RIGHT)

            # 右：近 90 天热力图（绿色色阶）
            hsec = tk.Frame(duo, bg=Design.BACKGROUND)
            hsec.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
            hhead = tk.Frame(hsec, bg=Design.BACKGROUND)
            hhead.pack(fill=tk.X)
            tk.Label(hhead, text=L("近 90 天用量热力图"), fg=Design.TEXT_PRIMARY,
                     bg=Design.BACKGROUND, font=Design.FONT_CARD_TITLE,
                     anchor='w').pack(side=tk.LEFT)
            lg = tk.Canvas(hhead, width=92, height=14, bg=Design.BACKGROUND, highlightthickness=0)
            lg.pack(side=tk.RIGHT)
            lg.create_text(0, 7, text=L("少"), fill=Design.TEXT_MUTED,
                           font=Design.FONT_UI_TINY, anchor='w')
            for i in range(4):
                lg.create_rectangle(18 + i * 13, 3, 28 + i * 13, 13, outline="",
                                    fill=blend(INS_GREEN, Design.CARD_FILL, 0.18 + i * 0.27))
            lg.create_text(72, 7, text=L("多"), fill=Design.TEXT_MUTED,
                           font=Design.FONT_UI_TINY, anchor='w')

            heat_card, heat_body = rounded_card(hsec, padx=14, pady=10)
            heat_card.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
            start_day = day_zero - timedelta(days=90)
            pad = start_day.weekday()
            cells = [("", -1)] * pad
            for i in range(91):
                key_d = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
                cells.append((key_d, raw_by_date.get(key_d, 0)))
            max_token = max((t for _, t in cells), default=0) or 1
            weeks = (len(cells) + 6) // 7
            cell_size, gap = 14, 3
            left_axis = 18
            canvas_w = max(left_axis + weeks * (cell_size + gap) + 4, 120)
            rows_h = 7 * (cell_size + gap) + 20
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
                    tip.place(x=min(cx + 12, max(heat.winfo_width() - 160, 12)),
                              y=max(cy - 26, 2))
                return enter

            for row, name in ((0, L("一")), (2, L("三")), (4, L("五"))):
                heat.create_text(4, 3 + row * (cell_size + gap) + cell_size / 2,
                                 text=name, fill=Design.TEXT_MUTED,
                                 font=Design.FONT_UI_TINY, anchor='w')
            month_seen = set()
            for idx, (date_text, token) in enumerate(cells):
                row, col = idx % 7, idx // 7
                x1 = left_axis + col * (cell_size + gap)
                y1 = 3 + row * (cell_size + gap)
                if token < 0:
                    color = blend("#FFFFFF", Design.CARD_FILL, 0.03)
                elif token == 0:
                    color = blend("#FFFFFF", Design.CARD_FILL, 0.07)
                else:
                    color = blend(INS_GREEN, Design.CARD_FILL,
                                  0.12 + 0.83 * min(token / max_token, 1.0))
                tag = "cell%d" % idx
                ChartCanvas.round_rect(heat, x1, y1, x1 + cell_size, y1 + cell_size, 3,
                                       fill=color, outline="", tags=tag)
                if date_text:
                    heat.tag_bind(tag, "<Enter>", make_enter(date_text, token, x1, y1))
                    heat.tag_bind(tag, "<Leave>", on_leave)
                    if date_text[8:10] == "01":
                        mk = date_text[:7]
                        if mk not in month_seen:
                            month_seen.add(mk)
                            heat.create_text(x1 + 2, 3 + 7 * (cell_size + gap) + 2,
                                             text=L("%d 月") % int(date_text[5:7]),
                                             fill=Design.TEXT_MUTED,
                                             font=Design.FONT_UI_TINY, anchor='nw')

            # ---- 模型编年史（横向时间条） ----

            def chronicle_rows(force=False):
                if force or "rows" not in chron_cache:
                    chron_cache["rows"] = store.query_model_history() or []
                return chron_cache["rows"]

            def auto_merge_models():
                from tkinter import messagebox
                groups, changed = store.auto_merge_models()
                render_chronicle(force=True)
                if groups == 0:
                    messagebox.showinfo(L("没有需要合并的模型"),
                                        L("大小写、厂商前缀不同的同名模型都已一致"))
                else:
                    messagebox.showinfo(L("合并完成"),
                                        L("已合并 %d 组 · 改写 %d 行明细") % (groups, changed))

            def manual_merge_models():
                from tkinter import messagebox
                names = [m for m, _f, _l, _t in chronicle_rows()]
                if len(names) < 2:
                    messagebox.showinfo(L("手动合并模型"), L("模型不足两个，无需合并"))
                    return
                from tkinter import simpledialog
                src = simpledialog.askstring(L("手动合并模型"),
                                             L("要并入其他模型的名称：\n") + "\n".join(names[:20]),
                                             parent=root)
                if not src:
                    return
                dst = simpledialog.askstring(L("手动合并模型"), L("合并到（目标模型名）："),
                                             parent=root)
                if not dst or dst == src:
                    return
                changed = store.merge_model(src, dst)
                render_chronicle(force=True)
                messagebox.showinfo(L("合并完成"), L("已改写 %d 行明细") % changed)

            csec = tk.Frame(ins_body, bg=Design.BACKGROUND)
            csec.pack(fill=tk.X, padx=6, pady=(10, 0))
            tk.Label(csec, text=L("模型编年史"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_CARD_TITLE, anchor='w').pack(side=tk.LEFT)
            cmeta_lbl = tk.Label(csec, text="", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                                 font=Design.FONT_UI_SMALL)
            cmeta_lbl.pack(side=tk.LEFT, padx=(8, 0))

            chron_card, chron_body = rounded_card(ins_body, padx=14, pady=10)
            chron_card.pack(fill=tk.X, padx=16, pady=(4, 0))
            chron_wrap = tk.Frame(chron_body, bg=Design.CARD_FILL)
            chron_wrap.pack(fill=tk.X)

            def render_chronicle(force=False):
                for w in chron_wrap.winfo_children():
                    w.destroy()
                history = chronicle_rows(force)
                cmeta_lbl.configure(
                    text=(L("共 %d 个 · 按首次使用排序") % len(history)) if history else "")
                if not history:
                    tk.Label(chron_wrap, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                             bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(anchor='w')
                    return
                shown = history if chron_state["expanded"] else history[:12]
                max_epoch = max(r[2] for r in shown) or 1
                min_epoch = min((r[1] for r in shown if r[1] > 0), default=max_epoch - 86400 * 30)
                span = max(max_epoch - min_epoch, 1)
                colors = model_colors()

                # 顶部月份刻度（用空标签占位，保证与下方时间条对齐）
                axis_row = tk.Frame(chron_wrap, bg=Design.CARD_FILL)
                axis_row.pack(fill=tk.X)
                tk.Label(axis_row, text="", width=18, bg=Design.CARD_FILL,
                         font=Design.FONT_UI).pack(side=tk.LEFT)
                axis = tk.Canvas(axis_row, height=16, bg=Design.CARD_FILL,
                                 highlightthickness=0)
                axis.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
                tk.Label(axis_row, text="", width=10, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

                def draw_axis(event, cv=axis):
                    cv.delete("all")
                    w = max(event.width, 1)
                    start = datetime.fromtimestamp(min_epoch)
                    cur = datetime(start.year, start.month, 1)
                    while True:
                        marker = cur + timedelta(days=31)
                        marker = datetime(marker.year, marker.month, 1)
                        if marker.timestamp() > max_epoch:
                            break
                        x = (marker.timestamp() - min_epoch) / span * w
                        cv.create_text(x, 8, text=L("%d 月") % marker.month,
                                       fill=Design.TEXT_MUTED, font=Design.FONT_UI_TINY,
                                       anchor='w')
                        cur = marker

                axis.bind("<Configure>", draw_axis)

                for idx, (model, first_epoch, last_epoch, token) in enumerate(shown):
                    row = tk.Frame(chron_wrap, bg=Design.CARD_FILL)
                    row.pack(fill=tk.X, pady=2)
                    tk.Label(row, text=model or "-", fg=Design.TEXT_PRIMARY,
                             bg=Design.CARD_FILL, font=Design.FONT_UI, anchor='w',
                             width=18).pack(side=tk.LEFT)
                    bar = tk.Canvas(row, height=16, bg=Design.CARD_FILL,
                                    highlightthickness=0)
                    bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
                    tk.Label(row, text=self.fmt_tokens(token), fg=Design.DATA,
                             bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL,
                             anchor='e', width=10).pack(side=tk.RIGHT)
                    color = colors[idx % len(colors)]

                    def draw_bar(event, cv=bar, f0=first_epoch, f1=last_epoch, c=color):
                        cv.delete("all")
                        w = max(event.width, 1)
                        x0 = (f0 - min_epoch) / span * w
                        x1 = (f1 - min_epoch) / span * w
                        cv.create_line(0, 8, w, 8, fill=Design.CARD_BORDER)
                        ChartCanvas.round_rect(cv, x0, 3, max(x1, x0 + 6), 13, 5,
                                               fill=c, outline="")

                    bar.bind("<Configure>", draw_bar)

                def toggle():
                    chron_state["expanded"] = not chron_state["expanded"]
                    render_chronicle()

                if len(history) > 12:
                    tk.Button(chron_wrap,
                              text=(L("收起") if chron_state["expanded"]
                                    else L("展开其余 %d 个模型") % (len(history) - 12)),
                              command=toggle, bg=Design.CARD_FILL, fg=Design.BRAND,
                              activebackground=Design.CARD_FILL, activeforeground=Design.BRAND,
                              relief="flat", bd=0, cursor="hand2",
                              font=Design.FONT_UI_SMALL).pack(anchor='w', pady=(4, 0))
                menu_row = tk.Frame(chron_wrap, bg=Design.CARD_FILL)
                menu_row.pack(fill=tk.X, pady=(6, 0))
                mb = tk.Menubutton(menu_row, text=L("整理模型 ▾"), bg=Design.BTN_BG,
                                   fg=Design.TEXT_PRIMARY, activebackground=Design.BTN_BG_HOVER,
                                   activeforeground=Design.TEXT_PRIMARY, relief="flat", bd=0,
                                   cursor="hand2", font=Design.FONT_UI_SMALL, padx=10, pady=3)
                menu = tk.Menu(mb, tearoff=0, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                               activebackground=Design.BRAND,
                               activeforeground=Design.TEXT_PRIMARY,
                               font=Design.FONT_UI_SMALL)
                menu.add_command(label=L("自动合并同名模型"), command=auto_merge_models)
                menu.add_command(label=L("手动合并…"), command=manual_merge_models)
                mb.configure(menu=menu)
                mb.pack(side=tk.RIGHT)
                ins_wheel(chron_wrap)

            render_chronicle()
            ins_wheel(ins_body)

        render_range()

        # ============ 分享 ============
        f_share = page_frame[L("分享")]
        share_page, share_wheel = scroll_page(f_share)

        share_head = tk.Frame(share_page, bg=Design.BACKGROUND)
        share_head.pack(fill=tk.X, padx=18, pady=(10, 2))
        share_log = tk.Frame(share_head, bg=Design.BACKGROUND)
        share_log.pack(side=tk.LEFT)
        tk.Label(share_log, text=L("分享"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(share_log, text=L("把战报卡与周报导出成图片"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(anchor='w')

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
        f_ch = page_frame[L("渠道")]
        ch_page, ch_wheel = scroll_page(f_ch)
        ch_body = tk.Frame(ch_page, bg=Design.BACKGROUND)
        ch_body.pack(fill=tk.X)

        ch_head = tk.Frame(ch_body, bg=Design.BACKGROUND)
        ch_head.pack(fill=tk.X, padx=18, pady=(10, 2))
        chlog = tk.Frame(ch_head, bg=Design.BACKGROUND)
        chlog.pack(side=tk.LEFT)
        tk.Label(chlog, text=L("渠道"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(chlog, text=L("渠道 = 接入方式 · 应用 = 实际使用的工具"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(anchor='w')

        ch_state = {"days": 30, "mode": "channel"}

        def on_ch_range(days):
            ch_state["days"] = days
            render_channels()

        ch_pills, _ch_set = range_pills(
            ch_head, [(7, L("7 天")), (30, L("30 天")), (90, L("90 天"))],
            30, on_ch_range)
        ch_pills.pack(side=tk.RIGHT)

        ch_area = tk.Frame(ch_body, bg=Design.BACKGROUND)
        ch_area.pack(fill=tk.X, padx=10, pady=(4, 10))

        CH_COLORS = {"Trae": "#34C759", "ZCode": "#22A85A", "cc-switch": "#167A41"}

        def ch_color(name, idx):
            return CH_COLORS.get(name, Design.MODEL_COLORS[idx % len(Design.MODEL_COLORS)])

        def pct_bar(parent, items, height=12):
            """横向百分比构成条：items=[(名称, 占比 0..1, 颜色)]"""
            c = tk.Canvas(parent, height=height, bg=Design.CARD_FILL, highlightthickness=0)
            c.pack(fill=tk.X, pady=(8, 4))

            def draw(event, cv=c):
                cv.delete("all")
                w = max(event.width, 1)
                x = 0.0
                for i, (_n, ratio, color) in enumerate(items):
                    seg = w * ratio if i < len(items) - 1 else w - x
                    seg = max(seg, 1.5)
                    ChartCanvas.round_rect(cv, x, 0, x + seg, height, height / 2, fill=color)
                    x += seg
                if x < w:
                    ChartCanvas.round_rect(cv, x, 0, w, height, height / 2,
                                           fill=Design.BTN_BG)
            c.bind("<Configure>", draw)
            return c

        def render_channels():
            for w in ch_area.winfo_children():
                w.destroy()
            days = ch_state["days"]
            cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            ch_rows = [r for r in channels90 if r[0] >= cutoff]
            app_rows = [r for r in apps90 if r[0] >= cutoff]
            comp_rows = [r for r in comp90 if r[0] >= cutoff]

            # ---- 今日渠道构成 ----
            tcard, tbody = rounded_card(ch_area, padx=16, pady=12)
            tcard.pack(fill=tk.X, padx=6, pady=4)
            thead = tk.Frame(tbody, bg=Design.CARD_FILL)
            thead.pack(fill=tk.X)
            tk.Label(thead, text=L("今日渠道构成"), fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            req_total = sum(r[1] for r in channel_today)
            tok_total = sum(r[2] for r in channel_today)
            tk.Label(thead, text=L("%d 次请求 · %s tokens") % (req_total, self.fmt_tokens(tok_total)),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            if not channel_today or tok_total <= 0:
                tk.Label(tbody, text=L("暂无数据"), fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(anchor='w', pady=(8, 0))
            else:
                names = [store.source_display_name(r[0]) for r in channel_today]
                colors = {n: ch_color(n, i) for i, n in enumerate(names)}
                pct_bar(tbody, [(n, r[2] / tok_total, colors[n])
                                for n, r in zip(names, channel_today)])
                for n, r in zip(names, channel_today):
                    row = tk.Frame(tbody, bg=Design.CARD_FILL)
                    row.pack(fill=tk.X, pady=2)
                    tk.Label(row, text="● " + n, fg=colors[n], bg=Design.CARD_FILL,
                             font=Design.FONT_UI).pack(side=tk.LEFT)
                    tk.Label(row, text=L("%d 次") % r[1], fg=Design.TEXT_MUTED,
                             bg=Design.CARD_FILL,
                             font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))
                    tk.Label(row, text="%.0f%%" % (r[2] / tok_total * 100), fg=Design.SUCCESS,
                             bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                    tk.Label(row, text=self.fmt_tokens(r[2]), fg=Design.TEXT_SECONDARY,
                             bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(
                        side=tk.RIGHT, padx=(0, 10))

            # ---- 构成变化（按渠道 / 按应用）----
            ccard, cbody = rounded_card(ch_area, padx=16, pady=12)
            ccard.pack(fill=tk.X, padx=6, pady=(10, 4))
            chead = tk.Frame(cbody, bg=Design.CARD_FILL)
            chead.pack(fill=tk.X)
            tk.Label(chead, text=L("近 %d 天构成变化") % days, fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            toggle_body = tk.Frame(chead, bg=Design.BTN_BG)
            toggle_body.pack(side=tk.RIGHT)
            mode_btns = {}

            def set_mode(mode):
                ch_state["mode"] = mode
                for k, b in mode_btns.items():
                    sel = k == mode
                    b.configure(bg=Design.CARD_FILL if sel else Design.BTN_BG,
                                fg=Design.TEXT_PRIMARY if sel else Design.TEXT_MUTED)
                render_channels()

            for mode, label in (("channel", L("按渠道")), ("app", L("按应用"))):
                b = tk.Button(toggle_body, text=label, bd=0, relief=tk.FLAT, cursor="hand2",
                              bg=Design.BTN_BG, fg=Design.TEXT_MUTED,
                              activebackground=Design.BTN_BG_HOVER,
                              activeforeground=Design.TEXT_PRIMARY,
                              font=Design.FONT_UI_SMALL, padx=8, pady=1,
                              command=lambda m=mode: set_mode(m))
                b.pack(side=tk.LEFT)
                mode_btns[mode] = b

            if ch_state["mode"] == "channel":
                src_rows = ch_rows
                key_fn = lambda r: store.source_display_name(r[1])
                raw_names = [key_fn(r) for r in src_rows]
            else:
                src_rows = app_rows
                app_map = {"claude": "Claude Code", "claude-desktop": "Claude Desktop",
                           "codex": "Codex", "opencode": "OpenCode", "zcode": "ZCode",
                           "unknown": L("未知")}
                key_fn = lambda r: app_map.get(r[1], r[1])
                raw_names = [key_fn(r) for r in src_rows]
            used = [n for n in sorted(set(raw_names),
                                       key=lambda n: sum(r[2] for r in src_rows if key_fn(r) == n),
                                       reverse=True)
                    if sum(r[2] for r in src_rows if key_fn(r) == n) > 0]
            col_map = {n: ch_color(n, i) for i, n in enumerate(used)}
            by_day = {}
            for r in src_rows:
                by_day.setdefault(r[0], {}).setdefault(key_fn(r), 0)
                by_day[r[0]][key_fn(r)] += r[2]
            series = []
            for d in sorted(by_day):
                try:
                    label = L("%d月%d日") % (int(d[5:7]), int(d[8:10]))
                except (TypeError, ValueError):
                    label = d
                series.append((label, by_day[d]))
            totals = {n: sum(vals.get(n, 0) for _l, vals in series) for n in used}
            grand = sum(totals.values()) or 1
            legend = tk.Frame(cbody, bg=Design.CARD_FILL)
            legend.pack(fill=tk.X, pady=(6, 2))
            for n in used[:4]:
                tk.Label(legend, text="● " + n, fg=col_map[n], bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
                tk.Label(legend, text="%.0f%%" % (totals[n] / grand * 100),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(4, 12))
            # 切换标注：最大份额单日跃变的位置
            marker = None
            if len(series) >= 2 and used:
                top_name = used[0]
                best_jump, best_i = -1.0, 1
                for i in range(1, len(series)):
                    prev = series[i - 1][1]
                    cur = series[i][1]
                    p = prev.get(top_name, 0) / (sum(prev.values()) or 1)
                    c = cur.get(top_name, 0) / (sum(cur.values()) or 1)
                    if abs(c - p) > best_jump:
                        best_jump, best_i = abs(c - p), i
                if best_jump > 0.2:
                    day_text = sorted(by_day)[best_i]
                    marker = (best_i, L("%d月%d日 切换 %s") % (
                        int(day_text[5:7]), int(day_text[8:10]), top_name))
            png_label(cbody, self._stacked_area_png(series, col_map, marker=marker),
                      padx=0, pady=(4, 2))
            first_share = (series[0][1].get(used[0], 0) / (sum(series[0][1].values()) or 1)
                           if series and used else 0)
            last_share = (series[-1][1].get(used[0], 0) / (sum(series[-1][1].values()) or 1)
                          if series and used else 0)
            tk.Label(cbody,
                     text=L("百分比堆叠：纵轴是构成比例，不是用量。%s 占比从 %.0f%% 变为 %.0f%%。")
                          % (used[0], first_share * 100, last_share * 100) if used
                          else L("暂无数据"),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                     wraplength=620).pack(fill=tk.X, pady=(6, 0))

            # ---- 底部：Token 构成 + 缓存收益 ----
            bottom = tk.Frame(ch_area, bg=Design.BACKGROUND)
            bottom.pack(fill=tk.X, padx=6, pady=(10, 0))
            bottom.grid_columnconfigure(0, weight=1, uniform="chbot")
            bottom.grid_columnconfigure(1, weight=1, uniform="chbot")

            tok = [sum(r[1] for r in comp_rows), sum(r[2] for r in comp_rows),
                   sum(r[3] for r in comp_rows), sum(r[4] for r in comp_rows)]
            # 顺序：缓存读 / 输入 / 输出 / 缓存创建（与原型图一致）
            items = [(L("缓存读"), tok[2], "#34C759"), (L("输入"), tok[0], "#5AC8FA"),
                     (L("输出"), tok[1], "#FFD60A"), (L("缓存创建"), tok[3], "#8E8E93")]
            tok_total2 = sum(tok) or 1
            t2card, t2body = rounded_card(bottom, padx=14, pady=12)
            t2card.grid(row=0, column=0, sticky="nsew", padx=6, pady=4)
            t2head = tk.Frame(t2body, bg=Design.CARD_FILL)
            t2head.pack(fill=tk.X)
            tk.Label(t2head, text=L("Token 构成"), fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            tk.Label(t2head, text=L("近 %d 天 · %s") % (days, self.fmt_tokens(tok_total2)),
                     fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            pct_bar(t2body, [(n, v / tok_total2, c) for n, v, c in items])
            grid = tk.Frame(t2body, bg=Design.CARD_FILL)
            grid.pack(fill=tk.X)
            grid.grid_columnconfigure(0, weight=1, uniform="tok")
            grid.grid_columnconfigure(1, weight=1, uniform="tok")
            for i, (n, v, c) in enumerate(items):
                cell = tk.Frame(grid, bg=Design.CARD_FILL)
                cell.grid(row=i // 2, column=i % 2, sticky="w", pady=2)
                tk.Label(cell, text="● " + n, fg=c, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
                tk.Label(cell, text="%.0f%%" % (v / tok_total2 * 100), fg=Design.TEXT_PRIMARY,
                         bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)

            # 缓存收益：缓存读按输入价 1/10 计，省下 90% 的输入成本
            scard, sbody = rounded_card(bottom, padx=14, pady=12)
            scard.grid(row=0, column=1, sticky="nsew", padx=6, pady=4)
            tk.Label(sbody, text=L("缓存收益"), fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                     font=Design.FONT_CARD_TITLE, anchor='w').pack(fill=tk.X)
            cost_sel = sum(c for d, c in cost_daily90 if d >= cutoff)
            cached = tok[2]
            if price > 0 and cached > 0:
                saved = cached / 1_000_000 * price * 0.9
                srow = tk.Frame(sbody, bg=Design.CARD_FILL)
                srow.pack(fill=tk.X, pady=(4, 0))
                tk.Label(srow, text="$%.0f" % saved, fg=Design.SUCCESS, bg=Design.CARD_FILL,
                         font=("Microsoft YaHei UI", 20, "bold")).pack(side=tk.LEFT)
                tk.Label(srow, text=L("本月估算省下"), fg=Design.TEXT_MUTED,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(
                    side=tk.LEFT, padx=(6, 0))
                hit = cached / tok_total2 * 100
                tk.Label(sbody,
                         text=L("%.0f%% 的 token 命中缓存，按缓存读约为输入价 1/10 计。")
                              % hit,
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                         wraplength=280).pack(fill=tk.X, pady=(6, 0))
                no_cache = cost_sel + saved
                ratio = saved / no_cache if no_cache > 0 else 0
                tk.Frame(sbody, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=(8, 6))
                arow = tk.Frame(sbody, bg=Design.CARD_FILL)
                arow.pack(fill=tk.X)
                tk.Label(arow, text=L("实际 ") + money(cost_sel), fg=Design.TEXT_SECONDARY,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
                tk.Label(arow, text=L("省 %.0f%%") % (ratio * 100), fg=Design.SUCCESS,
                         bg=Design.CARD_FILL, font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                share_bar(sbody, ratio, Design.SUCCESS)
            else:
                tk.Label(sbody, text=L("在设置里填写默认单价后估算缓存收益"),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL, anchor='w', justify=tk.LEFT,
                         wraplength=280).pack(fill=tk.X, pady=(8, 0))

        render_channels()
        ch_wheel(ch_page)

        # ============ 流水 ============
        f_tl = page_frame[L("流水")]

        tl_head = tk.Frame(f_tl, bg=Design.BACKGROUND)
        tl_head.pack(fill=tk.X, padx=18, pady=(10, 2))
        tl_title = tk.Frame(tl_head, bg=Design.BACKGROUND)
        tl_title.pack(side=tk.LEFT)
        tk.Label(tl_title, text=L("流水"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(tl_title, text=L("按天回看每条请求 · 支持导出 CSV"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(anchor='w')

        # 当日汇总三卡（请求数 / Token / 费用或积分）
        tl_stats = tk.Frame(f_tl, bg=Design.BACKGROUND)
        tl_stats.pack(fill=tk.X, padx=10, pady=(6, 2))
        for _i in range(3):
            tl_stats.grid_columnconfigure(_i, weight=1, uniform="tlstat")
        tl_labels = {}
        for _i, (_key, _title) in enumerate([("reqs", L("请求数")),
                                             ("tokens", "Token"),
                                             ("cost", L("费用"))]):
            _card, _body = rounded_card(tl_stats, padx=14, pady=8)
            _card.grid(row=0, column=_i, sticky="nsew", padx=6, pady=4)
            tk.Label(_body, text=_title, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)
            _lbl = tk.Label(_body, text="-", fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                            font=Design.FONT_TREND_VALUE, anchor='w')
            _lbl.pack(fill=tk.X)
            tl_labels[_key] = _lbl

        filter_bar = tk.Frame(f_tl, bg=Design.BACKGROUND)
        filter_bar.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(filter_bar, text=L("筛选"), fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)

        table_wrap = tk.Frame(f_tl, bg=Design.BACKGROUND)
        table_wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))
        table_card, table_body = rounded_card(table_wrap, padx=6, pady=6)
        table_card.pack(fill=tk.BOTH, expand=True)

        cols = (L("时间"), L("渠道"), L("模型"), "Token", L("费用"))
        tree = ttk.Treeview(table_body, columns=cols, show="headings", height=16,
                            style="Insights.Treeview")
        widths = (90, 90, 220, 110, 90)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor='w' if c in (L("渠道"), L("模型")) else 'e')
        vsb = ttk.Scrollbar(table_body, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

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
            rows = tl_state["rows"]
            tl_labels["reqs"].configure(text=str(len(rows)))
            tl_labels["tokens"].configure(
                text=self.fmt_tokens(sum(r[3] for r in rows)))
            day_cost = sum(r[4] for r in rows)
            day_credits = sum(r[5] for r in rows)
            if day_cost > 0:
                tl_labels["cost"].configure(text="$%.2f" % day_cost)
            elif day_credits > 0:
                tl_labels["cost"].configure(text=L("%s 积分") % self._fmt_credits(day_credits))
            else:
                tl_labels["cost"].configure(text="-")

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
        f_cred = page_frame[L("积分")]
        cred_page, cred_wheel = scroll_page(f_cred)
        cred_body = tk.Frame(cred_page, bg=Design.BACKGROUND)
        cred_body.pack(fill=tk.X)

        cred_head = tk.Frame(cred_body, bg=Design.BACKGROUND)
        cred_head.pack(fill=tk.X, padx=18, pady=(10, 2))
        crlog = tk.Frame(cred_head, bg=Design.BACKGROUND)
        crlog.pack(side=tk.LEFT)
        tk.Label(crlog, text=L("积分洞察"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=("Microsoft YaHei UI", 16, "bold"), anchor='w').pack(anchor='w')
        tk.Label(crlog, text=L("按 Trae 官方计费口径 · 历史数据最多回溯 90 天"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(anchor='w')

        cred_state = {"days": 30}

        def on_cred_range(days):
            cred_state["days"] = days
            render_credits()

        cred_pills, _cr_set = range_pills(
            cred_head, [(0, L("今日")), (7, L("7 天")), (30, L("30 天")),
                        (90, L("90 天"))], 30, on_cred_range)
        cred_pills.pack(side=tk.RIGHT)

        cred_area = tk.Frame(cred_body, bg=Design.BACKGROUND)
        cred_area.pack(fill=tk.X, padx=10, pady=(4, 10))

        def render_credits():
            for w in cred_area.winfo_children():
                w.destroy()
            cd = dict(credits_daily90)
            today_str = datetime.now().strftime("%Y-%m-%d")

            def sum_range(n, offset=0):
                total = 0.0
                for i in range(offset, offset + n):
                    d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    total += cd.get(d, 0.0)
                return total

            def zero_days(n):
                out = []
                for i in range(n - 1, -1, -1):
                    day = datetime.now() - timedelta(days=i)
                    out.append(("%d/%d" % (day.month, day.day),
                                cd.get(day.strftime("%Y-%m-%d"), 0.0)))
                return out

            def delta_badge(parent, cur, prev):
                if not prev or prev <= 0:
                    return
                pct = (cur - prev) / prev * 100
                tk.Label(parent, text="%s %.1f%%" % ("▲" if pct >= 0 else "▼", abs(pct)),
                         fg=Design.SUCCESS if pct >= 0 else Design.WARNING,
                         bg=Design.CARD_FILL, font=Design.FONT_UI_SMALL).pack(
                    side=tk.RIGHT)

            top_row = tk.Frame(cred_area, bg=Design.BACKGROUND)
            top_row.pack(fill=tk.X, padx=6)
            top_row.grid_columnconfigure(0, weight=3, uniform="credtop")
            top_row.grid_columnconfigure(1, weight=2, uniform="credtop")

            # ---- 左：今日积分 ----
            hcard, hbody = rounded_card(top_row, padx=16, pady=14)
            hcard.grid(row=0, column=0, sticky="nsew", padx=6, pady=4)
            hhead = tk.Frame(hbody, bg=Design.CARD_FILL)
            hhead.pack(fill=tk.X)
            tk.Label(hhead, text=L("今日积分"), fg=Design.TEXT_SECONDARY,
                     bg=Design.CARD_FILL, font=Design.FONT_UI).pack(side=tk.LEFT)
            detail_btn = tk.Button(hhead, text=L("明细 ›"), bd=0, relief=tk.FLAT,
                                   bg=Design.CARD_FILL, fg=Design.TEXT_MUTED,
                                   activebackground=Design.CARD_FILL,
                                   activeforeground=Design.TEXT_PRIMARY,
                                   cursor="hand2", font=Design.FONT_UI_SMALL,
                                   command=lambda: select_page(L("流水")))
            detail_btn.pack(side=tk.RIGHT)
            hrow = tk.Frame(hbody, bg=Design.CARD_FILL)
            hrow.pack(fill=tk.X, pady=(2, 0))
            tk.Label(hrow, text=format_credits(credits_today), fg=Design.BIG_NUMBER,
                     bg=Design.CARD_FILL, font=Design.FONT_BIG).pack(side=tk.LEFT)
            tk.Label(hrow, text=L("积分"), fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(6, 8))
            delta_badge(hrow, credits_today, credits_yday)
            cred_spark = ImageTk.PhotoImage(
                self._sparkline_png([v for _l, v in zero_days(7)], Design.SUCCESS,
                                    width=300, height=76))
            lbl = tk.Label(hbody, bg=Design.CARD_FILL, image=cred_spark)
            lbl.image = cred_spark
            lbl.pack(fill=tk.X, pady=(6, 2))
            tk.Frame(hbody, bg=Design.CARD_BORDER, height=1).pack(fill=tk.X, pady=(2, 6))
            # 今日请求数 / 缓存命中 / 时长
            today_reqs = len(today_timeline)
            today_comp = next((r for r in comp90 if r[0] == today_str), None)
            if today_comp:
                ct = sum(today_comp[1:]) or 1
                cache_hit = today_comp[3] / ct * 100
            else:
                cache_hit = 0.0
            if today_timeline:
                span = (max(r[0] for r in today_timeline)
                        - min(r[0] for r in today_timeline)) / 3600.0
            else:
                span = 0.0
            stat_row(hbody, [(L("请求数"), str(today_reqs)),
                             (L("缓存命中"), "%.0f%%" % cache_hit),
                             (L("时长"), "%.1fh" % span)])

            # ---- 右：积分余额 ----
            # expand=True 让内容区撑满卡片，耗尽提示才能压到卡片底部
            bcard, bbody = rounded_card(top_row, padx=16, pady=14, expand=True)
            bcard.grid(row=0, column=1, sticky="nsew", padx=6, pady=4)
            bhead = tk.Frame(bbody, bg=Design.CARD_FILL)
            bhead.pack(fill=tk.X)
            tk.Label(bhead, text=L("积分余额"), fg=Design.TEXT_SECONDARY,
                     bg=Design.CARD_FILL, font=Design.FONT_UI).pack(side=tk.LEFT)
            if credits_ent:
                consumed, ent_total = credits_ent
                remain = max(ent_total - consumed, 0.0)
                daily_rate = credits30 / 30.0
                days_left = int(remain / daily_rate) if daily_rate > 0 else 0
                if daily_rate > 0:
                    tk.Label(bhead, text=L("可用 %d 天") % days_left, fg=Design.SUCCESS,
                             bg=Design.BTN_BG, font=Design.FONT_UI_SMALL,
                             padx=8).pack(side=tk.RIGHT)
                brow = tk.Frame(bbody, bg=Design.CARD_FILL)
                brow.pack(fill=tk.X, pady=(2, 0))
                tk.Label(brow, text=format_credits(remain), fg=Design.BIG_NUMBER,
                         bg=Design.CARD_FILL, font=("Microsoft YaHei UI", 22, "bold")).pack(
                    side=tk.LEFT)
                tk.Label(brow, text=L("已用 %s · 共 %s") % (format_credits(consumed),
                                                          format_credits(ent_total)),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(8, 0))
                ent_ratio = consumed / ent_total if ent_total > 0 else 0.0
                used_ratio = min(max(ent_ratio, 0.0), 1.0)
                # 进度条两段：已用灰、剩余绿（绿色只表示剩余额度）
                remain_ratio = 1.0 - used_ratio
                eb = tk.Canvas(bbody, height=Design.BAR_HEIGHT, bg=Design.CARD_FILL,
                               highlightthickness=0)
                eb.pack(fill=tk.X, pady=(8, 2))

                def draw_eb(event, cv=eb, used=used_ratio):
                    cv.delete("all")
                    w = max(event.width, 1)
                    r = Design.BAR_HEIGHT / 2
                    used_px = min(w * used, w)
                    ChartCanvas.round_rect(cv, 0, 0, w, Design.BAR_HEIGHT, r,
                                           fill=Design.BTN_BG)
                    if used_px > 1:
                        ChartCanvas.round_rect(
                            cv, 0, 0, used_px, Design.BAR_HEIGHT, r,
                            fill=blend(Design.TEXT_MUTED, Design.CARD_FILL, 0.45))
                    if w - used_px > 1:
                        ChartCanvas.round_rect(cv, used_px, 0, w, Design.BAR_HEIGHT, r,
                                               fill=Design.SUCCESS)
                eb.bind("<Configure>", draw_eb)
                lrow = tk.Frame(bbody, bg=Design.CARD_FILL)
                lrow.pack(fill=tk.X)
                tk.Label(lrow, text=L("已用 %.0f%%") % (used_ratio * 100),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
                tk.Label(lrow, text=L("剩余 %.0f%%") % (remain_ratio * 100),
                         fg=Design.SUCCESS, bg=Design.CARD_FILL,
                         font=Design.FONT_MONO_SMALL).pack(side=tk.RIGHT)
                # 耗尽提示压在卡片底部，避免余额卡下半截留一大片空白
                tk.Label(bbody,
                         text=(L("按今日速率，余额约 %d 天后耗尽") % days_left)
                              if daily_rate > 0 else L("暂无消耗速率"),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL, anchor='w',
                         justify=tk.LEFT).pack(side=tk.BOTTOM, fill=tk.X)
            else:
                tk.Label(bbody, text=L("接入 Trae 并产生用量后展示积分余额"),
                         fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL, anchor='w',
                         justify=tk.LEFT).pack(fill=tk.X, pady=(10, 0))

            # ---- 四张指标卡（带迷你走势）----
            c7 = sum_range(7)
            p7 = sum_range(7, 7)
            c30 = credits30
            p30 = sum_range(30, 30)
            avg30 = c30 / 30.0
            prev_avg = p30 / 30.0
            peak_cred = max(((d, v) for d, v in cd.items()
                             if d >= (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")),
                            key=lambda kv: kv[1], default=("", 0.0))
            cards = tk.Frame(cred_area, bg=Design.BACKGROUND)
            cards.pack(fill=tk.X, padx=6, pady=(10, 0))
            for i in range(4):
                cards.grid_columnconfigure(i, weight=1, uniform="cred")
            specs = [
                (L("近 7 天"), format_credits(c7), c7, p7, zero_days(7)),
                (L("近 30 天"), format_credits(c30), c30, p30, zero_days(30)),
                (L("日均消耗"), format_credits(avg30), avg30, prev_avg, zero_days(14)),
            ]
            for i, (title, value, cur, prev, pts) in enumerate(specs):
                card, body = rounded_card(cards, padx=14, pady=12)
                card.grid(row=0, column=i, sticky="nsew", padx=6, pady=4)
                trow = tk.Frame(body, bg=Design.CARD_FILL)
                trow.pack(fill=tk.X)
                tk.Label(trow, text=title, fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                         font=Design.FONT_UI).pack(side=tk.LEFT)
                delta_badge(trow, cur, prev)
                tk.Label(body, text=value, fg=Design.TEXT_PRIMARY, bg=Design.CARD_FILL,
                         font=Design.FONT_INSIGHT_VALUE, anchor='w').pack(fill=tk.X)
                sp = ImageTk.PhotoImage(
                    self._sparkline_png([v for _l, v in pts], Design.SUCCESS,
                                        width=170, height=34))
                sl = tk.Label(body, bg=Design.CARD_FILL, image=sp)
                sl.image = sp
                sl.pack(fill=tk.X, pady=(4, 0))
            # 峰值单日
            card, body = rounded_card(cards, padx=14, pady=12)
            card.grid(row=0, column=3, sticky="nsew", padx=6, pady=4)
            trow = tk.Frame(body, bg=Design.CARD_FILL)
            trow.pack(fill=tk.X)
            tk.Label(trow, text=L("峰值单日"), fg=Design.TEXT_SECONDARY, bg=Design.CARD_FILL,
                     font=Design.FONT_UI).pack(side=tk.LEFT)
            if peak_cred[0]:
                try:
                    peak_label = L("%d月%d日") % (int(peak_cred[0][5:7]),
                                                int(peak_cred[0][8:10]))
                except (TypeError, ValueError):
                    peak_label = peak_cred[0]
                tk.Label(trow, text=peak_label, fg=Design.TEXT_MUTED, bg=Design.CARD_FILL,
                         font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
            tk.Label(body, text=format_credits(peak_cred[1]), fg=Design.TEXT_PRIMARY,
                     bg=Design.CARD_FILL, font=Design.FONT_INSIGHT_VALUE,
                     anchor='w').pack(fill=tk.X)
            sp = ImageTk.PhotoImage(
                self._sparkline_png([v for _l, v in zero_days(30)], Design.DATA,
                                    width=170, height=34))
            sl = tk.Label(body, bg=Design.CARD_FILL, image=sp)
            sl.image = sp
            sl.pack(fill=tk.X, pady=(4, 0))

            # ---- 积分走势 ----
            days = cred_state["days"]
            sec = tk.Frame(cred_area, bg=Design.BACKGROUND)
            sec.pack(fill=tk.X, padx=6, pady=(12, 0))
            title = L("近 %d 天积分走势") % days if days else L("今日积分")
            tk.Label(sec, text=title, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_CARD_TITLE).pack(side=tk.LEFT)
            if days == 0:
                tk.Label(cred_area, text=L("今日消耗 %s 积分")
                         % format_credits(credits_today),
                         fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                         font=Design.FONT_UI, anchor='w').pack(fill=tk.X, padx=10,
                                                               pady=(8, 0))
            else:
                pts = zero_days(days)
                mean = sum(v for _l, v in pts) / max(len(pts), 1)
                tk.Label(sec, text=L("均值 %s") % format_credits(mean),
                         fg=Design.WARNING, bg=Design.BACKGROUND,
                         font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
                png_label(cred_area, self._trend_area_png(
                    pts, Design.SUCCESS, mean=mean,
                    value_fmt=lambda v: format_credits(v)))

        render_credits()
        tk.Label(cred_page,
                 text=L("积分为 Trae 官方计费口径；历史数据自接入起最多回溯 90 天"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w', justify=tk.LEFT,
                 wraplength=690).pack(fill=tk.X, padx=16, pady=(14, 8))
        cred_wheel(cred_page)

        # ============ WorkBuddy（独立页签，不与其他页面共享口径）============
        f_wb = page_frame[L("WorkBuddy")]
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

        # 导航最底部「设置」只是入口页（已在上方 page_frame 里登记），
        # 选中后切到设置窗口；设置窗口的导航栏保持同一顺序。

        # ============ 页签记忆 ============
        titles = [nb.tab(t, "text") for t in nb.tabs()]
        last_page = initial_page or self.settings.get("insights_last_page") or L("费用")
        # 「设置」只是导航入口，不参与页签记忆
        if last_page not in titles or last_page == L("设置"):
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
            if name == L("设置"):
                # 导航最底部是设置入口：打开设置窗口，洞察中心收起
                self.show_settings()
                root.destroy()
                return
            if self.settings.get("insights_last_page") != name:
                self.settings.set("insights_last_page", name)
                self.settings.save()

        nb.bind("<<NotebookTabChanged>>", on_tab_changed)

        self._bring_to_front(root)
