import sys
import logging
import os

# Windows 控制台默认 cp1252，中文 print 会抛 UnicodeEncodeError（CI 实锤），统一 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import threading
import time
import queue
from datetime import datetime, timedelta
from pathlib import Path

# 日志：--noconsole 打包后 print 的 stdout 不可见，异常落盘（~/.ccbar/ccbar.log）
# 供故障排查。仅记录 warning 及以上（同步/托盘/UI 线程等关键路径）。
_CCBAR_LOG_DIR = os.path.expanduser("~/.ccbar")
try:
    os.makedirs(_CCBAR_LOG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(_CCBAR_LOG_DIR, "ccbar.log"),
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
except Exception:
    pass   # 目录/权限异常不能拖垮主流程，异常仍由 print 兜底
logger = logging.getLogger("ccbar")


def enable_dpi_awareness():
    """声明进程 DPI 感知：高缩放屏幕上字体/窗口不再被系统位图放大而发虚。

    必须在创建 Tk 根窗口之前调用；非 Windows 或旧系统静默跳过。
    用 system-aware（1）而不是 per-monitor（2）：tkinter 不处理跨屏 DPI 变化，
    per-monitor 反而会在拖动窗口时布局错乱。
    """
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Windows 系统托盘
import pystray
from PIL import Image, ImageDraw
import win10toast

from stats_store import StatsStore, SOURCE_REGISTRY, validate_db, adapter_for
from ui_dispatch import _on_gui
from ui_popover import PopoverWindow
from ui_details import DetailWindowsMixin
from ui_insights import InsightsWindowsMixin
from ui_controls import ToggleRow, ToggleSwitch, ThemeSwatches
from update_check import fetch_latest_version, is_newer_version, RELEASES_URL
import app_settings
import autostart
import l10n
import trae_sync
import workbuddy_sync
from l10n import L          # 界面文案统一走 L(中文原文)；当前语言由 l10n 维护
import share_card
import themes
import weekly_report

# 当前版本（发布时随 tag 更新）
APP_VERSION = "1.9.21"   # 与 macOS 版本线对齐

# 数据库路径（cc-switch 默认值，可在设置中修改）
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")

# ============================================================
# 设计系统（与 macOS 版保持一致）
# ============================================================

# ---- 设计系统 / 图表 / 卡片原语：拆至 ui_kit（叶子模块，不依赖 main）----
from ui_kit import (Design, blend, usage_color, today_usage_color,
                    model_colors, gradient_color, ChartCanvas, ChartReadout,
                    work_area, center_geometry, card_underlay, rounded_card,
                    card_title, section_title, muted_line, flat_button,
                    semantic_color, glow_text, format_credits,
                    TODAY_RED_THRESHOLD, _hex_to_rgb, _rgb_to_hex)


class CcBarTray(DetailWindowsMixin, InsightsWindowsMixin):
    # 问候语原文表在 l10n 里（原文即词条 key）；显示时才经 L() 取词条，
    # 免得模块导入期把语言定死成中文
    GREETINGS = l10n.GREETINGS

    def __init__(self):
        self.icon = None
        # 窗口单例表：同一页只保留一份，重复点击提到最前
        self._windows = {}
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
                print(L("UI 回调异常: ") + str(e))
                logger.warning("UI callback error: %s", e)

        if self._ui_root is not None:
            self._ui_root.after(50, self._drain_ui)

    def _ui(self, fn):
        """把回调投递到 GUI 线程执行（可从任意线程调用）"""
        if self._ui_root is None:
            print(L("GUI 线程未就绪，忽略 UI 请求"))
            return
        self._ui_queue.put(fn)

    def _run_async(self, work, done=None, on_error=None):
        """后台线程跑 work()，完成后把 done(结果) 投递回 GUI 线程。

        耗时查询与文件 IO 一律走这里，绝不在 GUI 线程上阻塞（否则整个界面冻结）。
        """
        def worker():
            try:
                value = work()
            except Exception as e:
                logger.warning("background task failed: %s", e)
                if on_error is not None:
                    self._ui(lambda: on_error(e))
                return
            if done is not None:
                self._ui(lambda: done(value))

        threading.Thread(target=worker, daemon=True).start()

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
        self.theme = themes.find(
            self.settings.get("theme") or themes.default_theme()["id"],
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
        # Trae / WorkBuddy 走 HTTP 同步，单独把开关、凭据与后台间隔交给统计库
        self.store.rebuild(configs,
                           trae_enabled=bool(self.settings.get("trae_enabled")),
                           trae_sessionid=self.settings.get("trae_sessionid") or "",
                           trae_bg_interval=self.settings.get("trae_bg_interval"),
                           workbuddy_enabled=bool(self.settings.get("workbuddy_enabled")),
                           workbuddy_cookie=self.settings.get("workbuddy_cookie") or "",
                           workbuddy_bg_interval=self.settings.get("workbuddy_bg_interval"))
        self.store.sync_if_needed()

    def maybe_generate_weekly_report(self):
        """周一自动生成上周用量周报；错过周一自动补生成（幂等：文件存在即跳过）"""
        generate = getattr(weekly_report, "generate_if_needed", None)
        if generate is None:
            return None
        try:
            return generate(
                self.store,
                self._weekly_card_png_bytes,
                output_dir=weekly_report.directory(),
                notify=self._notify_weekly_report,
                auto=bool(self.settings.get("auto_weekly_report", True)),
                english=l10n.is_english())
        except Exception as e:
            print(L("周报生成失败:"), e)
            return None

    def _weekly_card_png_bytes(self, date_text, total, reqs, peak, trend):
        """周报渲染回调：与分享页预览用同一套卡片渲染"""
        return share_card.weekly_card_png(date_text, total, reqs, peak, trend,
                                         theme=self.theme)

    def _notify_weekly_report(self, title, body):
        """周报落盘后的系统通知（weekly_report 只给中文原文，显示时过 L）"""
        self._toast(L(title), L(body))

    def _toast(self, title, body, duration=5):
        """Windows 原生 Toast。

        通知失败（没装通知组件 / 被组策略禁用 / win10toast 在部分系统上抛异常）
        绝不能让统计主流程挂掉，所以统一在这里兜住；threaded 避免阻塞托盘菜单刷新。
        """
        try:
            win10toast.ToastNotifier().show_toast(
                title, body, duration=duration, threaded=True)
        except Exception as e:
            print(L("通知发送失败:"), e)

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
        """闪电 LED 变色（口径与 mac AppDelegate 的 LED 段逐条对齐）

        比的是「本次刷新相对上次刷新的今日增量」，不是今日总量的绝对值：
          · 没有基准（首次）或增量 ≤ 0（含跨天回退）→ 静息灰（mac 模板单色）
          · 0 < 增量 < 红线                        → 绿
          · 增量 ≥ 红线（红线 > 0 才生效）          → 红
        红线 = 「红色门槛」（万 tokens，0=关闭）× 10000。
        """
        today = self.query_day_stats(0)
        if not today:
            return

        total = today["total"]
        last = getattr(self, "_led_last_total", None)
        color = Design.LED_IDLE
        if last is not None:
            delta = total - last
            red_line = max(int(self.settings.get("led_red_threshold") or 0), 0) * 10_000
            if red_line > 0 and delta >= red_line:
                color = Design.LED_RED
            elif delta > 0:
                color = Design.LED_GREEN
        self._led_last_total = total

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
        self._toast(
            L("🫧 里程碑"),
            L("+%s tokens！今日已达 %s（每%d万通知一次）")
            % (self.fmt_tokens(delta), self.fmt_tokens(total), interval_wan))

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
        """查询统计：今日实时；历史区间读 daily_agg 聚合表（不再扫明细）

        days = 「近 N 天（含今天）」的天数：7 → 6 天前到昨天 + 今天，正好 7 天。
        起点用 days-1 而不是 days，否则近 7/30 天会多算一天。
        """
        if days == 0:
            return self._today_live()

        if days == 1:
            y = self._day_str(1)
            agg = self._agg_sum(y, y)
            return agg or self._today_live_or_zero()

        agg = self._agg_sum(self._day_str(days - 1), self._day_str(1))
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
        """查询每小时统计（epoch 区间条件，走索引）

        与 mac 每小时详情同口径：token 总量含缓存创建，别漏了它。
        """
        rows = self.store.query_all("""
            SELECT
                strftime('%H', created_at, 'unixepoch', 'localtime') as hour,
                COALESCE(SUM(request_count), 0) as reqs,
                COALESCE(SUM(output_tokens), 0) as output,
                COALESCE(SUM(input_tokens), 0) as input,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) as cache_create
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
                "cache_read": row[4],
                "cache_create": row[5],
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
            self._toast(
                L("用量预警"),
                L("今日 Token 用量已达 %s，超过预警阈值 %d万")
                % (self.fmt_tokens(stats['total']),
                   self.settings['warning_threshold']),
                duration=10)

            with open(notified_file, "a") as f:
                f.write(f"{today_key}\n")

    def fmt_tokens(self, tokens):
        """格式化 token 数量（中英双语：万/亿 ↔ K/M/B）"""
        return l10n.format_tokens(tokens)

    def get_menu_text(self):
        """获取菜单显示文本"""
        today = self.query_day_stats(0)
        if not today:
            return L("未找到数据源")

        total_str = self.fmt_tokens(today["total"])
        return total_str

    def build_menu(self):
        """托盘右键菜单：一屏摊开所有数据，纯显示不带入口

        动作（流水 / 洞察 / 设置 / 退出）全在左键弹窗的按钮栏里，
        右键这里只负责看数：用量、积分、渠道分布、模型分布——不分层、不点开子菜单，
        一次右键就看全。顺带跑一次用量预警检查（历史做法，通知不依赖菜单内容）。
        """
        today = self.query_day_stats(0)
        if today:
            self.check_warning(today)

        # 积分（Trae 口径；没接入就是 0）
        try:
            credits = float(self.store.query_today_credits() or 0)
        except Exception:
            credits = 0.0

        def info(text):
            # 纯展示行：action=None 就是点了不做事（pystray 会包成空操作）。
            # 不置灰：整份菜单都是数据，灰掉就没法看了。
            return pystray.MenuItem(text, None)

        menu_items = [
            # 左键触发项，故意不显示。pystray 左键走 Menu.__call__，它在 items 里
            # 找 default=True 的那条；右键走 _visible_items()，visible=False 会被滤掉。
            # 这条没了左键就是死的——托盘没有任何别的左键入口。
            pystray.MenuItem(L("打开面板"), self.toggle_popover,
                             default=True, visible=False),
            info(L("📊 用量: %s")
                 % (self.fmt_tokens(today["total"]) if today else "-")),
            info(L("💳 积分: %s") % format_credits(credits)),
            pystray.Menu.SEPARATOR,
            info(L("📡 渠道分布")),
        ]
        menu_items.extend(self._menu_stat_rows(
            [(self.store.source_display_name(s["source"]), s["total"])
             for s in self.query_source_breakdown()]))
        menu_items.append(info(L("🤖 模型分布")))
        menu_items.extend(self._menu_stat_rows(
            [(m["model"], m["total"])
             for m in (self.query_model_breakdown() or [])]))
        return menu_items

    def _menu_stat_rows(self, pairs):
        """缩进的「名称  用量」展示行；空数据留一行占位，别让小节整个消失"""
        if not pairs:
            return [pystray.MenuItem("  " + L("暂无数据"), None)]
        rows = []
        for name, total in pairs:
            label = name if len(name) <= 18 else name[:18] + "…"
            rows.append(pystray.MenuItem(
                "  %s  %s" % (label, self.fmt_tokens(total)), None))
        return rows

    # ------------------------------------------------------------ 详情窗口公共件
    # 五个详情窗口（每小时/模型/近7天/近30天/历史总量）共用：统计卡行、
    # 导航件、表头/数据行、CSV 导出与窗口位置记忆，口径对齐 mac DetailWindows.swift。

    @staticmethod
    def _detail_nav_button(parent, text, command):
        """详情窗口导航按钮（‹ / ›）"""
        import tkinter as tk
        return tk.Button(parent, text=text, command=command,
                         bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                         activebackground=Design.CARD_BORDER,
                         activeforeground=Design.TEXT_PRIMARY,
                         disabledforeground=Design.TEXT_MUTED,
                         relief="flat", bd=0, width=3,
                         font=Design.FONT_UI_SMALL, cursor="hand2")

    @staticmethod
    def _detail_today_chip(parent, command):
        """「回到今天」胶囊（只在非当前周期显示，对齐 mac DetailRootView.onToday）"""
        import tkinter as tk
        return tk.Button(parent, text=L("回到今天"), command=command,
                         bg=Design.CARD_FILL, fg=Design.BRAND,
                         activebackground=Design.CARD_BORDER,
                         activeforeground=Design.BRAND,
                         relief="flat", bd=0, padx=8, pady=1,
                         font=Design.FONT_UI_SMALL, cursor="hand2")

    @staticmethod
    def _detail_export_button(parent, command):
        """详情窗口的 CSV 导出按钮（mac 导航栏上的导出图标）"""
        import tkinter as tk
        return tk.Button(parent, text=L("导出 CSV"), command=command,
                         bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                         activebackground=Design.BTN_BG_HOVER,
                         activeforeground=Design.TEXT_PRIMARY,
                         relief="flat", bd=0, padx=10, pady=1,
                         font=Design.FONT_UI_SMALL, cursor="hand2")

    def _detail_stats_holder(self, root):
        """统计卡容器 → (holder, set_stats)；换页时 set_stats 重建卡片"""
        import tkinter as tk

        holder = tk.Frame(root, bg=Design.BACKGROUND)
        holder.pack(fill=tk.X, padx=12, pady=(6, 2))

        def set_stats(stats):
            for w in holder.winfo_children():
                w.destroy()
            if stats:
                self._detail_stat_cards(holder, stats)

        return holder, set_stats

    @staticmethod
    def _detail_stat_cards(parent, stats):
        """详情窗口统计卡行（mac DetailStat：小标签 + 大数字，可指定强调色）

        stats: [(标签, 值, 强调色或 None), ...]
        """
        import tkinter as tk

        row = tk.Frame(parent, bg=Design.BACKGROUND)
        row.pack(fill=tk.X)
        for i, (label, value, accent) in enumerate(stats):
            row.grid_columnconfigure(i, weight=1, uniform="detailstat")
            card, body = rounded_card(row, padx=10, pady=8)
            card.grid(row=0, column=i, sticky="nsew", padx=4, pady=2)
            tk.Label(body, text=label, fg=semantic_color("muted"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)
            tk.Label(body, text=value, fg=accent or Design.DATA, bg=Design.CARD_FILL,
                     font=Design.FONT_DETAIL_STAT, anchor='w').pack(fill=tk.X)
        return row

    @staticmethod
    def _detail_table_header(root, cols, padx=16):
        """详情窗口表头 + 分隔线；cols: [(文案, 宽度, 对齐), ...]"""
        import tkinter as tk

        header = tk.Frame(root, bg=Design.BACKGROUND)
        header.pack(fill=tk.X, padx=padx)
        for text, width, anchor in cols:
            tk.Label(header, text=text, width=width, anchor=anchor,
                     fg=semantic_color("muted"), bg=Design.BACKGROUND,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
        tk.Frame(root, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=padx, pady=4)

    @staticmethod
    def _detail_total_row(parent, cells):
        """详情窗口合计行（加粗整行）"""
        import tkinter as tk

        row = tk.Frame(parent, bg=Design.BACKGROUND)
        row.pack(fill=tk.X)
        for text, width, anchor in cells:
            tk.Label(row, text=text, width=width, anchor=anchor,
                     fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_VALUE).pack(side=tk.LEFT)
        return row

    @staticmethod
    def _detail_data_row(parent, vals, highlight=False):
        """详情窗口数据行；highlight=峰值行（主题色淡底，mac dataRow.highlight）"""
        import tkinter as tk

        bg = blend(Design.BRAND, Design.BACKGROUND, 0.10) if highlight else Design.BACKGROUND
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, pady=1)
        for text, width, anchor, fg in vals:
            tk.Label(row, text=text, width=width, anchor=anchor, fg=fg, bg=bg,
                     font=Design.FONT_MONO_SMALL).pack(side=tk.LEFT)
        return row

    @staticmethod
    def _detail_scroll_area(root, padx=16):
        """详情窗口可滚动数据区 → (inner, bind_wheel)"""
        import tkinter as tk

        container = tk.Frame(root, bg=Design.BACKGROUND)
        container.pack(fill=tk.BOTH, expand=True, padx=padx, pady=(0, 12))
        scrollbar = tk.Scrollbar(container, orient="vertical")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        data_frame = tk.Frame(container, bg=Design.BACKGROUND)
        data_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas_scroll = tk.Canvas(data_frame, bg=Design.BACKGROUND, highlightthickness=0,
                                  yscrollcommand=scrollbar.set)
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=canvas_scroll.yview)
        inner = tk.Frame(canvas_scroll, bg=Design.BACKGROUND)
        canvas_scroll.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_configure(_event=None):
            canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
            canvas_scroll.itemconfigure("inner", width=canvas_scroll.winfo_width())

        inner.bind("<Configure>", on_configure)
        canvas_scroll.bind("<Configure>", on_configure)

        def bind_wheel():
            def on_mousewheel(event):
                canvas_scroll.yview_scroll(int(-event.delta / 120), "units")
            canvas_scroll.bind("<MouseWheel>", on_mousewheel)
            inner.bind("<MouseWheel>", on_mousewheel)

        return inner, bind_wheel

    @staticmethod
    def _export_detail_csv(title, default_name, header, rows):
        """详情窗口导出 CSV（UTF-8 BOM + 表头，Excel 直开；返回落盘路径或 None）"""
        from tkinter import filedialog, messagebox
        import csv

        path = filedialog.asksaveasfilename(
            title=title, defaultextension=".csv", initialfile=default_name,
            filetypes=[("CSV", "*.csv")])
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
        except OSError as e:
            messagebox.showerror(L("导出失败"), str(e))
            return None
        messagebox.showinfo(L("导出完成"), L("已导出 %d 行到：\n%s") % (len(rows), path))
        return path

    # ------------------------------------------------------------ 窗口位置记忆
    # mac 用 setFrameAutosaveName；这里把 winfo_geometry() 存进设置，打开时还原。

    @staticmethod
    def _parse_geometry(text):
        """解析 Tk 几何串 'WxH+X+Y' / 'WxH-X-Y' → (w, h, x, y)；非法/缺位置返回 None"""
        import re
        if not isinstance(text, str):
            return None
        m = re.match(r"^\s*(\d+)x(\d+)([+-]\d+)?([+-]\d+)?\s*$", text)
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            return None
        x = int(m.group(3)) if m.group(3) else 0
        y = int(m.group(4)) if m.group(4) else 0
        return (w, h, x, y)

    @staticmethod
    def _geometry_on_screen(root, x, y, w, h):
        """保存的位置是否还落在屏幕里（拔掉外接显示器后要回落默认位置）

        尺寸小于 200x150 的几何串一律判废：Tk 在窗口还没映射时会把尺寸报成 1x1，
        这种值存下来下次打开会得到一个小得看不见的窗口。
        """
        if w < 200 or h < 150:
            return False
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        except Exception:
            return False
        # 至少露出一角，否则用户再也拖不回来
        return (x + w > 40 and y + h > 40 and x < sw - 40 and y < sh - 40)

    # Tk 的级联起点在 (34, 57) 附近；落在这个角落里的"记忆"其实是当年没给坐标的遗留，
    # 不是用户放的位置，还原时当没放过处理，改走居中。
    CASCADE_ARTIFACT_ZONE = 80

    def _apply_window_geometry(self, root, name, default):
        """还原窗口几何：缺省/非法/已移出屏幕/级联遗留，都回落到居中"""
        parsed = self._parse_geometry(self.settings.get("window_geometry_%s" % name))
        if parsed:
            w, h, x, y = parsed
            artifact = (abs(x) < self.CASCADE_ARTIFACT_ZONE
                        and abs(y) < self.CASCADE_ARTIFACT_ZONE)
            if not artifact and self._geometry_on_screen(root, x, y, w, h):
                root.geometry("%dx%d+%d+%d" % parsed)
                return True
        root.geometry(center_geometry(default))
        return False

    def _remember_window_geometry(self, root, name):
        """窗口关闭时把几何信息写进设置；返回可直接调用的保存函数（便于测试）"""
        key = "window_geometry_%s" % name
        state = {"geom": ""}
        try:
            state["geom"] = root.geometry()
        except Exception:
            pass

        def remember(_event=None):
            try:
                state["geom"] = root.geometry()
            except Exception:
                pass

        def save(event=None):
            if event is not None and getattr(event, "widget", None) is not root:
                return
            parsed = self._parse_geometry(state["geom"])
            if not parsed or parsed[0] < 200 or parsed[1] < 150:
                return      # 非法或还没映射（1x1）的几何串不存
            self.settings.set(key, "%dx%d+%d+%d" % parsed)
            self.settings.save()

        root.bind("<Configure>", remember, add="+")
        root.bind("<Destroy>", save, add="+")
        return save

    @staticmethod
    def _add_months(day, delta):
        """按月加减（先归到 1 号，避免 3/31 减一个月撞上不存在的 2/31）"""
        total = day.year * 12 + (day.month - 1) + delta
        return day.replace(year=total // 12, month=total % 12 + 1, day=1)


    # ------------------------------------------------ 洞察中心 / 图表共享原语

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

        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if not series:
            d.text((width // 2, height // 2), L("暂无数据"), fill="#6B6B6B",
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
            label = L("日预算 ") + (value_fmt(budget_line) if value_fmt else "%.2f" % budget_line)
            d.text((width - 4, y - 13 if y - 13 > 0 else y + 3), label,
                   fill=Design.WARNING, anchor="ra", font=CcBarTray._pil_font(10))
        d.text((4, 2), (value_fmt(maxv) if value_fmt else CcBarTray.fmt_tokens_static(maxv)),
               fill="#6B6B6B", font=CcBarTray._pil_font(11))
        if series:
            # 首尾 + 中间均分共 5 个日期刻度（原型图横轴不是只有两端）
            ticks = sorted({0, len(series) // 4, len(series) // 2,
                            (3 * len(series)) // 4, len(series) - 1})
            for i in ticks:
                if i == 0:
                    x, anchor = (i + 0.5) * slot, "la"
                elif i == len(series) - 1:
                    x, anchor = (i + 0.5) * slot, "ra"
                else:
                    x, anchor = (i + 0.5) * slot, "ma"
                d.text((x, height - 16), series[i][0], fill="#6B6B6B",
                       anchor=anchor, font=CcBarTray._pil_font(10))
        return img

    @staticmethod
    def _area_chart_png(points, color, width=520, height=120):
        """面积走势图（洞察 Hero）：折线 + 半透明填充 + 末点亮点。

        points: [(label, value)]，label 用于首尾日期标注。
        """
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if len(points) < 2:
            d.text((width // 2, height // 2), L("暂无数据"), fill="#6B6B6B",
                   anchor="mm", font=CcBarTray._pil_font(13))
            return img
        top, bottom = 14, height - 18
        maxv = max(v for _, v in points) or 1
        left, right = 8, width - 8
        step = (right - left) / (len(points) - 1)
        pts = [(left + i * step, bottom - (v / maxv) * (bottom - top))
               for i, (_, v) in enumerate(points)]
        d.polygon(pts + [(right, bottom), (left, bottom)],
                  fill=blend(color, Design.CARD_FILL, 0.22))
        d.line(pts, fill=color, width=2)
        # 末点小圆点（原型图里曲线右端有个亮点）
        d.ellipse([pts[-1][0] - 2.5, pts[-1][1] - 2.5,
                   pts[-1][0] + 2.5, pts[-1][1] + 2.5], fill=color)
        d.text((4, 0), CcBarTray.fmt_tokens_static(maxv),
               fill="#6B6B6B", font=CcBarTray._pil_font(10))
        d.text((left, height - 14), points[0][0], fill="#6B6B6B",
               font=CcBarTray._pil_font(9))
        d.text((right, height - 14), points[-1][0], fill="#6B6B6B", anchor="ra",
               font=CcBarTray._pil_font(9))
        return img

    @staticmethod
    def _line_chart_png(points, color, width=680, height=120, suffix=""):
        """深底折线图。points: [(label, value)]"""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if len(points) < 2:
            d.text((width // 2, height // 2), L("暂无数据"), fill="#6B6B6B",
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
    def _sparkline_png(points, color, width=260, height=46):
        """卡片内的迷你走势：只有折线与浅填充，无坐标文字。points: [值]"""
        from PIL import Image, ImageDraw

        vals = [float(v) for v in points]
        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if len(vals) < 2:
            return img
        maxv = max(vals) or 1
        minv = min(vals)
        rng = (maxv - minv) or maxv or 1
        left, right, top, bottom = 2, width - 3, 3, height - 4
        step = (right - left) / (len(vals) - 1)
        pts = [(left + i * step, bottom - (v - minv) / rng * (bottom - top))
               for i, v in enumerate(vals)]
        d.polygon(pts + [(right, bottom), (left, bottom)],
                  fill=blend(color, Design.CARD_FILL, 0.18))
        d.line(pts, fill=color, width=2)
        d.ellipse([pts[-1][0] - 2, pts[-1][1] - 2,
                   pts[-1][0] + 2, pts[-1][1] + 2], fill=color)
        return img

    @staticmethod
    def _stacked_area_png(series, colors, width=680, height=210, marker=None):
        """100% 堆叠面积图（渠道页构成变化）。

        series: [(label, {名称: 值})]，按构成比例铺满纵轴，不表示用量绝对值。
        marker: (i, 文案) 在第 i 个点画竖向虚线并标注（渠道切换日）。
        """
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if len(series) < 2:
            d.text((width // 2, height // 2), L("暂无数据"), fill="#6B6B6B",
                   anchor="mm", font=CcBarTray._pil_font(13))
            return img
        top, bottom, left, right = 8, height - 22, 6, width - 6
        names = [n for n in colors if any(vals.get(n, 0) for _, vals in series)]
        step = (right - left) / (len(series) - 1)
        xs = [left + i * step for i in range(len(series))]
        base = [float(bottom)] * len(series)
        for n in reversed(names):
            ys = []
            for i, (_label, vals) in enumerate(series):
                total = sum(vals.get(k, 0) for k in names) or 1
                ys.append(base[i] - vals.get(n, 0) / total * (bottom - top))
            poly = [(xs[i], ys[i]) for i in range(len(xs))]
            poly += [(xs[i], base[i]) for i in range(len(xs) - 1, -1, -1)]
            d.polygon(poly, fill=colors[n])
            base = ys
        if marker and 0 <= marker[0] < len(xs):
            mx = xs[marker[0]]
            y = top
            while y < bottom:
                d.line([(mx, y), (mx, min(y + 4, bottom))], fill=Design.WARNING)
                y += 7
            mfont = CcBarTray._pil_font(10)
            mwidth = d.textlength(marker[1], font=mfont)
            d.rectangle([mx + 4, top + 2, mx + 10 + mwidth, top + 17],
                        fill=Design.CARD_FILL)
            d.text((mx + 4, top + 2), marker[1], fill=Design.WARNING,
                   font=mfont)
        for i in range(0, len(series), max(1, len(series) // 5)):
            d.text((xs[i], height - 15), series[i][0], fill="#6B6B6B",
                   anchor="ma" if 0 < i < len(series) - 1 else ("la" if i == 0 else "ra"),
                   font=CcBarTray._pil_font(10))
        return img

    @staticmethod
    def _trend_area_png(points, color, width=680, height=190, mean=None,
                        value_fmt=None):
        """积分/费用走势：面积 + 折线 + 均值虚线 + 末点数值 + 右侧刻度。

        points: [(label, value)]；mean 有值时按同一纵轴画虚线并标注。
        """
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), Design.CARD_FILL)
        d = ImageDraw.Draw(img)
        if len(points) < 2:
            d.text((width // 2, height // 2), L("暂无数据"), fill="#6B6B6B",
                   anchor="mm", font=CcBarTray._pil_font(13))
            return img
        top, bottom, left, right = 14, height - 24, 8, width - 44
        maxv = max([v for _, v in points] + ([mean] if mean else [])) or 1
        step = (right - left) / (len(points) - 1)
        pts = [(left + i * step, bottom - (v / maxv) * (bottom - top))
               for i, (_, v) in enumerate(points)]
        d.polygon(pts + [(right, bottom), (left, bottom)],
                  fill=blend(color, Design.CARD_FILL, 0.20))
        d.line(pts, fill=color, width=2)
        fmt = value_fmt or (lambda v: CcBarTray.fmt_tokens_static(v))
        # 均值虚线
        if mean and mean > 0:
            my = bottom - (mean / maxv) * (bottom - top)
            x = left
            while x < right:
                d.line([(x, my), (min(x + 5, right), my)], fill=Design.WARNING)
                x += 9
        # 右侧刻度：0 / 半值 / 最大值
        for frac in (0.0, 0.5, 1.0):
            y = bottom - frac * (bottom - top)
            d.text((right + 6, y), fmt(maxv * frac), fill="#6B6B6B",
                   anchor="lm", font=CcBarTray._pil_font(9))
        # 末点数值
        d.text((pts[-1][0] - 4, max(pts[-1][1] - 12, 0)), fmt(points[-1][1]),
               fill=Design.TEXT_PRIMARY, anchor="ra", font=CcBarTray._pil_font(10))
        for i in range(0, len(points), max(1, len(points) // 4)):
            d.text((pts[i][0], height - 14), points[i][0], fill="#6B6B6B",
                   anchor="ma" if 0 < i < len(points) - 1 else ("la" if i == 0 else "ra"),
                   font=CcBarTray._pil_font(9))
        return img

    @staticmethod
    def fmt_tokens_static(tokens):
        """静态格式化（洞察图表用）：中英双语，同 l10n.format_tokens"""
        return l10n.format_tokens(tokens)

    @staticmethod
    def _fmt_credits(v):
        """积分显示：整数不带小数点，小数保留两位（与 mac formatCredits 同口径）"""
        return format_credits(v)

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
            weekly_report.date_range_text(now, l10n.is_english()),
            int(stats.get("total", 0) or 0),
            int(stats.get("reqs", 0) or 0), peak, trend, theme=self.theme)

    @staticmethod
    def _insight_card(parent, row, col, title, value, sub="", badge=None):
        """洞察页统计卡（两列网格中的一项，mac insightCard）

        badge 是可选的右上角小徽章：(文案, 颜色)，用来放同比涨跌。
        """
        import tkinter as tk

        card, body = rounded_card(parent, padx=14, pady=14)
        card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
        head = tk.Frame(body, bg=Design.CARD_FILL)
        head.pack(fill=tk.X)
        tk.Label(head, text=title, fg=semantic_color("secondary"), bg=Design.CARD_FILL,
                 font=Design.FONT_UI, anchor='w').pack(side=tk.LEFT)
        if badge:
            tk.Label(head, text=badge[0], fg=badge[1], bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='e').pack(side=tk.RIGHT)
        tk.Label(body, text=value, fg=Design.BIG_NUMBER, bg=Design.CARD_FILL,
                 font=Design.FONT_INSIGHT_VALUE, anchor='w').pack(fill=tk.X)
        if sub:
            tk.Label(body, text=sub, fg=semantic_color("muted"), bg=Design.CARD_FILL,
                     font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X)


    @_on_gui
    def show_settings(self, icon=None, item=None):
        """显示设置窗口（深色，内容可滚动，底部按钮栏钉底）"""
        if self._reuse_window("settings"):
            return
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog

        root = tk.Toplevel(self._ui_root)
        root.title(L("ccBar 设置"))
        root.geometry(center_geometry("1000x760"))
        root.minsize(900, 600)
        root.configure(bg=Design.BACKGROUND)

        # 左侧导航栏：与洞察中心同一套竖向 PillTabs，原型图里设置也挂在同一列
        from ui_tabs import PillTabs

        nav = PillTabs(root, vertical=True)
        nav.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 0), pady=(8, 12))
        NAV_PAGES = [L("洞察"), L("积分"), L("流水"), L("渠道"),
                     L("费用"), L("分享"), L("WorkBuddy"), L("设置")]
        NAV_ICONS = ["∿", "⚡", "≡", "▤", "◷", "⇧", "◈", "⚙"]
        for name, icon in zip(NAV_PAGES, NAV_ICONS):
            nav.add(tk.Frame(nav, bg=Design.BACKGROUND), text=name, icon=icon)

        page = tk.Frame(root, bg=Design.BACKGROUND)
        page.pack(fill=tk.BOTH, expand=True)

        rail_switching = {"on": False}

        def _nav_changed():
            if rail_switching["on"]:
                return
            sel = nav.tab(nav.select())
            if sel == L("设置"):
                return
            rail_switching["on"] = True
            self.show_insights(initial_page=sel)
            root.destroy()

        nav.bind("<<NotebookTabChanged>>", _nav_changed)
        nav.select(nav.tabs()[-1])

        head = tk.Frame(page, bg=Design.BACKGROUND)
        head.pack(fill=tk.X, padx=24, pady=(18, 10))
        hleft = tk.Frame(head, bg=Design.BACKGROUND)
        hleft.pack(side=tk.LEFT)
        tk.Label(hleft, text=L("设置"), font=Design.FONT_WINDOW_TITLE,
                 fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND).pack(anchor='w')
        tk.Label(hleft, text=L("改动需点击保存后生效"), font=Design.FONT_UI_SMALL,
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND).pack(anchor='w')
        hright = tk.Frame(head, bg=Design.BACKGROUND)
        hright.pack(side=tk.RIGHT, pady=(6, 0))
        tk.Label(hright, text=L("搜索设置"), fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(0, 8))
        search_var = tk.StringVar()
        search_entry = tk.Entry(hright, textvariable=search_var, width=14,
                                bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                                insertbackground=Design.TEXT_PRIMARY, relief="flat",
                                font=Design.FONT_UI_SMALL, highlightthickness=1,
                                highlightbackground=Design.CARD_BORDER,
                                highlightcolor=Design.BRAND)
        search_entry.pack(side=tk.LEFT, ipady=3)
        tk.Frame(page, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, padx=24)

        # 按钮栏钉底，内容区自己滚动
        btn_bar = tk.Frame(page, bg=Design.BACKGROUND)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=(8, 16))

        body = tk.Frame(page, bg=Design.BACKGROUND)
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

        def search_settings(event=None):
            """搜索设置：找到第一个命中的说明文字并滚到它，命中项短暂高亮"""
            keyword = search_var.get().strip().lower()
            if not keyword:
                return
            hits = []

            def walk(widget):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Label):
                        try:
                            text = str(child.cget("text"))
                        except Exception:
                            text = ""
                        if text and keyword in text.lower():
                            hits.append(child)
                    walk(child)
            walk(inner)
            if not hits:
                return
            target = hits[0]
            try:
                offset = target.winfo_rooty() - inner.winfo_rooty()
                canvas.yview_moveto(max(0.0, min(1.0, offset / max(inner.winfo_height(), 1))))
                for h in hits[:8]:
                    h.configure(fg=Design.BRAND)
            except Exception:
                pass

        search_entry.bind("<Return>", search_settings)

        OK_COLOR, WARN_COLOR = "#34C759", "#FF9500"
        dirty_label = tk.Label(btn_bar, text="", fg=WARN_COLOR, bg=Design.BACKGROUND,
                               font=Design.FONT_UI_SMALL)
        dirty_label.pack(side=tk.LEFT)

        def mark_dirty(*_args):
            try:
                dirty_label.configure(text=L("配置已修改"))
            except Exception:
                pass

        # 显示名统一由 l10n.language_display_name 给（"中文" 一项刻意保持中文）
        LANG_LABELS = {p: l10n.language_display_name(p) for p in l10n.LANGUAGES}
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
            entry.bind("<KeyRelease>", mark_dirty)
            if hint:
                tk.Label(row, text=hint, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                         font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))
            return entry

        def small_btn(parent, text, cmd, primary=False):
            return flat_button(parent, text, cmd, primary)

        def check_row(text, value):
            var = tk.BooleanVar(value=bool(value))
            # 自绘胶囊开关（原生 Checkbutton 的方框和暗色界面不搭）
            ToggleRow(inner, text=text, var=var, bg=Design.BACKGROUND).pack(
                fill=tk.X, padx=24, pady=4)
            var.trace_add("write", mark_dirty)
            return var

        custom = [dict(t) for t in (self.settings.get("custom_themes") or [])]

        # ------------------------------------------------------------ 主题
        section(L("外观"))
        theme_row = tk.Frame(inner, bg=Design.BACKGROUND)
        theme_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(theme_row, text=L("主题风格"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=18, anchor='w').pack(side=tk.LEFT)

        current_theme = self.theme or themes.default_theme()
        theme_var = tk.StringVar(value=L(current_theme.get("name")
                                         or themes.default_theme()["name"]))

        def theme_names():
            """主题显示名（英文界面下过 L；主题包本身的名字/ID 不动）"""
            names = []
            for t in themes.all_themes(custom):
                label = L(t["name"])
                if label not in names:
                    names.append(label)
            return names

        theme_menu = tk.OptionMenu(theme_row, theme_var, *theme_names())
        theme_menu.configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY, relief="flat", bd=0,
                             activebackground=Design.BTN_BG_HOVER,
                             activeforeground=Design.TEXT_PRIMARY, highlightthickness=0,
                             font=Design.FONT_UI_SMALL, width=12)
        theme_menu["menu"].configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                                     activebackground=Design.BRAND, font=Design.FONT_UI_SMALL)
        theme_menu.pack(side=tk.LEFT, padx=(0, 8))

        # 主题色卡：点色块直接换主题，不用去下拉菜单里找名字
        swatches = ThemeSwatches(
            inner,
            themes=[(L(t["name"]), t.get("accent") or themes.default_theme()["accent"])
                    for t in themes.all_themes(custom)],
            on_pick=theme_var.set,
            bg=Design.BACKGROUND)
        swatches.pack(fill=tk.X, padx=24, pady=(8, 0))
        swatches.set_selected(theme_var.get())
        theme_var.trace_add("write", lambda *args: swatches.set_selected(theme_var.get()))

        # 主题操作按钮单独一行：挤在主题下拉旁边会顶出右边界
        theme_actions = tk.Frame(inner, bg=Design.BACKGROUND)
        theme_actions.pack(fill=tk.X, padx=24, pady=(6, 0))

        def selected_theme():
            name = theme_var.get()
            for t in themes.all_themes(custom):
                if L(t["name"]) == name:
                    return t
            return themes.default_theme()

        def refresh_theme_menu():
            menu = theme_menu["menu"]
            menu.delete(0, "end")
            for name in theme_names():
                menu.add_command(label=name, command=lambda n=name: theme_var.set(n))

        def import_theme():
            path = filedialog.askopenfilename(title=L("导入主题包"),
                                              filetypes=[(L("ccBar 主题包"), "*.json"), ("All", "*.*")])
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    theme = themes.theme_from_json(f.read())
            except Exception as e:
                messagebox.showerror(L("导入失败"), L("主题包读取失败：%s") % e)
                return
            custom[:] = themes.upsert_custom(custom, theme)
            theme_var.set(L(theme["name"]))
            refresh_theme_menu()
            messagebox.showinfo(L("导入成功"), L("主题「%s」已加入可选列表（保存后生效）") % theme["name"])

        def export_theme():
            name = theme_var.get()
            path = filedialog.asksaveasfilename(
                title=L("导出主题包"), defaultextension=".json",
                initialfile="ccbar-theme-%s.json" % name,
                filetypes=[(L("ccBar 主题包"), "*.json")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(themes.theme_to_json(selected_theme()))
                messagebox.showinfo(L("导出成功"), L("已导出到：\n%s") % path)
            except Exception as e:
                messagebox.showerror(L("导出失败"), str(e))

        def rename_theme():
            name = theme_var.get()
            theme = selected_theme()
            if not themes.is_custom(theme.get("id"), custom):
                messagebox.showinfo(L("提示"), L("内置主题不能重命名"))
                return
            new_name = simpledialog.askstring(L("重命名主题"), L("新名称："), initialvalue=name,
                                             parent=root)
            if not new_name or new_name == name:
                return
            custom[:] = themes.rename_custom(custom, theme["id"], new_name)
            theme_var.set(L(new_name))
            refresh_theme_menu()

        def delete_theme():
            theme = selected_theme()
            if not themes.is_custom(theme.get("id"), custom):
                messagebox.showinfo(L("提示"), L("内置主题不能删除"))
                return
            custom[:] = themes.delete_custom(custom, theme["id"])
            theme_var.set(L(themes.default_theme()["name"]))
            refresh_theme_menu()

        small_btn(theme_actions, L("导入"), import_theme).pack(side=tk.LEFT)
        small_btn(theme_actions, L("导出"), export_theme).pack(side=tk.LEFT, padx=(6, 0))
        small_btn(theme_actions, L("重命名"), rename_theme).pack(side=tk.LEFT, padx=(6, 0))
        small_btn(theme_actions, L("删除"), delete_theme).pack(side=tk.LEFT, padx=(6, 0))

        # 界面语言
        lang_row = tk.Frame(inner, bg=Design.BACKGROUND)
        lang_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(lang_row, text=L("界面语言"), fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=18, anchor='w').pack(side=tk.LEFT)
        lang_var = tk.StringVar(value=LANG_LABELS.get(
            self.settings.get("app_language") or "system",
            l10n.language_display_name("system")))
        lang_menu = tk.OptionMenu(lang_row, lang_var, *LANG_LABELS.values())
        lang_menu.configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY, relief="flat", bd=0,
                            activebackground=Design.BTN_BG_HOVER,
                            activeforeground=Design.TEXT_PRIMARY, highlightthickness=0,
                            font=Design.FONT_UI_SMALL, width=12)
        lang_menu["menu"].configure(bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                                    activebackground=Design.BRAND, font=Design.FONT_UI_SMALL)
        lang_menu.pack(side=tk.LEFT)
        tk.Label(lang_row, text=L("重开窗口后生效"), fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 0))

        interval_entry = field_row(L("刷新间隔 (秒):"), L("范围 5 - 3000"))
        interval_entry.insert(0, str(self.settings["refresh_interval"]))

        # ------------------------------------------------------------ 数据源
        section(L("数据源"))
        _connected = sum(1 for _k in ("ccswitch_enabled", "zcode_enabled", "trae_enabled")
                         if self.settings.get(_k))
        tk.Label(inner, text=L("%d 个已连接") % _connected, fg=OK_COLOR,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='e').pack(fill=tk.X, padx=24)

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
                status.config(text=L("表结构正确（保存后生效）"), fg=OK_COLOR)
            else:
                # validate_db 返回的是数据层的中文原文，显示时才过 L（同 macOS）
                status.config(text=L(error), fg=WARN_COLOR)

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
            var.trace_add("write", mark_dirty)
            ToggleSwitch(row, var=var, bg=Design.BACKGROUND).pack(side=tk.LEFT)

            tk.Label(row, text=adapter.name, fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                     font=Design.FONT_UI, width=10, anchor='w').pack(side=tk.LEFT, padx=(8, 0))

            entry = tk.Entry(row, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                             insertbackground=Design.TEXT_PRIMARY, relief="flat",
                             font=Design.FONT_MONO_SMALL, highlightthickness=1,
                             highlightbackground=Design.CARD_BORDER,
                             highlightcolor=Design.BRAND)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            entry.insert(0, self.settings.get(path_key) or adapter.default_path)
            entry.bind("<KeyRelease>", mark_dirty)
            source_entries[adapter.id] = entry

            def make_browse(entry_box, adapter_id):
                def browse():
                    filename = filedialog.askopenfilename(
                        title=L("选择数据库文件"),
                        filetypes=[("SQLite", "*.db *.sqlite"), ("All", "*.*")])
                    if filename:
                        entry_box.delete(0, tk.END)
                        entry_box.insert(0, filename)
                        validate_source(adapter_id)
                return browse

            small_btn(row, L("浏览"), make_browse(entry, adapter.id)).pack(side=tk.LEFT, padx=(8, 0))

            status = tk.Label(src, text="", fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                              font=Design.FONT_UI_SMALL, anchor='w')
            status.pack(fill=tk.X, pady=(2, 0))
            source_status[adapter.id] = status
            if adapter.id in attached_ids:
                status.config(text=L("已连接"), fg=OK_COLOR)
            elif not var.get():
                status.config(text=L("未启用"), fg=Design.TEXT_MUTED)
            else:
                status.config(text=L("未连接"), fg=WARN_COLOR)
            entry.bind("<FocusOut>", lambda e, aid=adapter.id: validate_source(aid))

        # Trae 是 HTTP 源（不在 SOURCE_REGISTRY 里），用登录凭据代替库路径
        trae_row = tk.Frame(inner, bg=Design.BACKGROUND)
        trae_row.pack(fill=tk.X, padx=24, pady=(8, 0))
        trae_head = tk.Frame(trae_row, bg=Design.BACKGROUND)
        trae_head.pack(fill=tk.X)

        trae_var = tk.BooleanVar(value=self.settings.get("trae_enabled", False))
        ToggleSwitch(trae_head, var=trae_var, bg=Design.BACKGROUND).pack(side=tk.LEFT)
        tk.Label(trae_head, text="Trae", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=10, anchor='w').pack(side=tk.LEFT, padx=(8, 0))

        trae_entry = tk.Entry(trae_head, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                              insertbackground=Design.TEXT_PRIMARY, relief="flat",
                              font=Design.FONT_MONO_SMALL, highlightthickness=1,
                              highlightbackground=Design.CARD_BORDER,
                              highlightcolor=Design.BRAND)
        trae_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        trae_entry.insert(0, self.settings.get("trae_sessionid") or "")
        trae_hint = tk.Frame(trae_row, bg=Design.BACKGROUND)
        trae_hint.pack(fill=tk.X, pady=(2, 0))
        tk.Label(trae_hint, text=L("粘贴 sessionid / 整段 Cookie / X-Cloudide-Session"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(side=tk.LEFT)

        def show_trae_help():
            """凭据是一长串 Cookie，不给步骤用户不知道从哪里复制"""
            import webbrowser

            dlg = tk.Toplevel(root)
            dlg.title(L("获取 Trae 凭据"))
            dlg.configure(bg=Design.BACKGROUND)
            dlg.transient(root)
            dlg.resizable(False, False)

            wrap = tk.Frame(dlg, bg=Design.BACKGROUND, padx=18, pady=14)
            wrap.pack(fill=tk.BOTH, expand=True)
            tk.Label(wrap, text=L("获取 Trae 凭据"), fg=Design.TEXT_PRIMARY,
                     bg=Design.BACKGROUND, font=Design.FONT_UI, anchor='w').pack(fill=tk.X)
            tk.Label(wrap, text=L("sessionid 就是登录态；推荐只复制它的值，不要带引号。"),
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                     anchor='w', justify='left', wraplength=400).pack(fill=tk.X, pady=(3, 10))

            steps = [
                L("浏览器打开 trae.cn 并登录"),
                L("按 F12 打开开发者工具，进入 Application → Cookies → https://www.trae.cn"),
                L("找到 sessionid，双击 Value 后复制（约 60 天有效）"),
                L("粘贴到左侧输入框，点「保存」"),
            ]
            steps_box = tk.Frame(wrap, bg=Design.BACKGROUND)
            steps_box.pack(fill=tk.X)
            for idx, step in enumerate(steps, 1):
                line = tk.Frame(steps_box, bg=Design.BACKGROUND)
                line.pack(fill=tk.X, pady=2)
                tk.Label(line, text="%d." % idx, fg=Design.BRAND, bg=Design.BACKGROUND,
                         font=Design.FONT_MONO_SMALL, width=2,
                         anchor='ne').pack(side=tk.LEFT)
                tk.Label(line, text=step, fg=Design.TEXT_SECONDARY, bg=Design.BACKGROUND,
                         font=Design.FONT_UI_SMALL, anchor='w', justify='left',
                         wraplength=380).pack(side=tk.LEFT, fill=tk.X, expand=True)

            tk.Frame(wrap, bg=Design.SEPARATOR, height=1).pack(fill=tk.X, pady=(10, 8))
            tk.Label(wrap,
                     text=L("也支持 X-Cloudide-Session=…（约 14 天）或整段 Cookie，但只填 sessionid 最省事。"),
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                     anchor='w', justify='left', wraplength=400).pack(fill=tk.X)
            tk.Label(wrap, text=L("凭据只保存在本机，仅用于请求 Trae 官方接口。"),
                     fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                     anchor='w', justify='left', wraplength=400).pack(fill=tk.X, pady=(3, 0))

            btns = tk.Frame(wrap, bg=Design.BACKGROUND)
            btns.pack(fill=tk.X, pady=(12, 0))
            flat_button(btns, L("打开 trae.cn"),
                        lambda: webbrowser.open("https://www.trae.cn")).pack(side=tk.LEFT)
            flat_button(btns, L("知道了"), dlg.destroy, primary=True).pack(side=tk.RIGHT)

            x = root.winfo_rootx() + max(0, (root.winfo_width() - 440) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - 380) // 3)
            dlg.geometry("+%d+%d" % (x, y))
            dlg.after(50, dlg.grab_set)
            dlg.focus_set()

        flat_button(trae_hint, L("如何获取？"), show_trae_help).pack(side=tk.RIGHT)

        # 状态行：HTTP 源没有库路径可校验，直接透出统计库的诊断信息
        try:
            # source_status 是数据层的中文原文（未启用/已连接/同步失败…），显示时才过 L
            trae_status_text = L(self.store.source_status.get("trae", "") or "")
            ent = self.store.trae_ent_summary()
        except Exception:
            trae_status_text, ent = "", None
        if ent:
            trae_status_text += L(" · 积分 %s/%s") % (self._fmt_credits(ent[0]),
                                                     self._fmt_credits(ent[1]))
        tk.Label(trae_row, text=trae_status_text, fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(fill=tk.X, pady=(2, 0))

        # 后台拉取频率（默认 10 分钟；label_width 对齐上面的 Trae 勾选列）
        trae_interval_entry = field_row(
            L("后台同步间隔 (秒):"),
            L("范围 %d - %d") % (trae_sync.BG_INTERVAL_MIN, trae_sync.BG_INTERVAL_MAX),
            label_width=10)
        trae_interval_entry.insert(0, str(self.settings.get(
            "trae_bg_interval", trae_sync.BG_MIN_INTERVAL)))

        # ------------------------------------------------------------ WorkBuddy（独立源）
        # 与上面数据源刻意分开：它的数据只出现在洞察中心的独立页签，不混进任何其他页面
        section("WorkBuddy")
        wb_row = tk.Frame(inner, bg=Design.BACKGROUND)
        wb_row.pack(fill=tk.X, padx=24, pady=(8, 0))
        wb_head = tk.Frame(wb_row, bg=Design.BACKGROUND)
        wb_head.pack(fill=tk.X)

        wb_var = tk.BooleanVar(value=self.settings.get("workbuddy_enabled", False))
        ToggleSwitch(wb_head, var=wb_var, bg=Design.BACKGROUND).pack(side=tk.LEFT)
        tk.Label(wb_head, text="WorkBuddy", fg=Design.TEXT_PRIMARY, bg=Design.BACKGROUND,
                 font=Design.FONT_UI, width=10, anchor='w').pack(side=tk.LEFT, padx=(8, 0))

        # 凭据是一整条 Cookie（可达数 KB），单行 Entry 不实用，用 2 行多行框
        wb_text = tk.Text(wb_head, height=2, bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                          insertbackground=Design.TEXT_PRIMARY, relief="flat", wrap=tk.CHAR,
                          font=Design.FONT_MONO_SMALL, highlightthickness=1,
                          highlightbackground=Design.CARD_BORDER, highlightcolor=Design.BRAND)
        wb_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        wb_text.insert("1.0", self.settings.get("workbuddy_cookie") or "")

        tk.Label(wb_row,
                 text=L("粘贴 codebuddy.cn 的 Cookie（也可直接粘贴整条 curl 命令，只需 session）"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(fill=tk.X, pady=(2, 0))

        wb_btn_row = tk.Frame(wb_row, bg=Design.BACKGROUND)
        wb_btn_row.pack(fill=tk.X, pady=(4, 0))

        def wb_import_clipboard():
            """从剪贴板导入：整条 Cookie 或整条 curl 命令都能识别"""
            try:
                raw = root.clipboard_get()
            except Exception:
                raw = ""
            normalized = workbuddy_sync.normalize_cookie(raw)
            if not normalized:
                messagebox.showerror(L("无法导入"),
                                     L("剪贴板里没有可识别的 Cookie（缺 session）"))
                return
            wb_text.delete("1.0", tk.END)
            wb_text.insert("1.0", normalized)

        def wb_clear():
            wb_text.delete("1.0", tk.END)

        small_btn(wb_btn_row, L("从剪贴板导入"), wb_import_clipboard).pack(side=tk.LEFT)
        small_btn(wb_btn_row, L("清空"), wb_clear).pack(side=tk.LEFT, padx=(6, 0))

        # 状态行：数据层的中文原文（未启用/已连接/登录已过期…），显示时才过 L
        try:
            wb_status_text = L(self.store.source_status.get("workbuddy", "") or "")
            wb_cookie_now = self.store.workbuddy_live_cookie()
        except Exception:
            wb_status_text, wb_cookie_now = "", ""
        wb_expiry = workbuddy_sync.cookie_expiry(wb_cookie_now)
        if wb_expiry:
            wb_status_text += L(" · 有效期至 %s") % time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(wb_expiry))
        elif wb_cookie_now:
            wb_status_text += L(" · 有效期未知")
        tk.Label(wb_row, text=wb_status_text, fg=Design.TEXT_MUTED, bg=Design.BACKGROUND,
                 font=Design.FONT_UI_SMALL, anchor='w').pack(fill=tk.X, pady=(2, 0))

        wb_interval_entry = field_row(
            L("后台同步间隔 (秒):"),
            L("范围 %d - %d") % (workbuddy_sync.BG_INTERVAL_MIN,
                                 workbuddy_sync.BG_INTERVAL_MAX),
            label_width=10)
        wb_interval_entry.insert(0, str(self.settings.get(
            "workbuddy_bg_interval", workbuddy_sync.BG_MIN_INTERVAL)))

        tk.Label(wb_row, text=L("WorkBuddy 数据独立统计，只出现在洞察中心"),
                 fg=Design.TEXT_MUTED, bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL,
                 anchor='w').pack(fill=tk.X, pady=(6, 0))

        # ------------------------------------------------------------ 提醒与费用
        section(L("提醒"))
        warning_entry = field_row(L("预警阈值 (万):"), L("超过此值将弹出通知提醒"))
        warning_entry.insert(0, str(self.settings["warning_threshold"]))
        notify_entry = field_row(L("通知间隔 (万):"), L("每累计N万通知一次，0=关闭"))
        notify_entry.insert(0, str(self.settings.get("notify_interval", 1000)))
        led_entry = field_row(L("红色门槛 (万):"), L("单次刷新增量达到即变红，0=不变红"))
        led_entry.insert(0, str(self.settings.get("led_red_threshold", 100)))

        section(L("费用估算"))
        budget_entry = field_row(L("月度预算 ($):"), L("0=关闭预算显示"))
        budget_value = self.settings.get("monthly_budget_usd") or 0
        budget_entry.insert(0, ("%g" % budget_value) if budget_value else "0")
        price_entry = field_row(L("默认单价 ($/M):"), L("未计费渠道按此估算，0=关闭"))
        price_value = self.settings.get("default_token_price") or 0
        price_entry.insert(0, ("%g" % price_value) if price_value else "0")

        # ------------------------------------------------------------ 开关
        section(L("行为"))
        warning_var = check_row(L("启用用量预警"), self.settings["warning_enabled"])
        login_var = check_row(L("开机自动启动"), self.settings.get("launch_at_login"))
        report_var = check_row(L("每周一自动生成用量周报"),
                               self.settings.get("auto_weekly_report", True))
        wide_var = check_row(L("宽版弹窗（380pt）"), self.settings.get("popover_wide"))

        backup_row = tk.Frame(inner, bg=Design.BACKGROUND)
        backup_row.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(backup_row,
                 text=L("上次自动备份：%s") % (self.settings.get("last_auto_backup_date") or L("从未")),
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

        small_btn(backup_row, L("打开备份目录"), open_backup_dir).pack(side=tk.RIGHT)

        # ------------------------------------------------------------ 数据
        section(L("数据"))

        mig = tk.Frame(inner, bg=Design.BACKGROUND)
        mig.pack(fill=tk.X, padx=24, pady=(10, 0))
        tk.Label(mig, text=L("明细 CSV 迁移（导入幂等：主键去重）"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
        tk.Button(mig, text=L("导入 CSV"), command=lambda: self.import_data(root),
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT)
        tk.Button(mig, text=L("导出 CSV"), command=self.export_data,
                  bg=Design.CARD_FILL, fg=Design.TEXT_PRIMARY,
                  activebackground=Design.BTN_BG_HOVER, activeforeground=Design.TEXT_PRIMARY,
                  relief="flat", bd=0, padx=12, cursor="hand2",
                  font=Design.FONT_UI_SMALL).pack(side=tk.RIGHT, padx=(0, 8))

        def merge_database():
            """多机合并：把另一台机器的统计库明细按主键并进来（不重不漏）"""
            path = filedialog.askopenfilename(
                title=L("选择另一台机器的 ccbar.db"),
                filetypes=[("SQLite", "*.db"), ("All", "*.*")])
            if not path:
                return
            merge = getattr(self.store, "merge_from_db", None)
            if merge is None:
                messagebox.showerror(L("合并失败"), L("当前版本不支持多机合并"))
                return
            read, inserted = merge(path)
            if read == 0 and inserted == 0:
                messagebox.showerror(L("合并失败"), L("不是有效的 ccBar 统计库（缺 usage_log 表或打不开）"))
                return
            messagebox.showinfo(L("合并完成"),
                                L("共读取 %d 行 · 新增 %d 行（主键去重）") % (read, inserted))

        merge_row = tk.Frame(inner, bg=Design.BACKGROUND)
        merge_row.pack(fill=tk.X, padx=24, pady=(10, 4))
        tk.Label(merge_row, text=L("多机合并（另一台机器的统计库）"), fg=Design.TEXT_MUTED,
                 bg=Design.BACKGROUND, font=Design.FONT_UI_SMALL).pack(side=tk.LEFT)
        tk.Button(merge_row, text=L("合并数据库…"), command=merge_database,
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
            wide_var.set(False)
            lang_var.set(LANG_LABELS["system"])
            custom[:] = []
            theme_var.set(L(themes.default_theme()["name"]))
            refresh_theme_menu()
            trae_var.set(False)
            trae_entry.delete(0, tk.END)
            trae_interval_entry.delete(0, tk.END)
            trae_interval_entry.insert(
                0, str(app_settings.DEFAULTS["trae_bg_interval"]))
            wb_var.set(False)
            wb_text.delete("1.0", tk.END)
            wb_interval_entry.delete(0, tk.END)
            wb_interval_entry.insert(
                0, str(app_settings.DEFAULTS["workbuddy_bg_interval"]))

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
                "popover_wide": wide_var.get(),
                "app_language": LANG_BY_LABEL.get(lang_var.get(), "system"),
                "theme": selected_theme().get("id") or themes.default_theme()["id"],
                "custom_themes": custom,
            }
            for adapter in SOURCE_REGISTRY:
                enable_key, path_key = source_keys.get(
                    adapter.id, ("%s_enabled" % adapter.id, "%s_path" % adapter.id))
                updates[enable_key] = source_vars[adapter.id].get()
                path_value = source_entries[adapter.id].get().strip()
                updates[path_key] = path_value or adapter.default_path
            # Trae 是 HTTP 源：开关 + 登录凭据 + 后台同步间隔（validate/apply 一并处理）
            updates["trae_enabled"] = trae_var.get()
            updates["trae_sessionid"] = trae_entry.get().strip()
            updates["trae_bg_interval"] = trae_interval_entry.get()
            # WorkBuddy 同理；粘贴的内容统一归一化成 "session=…; session_2=…"
            wb_raw = wb_text.get("1.0", tk.END)
            updates["workbuddy_enabled"] = wb_var.get()
            updates["workbuddy_cookie"] = (workbuddy_sync.normalize_cookie(wb_raw)
                                           or wb_raw.strip())
            updates["workbuddy_bg_interval"] = wb_interval_entry.get()
            return updates

        def save():
            updates = collect()
            errors = self.settings.validate(updates)
            if errors:
                messagebox.showerror(L("设置未保存"),
                                     L("请检查：\n· ")
                                     + "\n· ".join(L(e) for e in errors))
                return
            self.settings.apply(updates)
            # 开关类设置需要同步系统状态
            try:
                autostart.apply(self.settings.get("launch_at_login"))
            except Exception as e:
                print(L("开机自启设置失败:"), e)
            self.apply_language()
            self.apply_theme()
            # 立即按新设置重连统计库（启用新源会触发首次全量回填）。
            # 回填可能很久，必须放后台，别在保存时把设置窗口冻住。
            self._run_async(self.connect_store)
            messagebox.showinfo(L("成功"), L("设置已保存，将在下次刷新时生效"))
            root.destroy()

        small_btn(btn_bar, L("重置"), reset).pack(side=tk.RIGHT)
        small_btn(btn_bar, L("检查更新"), self.check_for_updates).pack(side=tk.RIGHT, padx=(0, 8))
        small_btn(btn_bar, L("备份数据"), self.backup_data).pack(side=tk.RIGHT, padx=(0, 8))
        small_btn(btn_bar, L("保存"), save, primary=True).pack(side=tk.RIGHT, padx=(0, 8))

        self._register_window("settings", root)
        self._bring_to_front(root)

    @_on_gui
    def backup_data(self, icon=None, item=None):
        """一键备份统计库（VACUUM INTO 导出独立 db 文件，与 macOS 版同款）"""
        from tkinter import filedialog, messagebox

        stamp = datetime.now().strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            title=L("备份统计库"),
            defaultextension=".db",
            initialfile=f"ccbar-backup-{stamp}.db",
            filetypes=[("SQLite", "*.db"), ("All", "*.*")])
        if not path:
            return

        def done(ok):
            if ok:
                messagebox.showinfo(
                    L("备份完成"),
                    L("统计库已备份到：\n%s\n\n恢复方式：退出 ccBar 后用备份文件替换\n"
                      "~/.ccbar/ccbar.db") % path)
            else:
                messagebox.showerror(L("备份失败"), L("统计库未打开或目标位置不可写"))

        # VACUUM 在大库上要几秒，放后台线程，别冻住设置窗口
        self._run_async(lambda: self.store.backup(path), done)

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
                    L("发现新版本"),
                    L("最新版本 v%s，当前 v%s\n是否前往下载？") % (latest, APP_VERSION)):
                webbrowser.open(RELEASES_URL)
            return
        messagebox.showinfo(
            L("检查更新"),
            L("已经是最新版本（v%s）") % APP_VERSION if latest
            else L("检查失败，稍后再试，或直接到 GitHub Releases 页面查看"))

    def export_data(self):
        """导出明细 CSV（换机迁移 / Excel 查看）"""
        from tkinter import filedialog, messagebox
        stamp = datetime.now().strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            title=L("导出明细"), defaultextension=".csv",
            initialfile=f"ccbar-export-{stamp}.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return

        def done(n):
            if n >= 0:
                messagebox.showinfo(L("导出完成"),
                                    L("已导出 %d 行明细到：\n%s") % (n, path))
            else:
                messagebox.showerror(L("导出失败"), L("统计库未打开或目标位置不可写"))

        # 全表明细可能几十万行，导出放后台线程并流式写盘
        self._run_async(lambda: self.store.export_csv(path), done)

    def import_data(self, root):
        """幂等导入明细 CSV：主键去重，同一文件重复导零新增"""
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(title=L("导入明细"),
                                          filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return

        def done(result):
            read, inserted, skipped = result
            if skipped == -1:
                messagebox.showerror(L("导入失败"),
                                     L("不是 ccBar 导出的明细 CSV（表头不符）"))
                return
            messagebox.showinfo(
                L("导入完成"),
                L("共读取 %d 行 · 新增 %d 行 · 跳过 %d 行（重复或非法）")
                % (read, inserted, skipped))

        def failed(e):
            messagebox.showerror(L("导入失败"), str(e))

        # 读 CSV 与写库都可能耗时，统统放后台线程
        self._run_async(lambda: self.store.import_csv(path), done, failed)

    def quit_app(self, icon, item):
        """退出应用"""
        icon.stop()

    def update_icon(self):
        """更新图标和标题"""
        while True:
            try:
                time.sleep(self.settings["refresh_interval"])
                # 懒惰补账：历史（昨天及更早）落后就同步进自建库
                self.store.sync_if_needed()
                # Trae 用量（HTTP 源）：后台按设置间隔节流（默认 10 分钟），0-9 点静默
                self.sync_trae(interactive=False)
                # WorkBuddy（独立表）：同样按自己的间隔节流，0-9 点静默
                self.sync_workbuddy(interactive=False)
                # 按用量更新图标颜色
                self.update_icon_color()
                # 检查里程碑（每1000万token冒泡通知）
                today = self.query_day_stats(0)
                if today:
                    self.check_token_milestone(today["total"])
                # 每天首次刷新自动备份统计库（滚动保留 7 份）
                self.maybe_auto_backup()
                # 周一自动生成上周用量周报（错过周一下次刷新补生成）
                self.maybe_generate_weekly_report()
                # 静默检查更新（3 天一次，只有真有新版才提示）
                self.maybe_check_updates_silently()
                # 更新菜单
                self._refresh_tray()
            except Exception as e:
                # 单轮失败不能杀掉托盘更新线程（下一轮照跑），记日志便于排查
                print(L("刷新异常: ") + str(e))
                logger.warning("refresh error: %s", e)

    def sync_trae(self, interactive=False):
        """跑一次 Trae 同步（网络请求不能堵 GUI/托盘线程，放后台线程）"""
        sync = getattr(self.store, "sync_trae_if_needed", None)
        if sync is None or not self.settings.get("trae_enabled"):
            return
        # 登录态与后台间隔可能在设置里改过，先同步进统计库
        self.store.trae_sessionid = self.settings.get("trae_sessionid") or ""
        self.store.trae_bg_interval = trae_sync.clamp_bg_interval(
            self.settings.get("trae_bg_interval"))

        def worker():
            try:
                sync(interactive=interactive)
            except Exception as e:
                print(L("Trae 同步失败:"), e)
                logger.warning("Trae sync failed: %s", e)

        threading.Thread(target=worker, daemon=True).start()

    def sync_workbuddy(self, interactive=False):
        """跑一次 WorkBuddy 同步（独立表，不参与主界面；同样放后台线程）"""
        sync = getattr(self.store, "sync_workbuddy_if_needed", None)
        if sync is None or not self.settings.get("workbuddy_enabled"):
            return
        # 凭据与后台间隔可能在设置里改过，先同步进统计库
        self._apply_workbuddy_settings()

        def worker():
            try:
                sync(interactive=interactive)
            except Exception as e:
                print(L("WorkBuddy 同步失败:"), e)
                logger.warning("WorkBuddy sync failed: %s", e)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_workbuddy_settings(self):
        """把设置里的 WorkBuddy 凭据与间隔同步进统计库（任意线程可调）"""
        self.store.workbuddy_cookie = workbuddy_sync.normalize_cookie(
            self.settings.get("workbuddy_cookie") or "")
        self.store.workbuddy_bg_interval = workbuddy_sync.clamp_bg_interval(
            self.settings.get("workbuddy_bg_interval"))

    def _prepare_workbuddy_sync(self):
        """后台线程里同步跑一次 WorkBuddy（交互档节流），保证洞察页读到最新数字"""
        sync = getattr(self.store, "sync_workbuddy_if_needed", None)
        if sync is None or not self.settings.get("workbuddy_enabled"):
            return
        self._apply_workbuddy_settings()
        try:
            sync(interactive=True)
        except Exception as e:
            logger.warning("WorkBuddy insights sync failed: %s", e)

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
        """托盘悬停提示：只显示今日用量；有积分才追加今日积分"""
        today = self.query_day_stats(0)
        if not today:
            return L("ccBar - 未找到数据")
        text = L("今日用量：%s") % self.fmt_tokens(today["total"])
        try:
            credits = float(self.store.query_today_credits() or 0)
        except Exception:
            credits = 0.0
        if credits > 0:
            text += "\n" + L("今日积分：%s") % format_credits(credits)
        return text

    def run(self):
        """运行应用"""
        # 必须早于任何 Tk 窗口创建（否则只对后续进程生效）
        enable_dpi_awareness()
        # 先起 GUI 线程，后续所有窗口都投递到它上面
        self._start_gui()

        # 创建图标：启动即静息灰（mac 同款，下一次刷新有增量才亮绿/红）。
        # 托盘必须在任何回填/备份之前出现——首次全量补账可能要几十秒到分钟级，
        # 放在 icon.run() 之前会让用户以为程序没启动。初始菜单走"未找到数据源"占位，
        # 连接完成后由 _bootstrap 立即刷新。
        initial_color = Design.LED_IDLE
        self._last_icon_color = initial_color
        self._led_last_total = None
        image = self.create_icon(initial_color)
        self.icon = pystray.Icon(
            "ccBar",
            image,
            L("ccBar - 正在加载数据…"),
            pystray.Menu(*self.build_menu())
        )

        # 统计库连接、首次回填、备份、周报与首轮同步全部转后台
        threading.Thread(target=self._bootstrap, daemon=True,
                         name="ccbar-bootstrap").start()
        # 启动更新线程
        update_thread = threading.Thread(target=self.update_icon, daemon=True)
        update_thread.start()

        # 运行
        self.icon.run(setup=self._on_icon_ready)

    def _on_icon_ready(self, icon=None):
        """pystray 事件循环就绪后的回调（独立线程）：补一次托盘刷新并显示图标。

        _bootstrap 可能在 icon.run() 之前就跑完，那时设置 menu/title 还是空操作，
        初始菜单会停留在"正在加载"占位。

        注意：run(setup=...) 一旦传入自定义 setup，pystray 就不再执行默认的
        "把图标设为可见"逻辑，必须自己置 visible=True，否则 Shell_NotifyIcon
        根本不会注册——表现为进程在跑、托盘消息窗口也在，但通知区域没有图标。
        """
        self._refresh_tray()
        if self.icon:
            try:
                self.icon.visible = True
            except Exception as e:
                logger.warning("tray show failed: %s", e)

    def _bootstrap(self):
        """启动后台维护：连库 + 回填 + 备份 + 周报 + 首轮同步（不阻塞托盘出现）"""
        try:
            self.connect_store()
        except Exception as e:
            logger.warning("store init failed: %s", e)
        # 连接完成后立即刷新一次托盘，别让"未找到数据源"停留到下一个刷新周期
        self._refresh_tray()

        try:
            self.maybe_auto_backup()
            self.maybe_generate_weekly_report()
        except Exception as e:
            logger.warning("startup maintenance failed: %s", e)
        self.sync_trae(interactive=False)
        self.sync_workbuddy(interactive=False)
        self._refresh_tray()

    def _refresh_tray(self):
        """刷新托盘菜单与标题（可从任意线程调用；pystray 的 menu setter 会更新原生菜单）"""
        if not self.icon:
            return
        try:
            self.icon.menu = pystray.Menu(*self.build_menu())
            self.icon.title = self.get_tooltip_text()
        except Exception as e:
            logger.warning("tray refresh failed: %s", e)

    # ------------------------------------------------------------ 窗口单例
    def _window_registry(self):
        """窗口注册表（测试绕过 __init__ 时也能懒初始化）"""
        reg = getattr(self, "_windows", None)
        if reg is None:
            reg = self._windows = {}
        return reg

    def _reuse_window(self, key):
        """同名单例窗口仍活着就提到最前并返回 True（连点两次不再开出两份）"""
        entry = self._window_registry().get(key)
        if not entry:
            return False
        if entry.get("pending"):
            return True   # 正在后台准备，等它建出来即可
        win = entry.get("win")
        try:
            if win is None or not win.winfo_exists():
                self._window_registry().pop(key, None)
                return False
            self._bring_to_front(win)
            on_reuse = entry.get("on_reuse")
            if on_reuse:
                on_reuse()
            return True
        except Exception:
            self._window_registry().pop(key, None)
            return False

    def _register_window(self, key, win, on_reuse=None):
        self._window_registry()[key] = {"win": win, "on_reuse": on_reuse}

if __name__ == "__main__":
    app = CcBarTray()
    app.run()