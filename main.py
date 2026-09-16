import sys
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
import random

# Windows 系统托盘
import pystray
from PIL import Image, ImageDraw
import win10toast

# 数据库路径
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")

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
        self.load_settings()

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

    def create_icon(self):
        """加载闪电图标"""
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccBar.ico")
        if os.path.exists(ico_path):
            return Image.open(ico_path)
        # fallback
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pts = [(35, 63), (21, 35), (31, 35), (28, 7), (42, 35), (31, 35)]
        draw.polygon(pts, fill=(255, 110, 180, 255))
        return img

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
            menu_items.append(pystray.MenuItem("🤖 模型分布", None, enabled=False))
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

    def show_hourly_detail(self, days_ago=0, date_str=None):
        """显示每小时详情窗口"""
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("每小时用量详情")
        root.geometry("500x400")
        root.configure(bg='#1a1a19')

        # 计算日期
        if date_str:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            target_date = datetime.now() - timedelta(days=days_ago)

        day_text = "今日" if days_ago == 0 else "昨日"

        # 导航栏
        nav_frame = tk.Frame(root, bg='#1a1a19')
        nav_frame.pack(fill=tk.X, padx=16, pady=8)

        def prev_day():
            nonlocal target_date, days_ago
            target_date -= timedelta(days=1)
            days_ago = (datetime.now() - target_date).days
            refresh_hourly()

        def next_day():
            nonlocal target_date, days_ago
            if target_date < datetime.now() - timedelta(days=1):
                target_date += timedelta(days=1)
                days_ago = (datetime.now() - target_date).days
                refresh_hourly()

        tk.Button(nav_frame, text="◀", command=prev_day, bg='#333', fg='white', width=3).pack(side=tk.LEFT)

        date_label = tk.Label(nav_frame, text=target_date.strftime("%y-%m-%d"), font=("Consolas", 12, "bold"), fg='white', bg='#1a1a19')
        date_label.pack(side=tk.LEFT, expand=True)

        tk.Button(nav_frame, text="▶", command=next_day, bg='#333', fg='white', width=3).pack(side=tk.RIGHT)

        # 内容区域
        content_frame = tk.Frame(root, bg='#1a1a19')
        content_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        # 标题行
        header_frame = tk.Frame(content_frame, bg='#1a1a19')
        header_frame.pack(fill=tk.X)
        tk.Label(header_frame, text="时间", width=6, anchor='w', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="请求数", width=8, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="总token", width=10, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="缓存读", width=10, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)

        # 分隔线
        tk.Frame(content_frame, bg='#444', height=1).pack(fill=tk.X, pady=2)

        # 数据区域
        data_frame = tk.Frame(content_frame, bg='#1a1a19')
        data_frame.pack(fill=tk.BOTH, expand=True)

        def refresh_hourly():
            for widget in data_frame.winfo_children():
                widget.destroy()

            date_label.config(text=target_date.strftime("%y-%m-%d"))

            hourly_data = self.query_hourly_stats(days_ago)
            if not hourly_data:
                tk.Label(data_frame, text="暂无数据", fg='#666', bg='#1a1a19').pack(pady=20)
                return

            # 找有数据的范围
            hours_with_data = [h for h, d in hourly_data.items() if d["reqs"] > 0]
            if not hours_with_data:
                tk.Label(data_frame, text="暂无数据", fg='#666', bg='#1a1a19').pack(pady=20)
                return

            start_hour = min(hours_with_data)
            end_hour = max(hours_with_data)

            # 合计
            total_reqs = sum(d["reqs"] for d in hourly_data.values())
            total_token = sum(d["output"] + d["input"] + d["cache_read"] for d in hourly_data.values())
            total_cache = sum(d["cache_read"] for d in hourly_data.values())

            # 合计行
            total_frame = tk.Frame(data_frame, bg='#1a1a19')
            total_frame.pack(fill=tk.X)
            tk.Label(total_frame, text="合计", width=6, anchor='w', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=f"{total_reqs}次", width=8, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=self.fmt_tokens(total_token), width=10, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=self.fmt_tokens(total_cache), width=10, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

            tk.Frame(data_frame, bg='#444', height=1).pack(fill=tk.X, pady=2)

            # 每小时数据
            for hour in range(start_hour, end_hour + 1):
                data = hourly_data.get(hour, {"reqs": 0, "output": 0, "input": 0, "cache_read": 0})
                total_token_hour = data["output"] + data["input"] + data["cache_read"]

                row_frame = tk.Frame(data_frame, bg='#1a1a19')
                row_frame.pack(fill=tk.X)
                tk.Label(row_frame, text=f"{hour}时", width=6, anchor='w', fg='#64b5f6', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=f"{data['reqs']}次" if data['reqs'] > 0 else "-", width=8, anchor='e', fg='white' if data['reqs'] > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=self.fmt_tokens(total_token_hour), width=10, anchor='e', fg='white' if total_token_hour > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=self.fmt_tokens(data['cache_read']), width=10, anchor='e', fg='#ccc' if data['cache_read'] > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)

        refresh_hourly()
        root.mainloop()

    def show_weekly_detail(self, icon=None, item=None):
        """显示近7天详情"""
        self.show_daily_detail(days=7, title="近7天用量")

    def show_monthly_detail(self, icon=None, item=None):
        """显示近30天详情"""
        self.show_daily_detail(days=30, title="近30天用量")

    def show_daily_detail(self, days=7, title="近7天用量"):
        """显示每日详情窗口"""
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(title)
        root.geometry("550x450")
        root.configure(bg='#1a1a19')

        # 当前周/月开始日期
        current_date = datetime.now()

        # 导航栏
        nav_frame = tk.Frame(root, bg='#1a1a19')
        nav_frame.pack(fill=tk.X, padx=16, pady=8)

        def prev_period():
            nonlocal current_date
            if days == 7:
                current_date -= timedelta(days=7)
            else:
                # 上个月
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
                # 下个月
                if current_date.month == 12:
                    next_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    next_date = current_date.replace(month=current_date.month + 1)
                if next_date <= datetime.now():
                    current_date = next_date
            refresh_daily()

        tk.Button(nav_frame, text="◀", command=prev_period, bg='#333', fg='white', width=3).pack(side=tk.LEFT)

        date_label = tk.Label(nav_frame, text="", font=("Consolas", 12, "bold"), fg='white', bg='#1a1a19')
        date_label.pack(side=tk.LEFT, expand=True)

        tk.Button(nav_frame, text="▶", command=next_period, bg='#333', fg='white', width=3).pack(side=tk.RIGHT)

        # 内容区域
        content_frame = tk.Frame(root, bg='#1a1a19')
        content_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        # 标题行
        header_frame = tk.Frame(content_frame, bg='#1a1a19')
        header_frame.pack(fill=tk.X)
        tk.Label(header_frame, text="日期", width=8, anchor='w', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="请求数", width=8, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="总token", width=10, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(header_frame, text="缓存读", width=10, anchor='e', fg='#999', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)

        tk.Frame(content_frame, bg='#444', height=1).pack(fill=tk.X, pady=2)

        # 数据区域
        data_frame = tk.Frame(content_frame, bg='#1a1a19')
        data_frame.pack(fill=tk.BOTH, expand=True)

        def refresh_daily():
            for widget in data_frame.winfo_children():
                widget.destroy()

            if days == 7:
                # 计算本周一开始
                weekday = current_date.weekday()
                week_start = current_date - timedelta(days=weekday)
                week_end = week_start + timedelta(days=6)
                start_date = week_start.strftime("%Y-%m-%d")
                end_date = week_end.strftime("%Y-%m-%d")
                date_label.config(text=f"{week_start.strftime('%y-%m-%d')} ~ {week_end.strftime('%y-%m-%d')}")
            else:
                # 本月
                month_start = current_date.replace(day=1)
                if current_date.month == 12:
                    month_end = current_date.replace(year=current_date.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    month_end = current_date.replace(month=current_date.month + 1, day=1) - timedelta(days=1)
                start_date = month_start.strftime("%Y-%m-%d")
                end_date = min(month_end, datetime.now()).strftime("%Y-%m-%d")
                date_label.config(text=current_date.strftime("%y-%m"))

            daily_data = self.query_daily_stats_for_range(start_date, end_date)
            if not daily_data:
                tk.Label(data_frame, text="暂无数据", fg='#666', bg='#1a1a19').pack(pady=20)
                return

            # 按日期排序
            sorted_dates = sorted(daily_data.keys())

            # 合计
            total_reqs = sum(d["reqs"] for d in daily_data.values())
            total_token = sum(d["output"] + d["input"] + d["cache_read"] for d in daily_data.values())
            total_cache = sum(d["cache_read"] for d in daily_data.values())

            # 合计行
            total_frame = tk.Frame(data_frame, bg='#1a1a19')
            total_frame.pack(fill=tk.X)
            tk.Label(total_frame, text="合计", width=8, anchor='w', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=f"{total_reqs}次", width=8, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=self.fmt_tokens(total_token), width=10, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(total_frame, text=self.fmt_tokens(total_cache), width=10, anchor='e', fg='white', bg='#1a1a19', font=("Consolas", 10, "bold")).pack(side=tk.LEFT)

            tk.Frame(data_frame, bg='#444', height=1).pack(fill=tk.X, pady=2)

            # 每日数据
            for date_str in sorted_dates:
                data = daily_data[date_str]
                total_token_day = data["output"] + data["input"] + data["cache_read"]

                # 日期格式转换
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                display_date = date_obj.strftime("%m/%d")

                row_frame = tk.Frame(data_frame, bg='#1a1a19')
                row_frame.pack(fill=tk.X)
                tk.Label(row_frame, text=display_date, width=8, anchor='w', fg='#64b5f6', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=f"{data['reqs']}次" if data['reqs'] > 0 else "-", width=8, anchor='e', fg='white' if data['reqs'] > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=self.fmt_tokens(total_token_day), width=10, anchor='e', fg='white' if total_token_day > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)
                tk.Label(row_frame, text=self.fmt_tokens(data['cache_read']), width=10, anchor='e', fg='#ccc' if data['cache_read'] > 0 else '#666', bg='#1a1a19', font=("Consolas", 10)).pack(side=tk.LEFT)

        refresh_daily()
        root.mainloop()

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

    def show_settings(self, icon=None, item=None):
        """显示设置窗口"""
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox

        root = tk.Tk()
        root.title("ccBar 设置")
        root.geometry("500x400")

        tk.Label(root, text="刷新间隔 (秒):").pack(pady=5)
        interval_var = tk.StringVar(value=str(self.settings["refresh_interval"]))
        interval_entry = tk.Entry(root, textvariable=interval_var, width=10)
        interval_entry.pack()

        tk.Label(root, text="数据库路径:").pack(pady=5)
        path_var = tk.StringVar(value=self.settings["db_path"])
        path_entry = tk.Entry(root, textvariable=path_var, width=50)
        path_entry.pack()
        tk.Label(root, text="默认: ~/.cc-switch/cc-switch.db", fg="gray").pack()

        def browse():
            filename = filedialog.askopenfilename(
                title="选择数据库文件",
                filetypes=[("SQLite", "*.db"), ("All", "*.*")]
            )
            if filename:
                path_var.set(filename)

        tk.Button(root, text="浏览", command=browse).pack(pady=5)

        tk.Label(root, text="预警阈值 (万):").pack(pady=5)
        warning_var = tk.StringVar(value=str(self.settings["warning_threshold"]))
        warning_entry = tk.Entry(root, textvariable=warning_var, width=10)
        warning_entry.pack()
        tk.Label(root, text="超过此值将弹出通知提醒", fg="gray").pack()

        warning_enabled_var = tk.BooleanVar(value=self.settings["warning_enabled"])
        tk.Checkbutton(root, text="启用用量预警", variable=warning_enabled_var).pack(pady=5)

        def save():
            try:
                self.settings["refresh_interval"] = int(interval_var.get())
                self.settings["db_path"] = path_var.get()
                self.settings["warning_threshold"] = int(warning_var.get())
                self.settings["warning_enabled"] = warning_enabled_var.get()
                self.save_settings()
                messagebox.showinfo("成功", "设置已保存")
                root.destroy()
            except ValueError:
                messagebox.showerror("错误", "请输入有效的数字")

        tk.Button(root, text="保存", command=save).pack(pady=10)

        root.mainloop()

    def quit_app(self, icon, item):
        """退出应用"""
        icon.stop()

    def update_icon(self):
        """更新图标和标题"""
        while True:
            time.sleep(self.settings["refresh_interval"])
            # 检查定时备份
            self.check_and_run_scheduled_backup()
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
        # 初始化历史备份表
        self.init_history_table()

        # 启动时执行一次备份
        self.backup_history()

        # 创建图标
        image = self.create_icon()

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