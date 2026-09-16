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

    # 字体
    FONT_UI = ("Microsoft YaHei UI", 10)
    FONT_UI_SMALL = ("Microsoft YaHei UI", 9)
    FONT_UI_TINY = ("Microsoft YaHei UI", 8)
    FONT_MONO = ("Consolas", 10)
    FONT_MONO_SMALL = ("Consolas", 9)
    FONT_MONO_TINY = ("Consolas", 8)
    FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
    FONT_BIG = ("Consolas", 22, "bold")

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

    def show_hourly_detail(self, days_ago=0, date_str=None):
        """显示每小时详情窗口"""
        import tkinter as tk

        root = tk.Tk()
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

        canvas_scroll.bind_all("<MouseWheel>", on_mousewheel)

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
        root.mainloop()

    def show_weekly_detail(self, icon=None, item=None):
        """显示近7天详情"""
        self.show_daily_detail(days=7, title="近7天用量")

    def show_monthly_detail(self, icon=None, item=None):
        """显示近30天详情"""
        self.show_daily_detail(days=30, title="近30天用量")

    def show_model_detail(self, icon=None, item=None):
        """显示模型分布详情（带环形图）"""
        import tkinter as tk

        root = tk.Tk()
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

        canvas_scroll.bind_all("<MouseWheel>", on_mousewheel)

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

            # 环形图（取前 6 个模型）
            top_models = models[:6]
            items = []
            for idx, m in enumerate(top_models):
                color = Design.MODEL_COLORS[idx % len(Design.MODEL_COLORS)]
                items.append((m["total_token"], color, m["model"]))

            ChartCanvas.draw_donut(donut_canvas, items, 160)

            # 图例
            for idx, m in enumerate(top_models):
                color = Design.MODEL_COLORS[idx % len(Design.MODEL_COLORS)]
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
                color = (Design.MODEL_COLORS[idx % len(Design.MODEL_COLORS)]
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
        root.mainloop()

    def show_daily_detail(self, days=7, title="近7天用量"):
        """显示每日详情窗口（7天/30天）"""
        import tkinter as tk

        root = tk.Tk()
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

        canvas_scroll.bind_all("<MouseWheel>", on_mousewheel)

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
        """显示设置窗口（统一深色风格）"""
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Tk()
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