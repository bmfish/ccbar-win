import sys
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# Windows 系统托盘
import pystray
from PIL import Image, ImageDraw
import win10toast

# 数据库路径
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")

class CcBarTray:
    def __init__(self):
        self.icon = None
        self.settings = {
            "refresh_interval": 30,
            "db_path": DB_PATH,
            "warning_threshold": 50,  # 万为单位
            "warning_enabled": True,
        }
        self.last_notification_date = None
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

    def create_icon(self):
        """创建托盘图标"""
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 紫色渐变背景
        for i in range(64):
            r = int(255 * (1 - i/64) + 157 * (i/64))
            g = int(110 * (1 - i/64) + 78 * (i/64))
            b = int(180 * (1 - i/64) + 237 * (i/64))
            draw.line([(0, i), (63, i)], fill=(r, g, b, 255))

        # 圆角矩形
        draw.rounded_rectangle([(4, 4), (60, 60)], radius=12, fill=(255, 110, 180, 200))

        return img

    def query_day_stats(self, days=0):
        """查询统计"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # 使用本地时间的今天开始时间
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
                # 近N天
                start_date = start_of_day - timedelta(days=days)
                cursor.execute("""
                    SELECT
                        COUNT(*) as reqs,
                        COALESCE(SUM(input_tokens), 0) as input,
                        COALESCE(SUM(output_tokens), 0) as output,
                        COALESCE(SUM(cache_creation_tokens), 0) as cache_create,
                        COALESCE(SUM(cache_read_tokens), 0) as cache_read
                    FROM proxy_request_logs
                    WHERE created_at >= ?
                """, (int(start_date.timestamp()),))

            row = cursor.fetchone()
            conn.close()

            if row:
                return {
                    "reqs": row[0],
                    "input": row[1],
                    "output": row[2],
                    "cache_create": row[3],
                    "cache_read": row[4],
                    "total": row[1] + row[2] + row[3] + row[4]
                }
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

            # 使用本地时间的今天开始时间
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

            # 使用本地时间的今天开始时间
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

        # 检查今天是否已经通知过
        today_key = f"warning_{datetime.now().strftime('%Y-%m-%d')}"
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        notified_file = config_dir / "notified.txt"

        if notified_file.exists():
            with open(notified_file, "r") as f:
                if today_key in f.read():
                    return  # 今天已通知过

        # 检查是否超过阈值（万转换为tokens）
        threshold_tokens = self.settings["warning_threshold"] * 10000
        if stats["total"] >= threshold_tokens:
            # 发送通知
            try:
                toaster = win10toast.ToastNotifier()
                toaster.show_toast(
                    "用量预警",
                    f"今日 Token 用量已达 {self.fmt_tokens(stats['total'])}，超过预警阈值 {self.settings['warning_threshold']}万",
                    duration=10
                )
            except:
                pass

            # 标记今天已通知
            with open(notified_file, "a") as f:
                f.write(f"{today_key}\n")

    def fmt_tokens(self, tokens):
        """格式化 token 数量"""
        if tokens >= 100000000:  # 1亿
            return f"{tokens/100000000:.4f}亿"
        elif tokens >= 10000:  # 1万
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

        # 标题
        menu_items.append(pystray.MenuItem("📊 ccSwitch 用量", None, enabled=False))
        menu_items.append(pystray.Menu.SEPARATOR)

        # 今日数据
        if today:
            # 检查预警
            self.check_warning(today)

            today_str = self.fmt_tokens(today["total"])
            menu_items.append(pystray.MenuItem(f"📊 今日 Token: {today_str}", None, enabled=False))

            # 对比昨日
            if yesterday and yesterday["total"] > 0:
                change = (today["total"] - yesterday["total"]) / yesterday["total"] * 100
                emoji = "📈" if change >= 0 else "📉"
                menu_items.append(pystray.MenuItem(f"  {emoji} 较昨日 {change:+.1f}%", None, enabled=False))

            menu_items.append(pystray.MenuItem(f"  🔢 请求: {today['reqs']}次", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  📥 输入: {self.fmt_tokens(today['input'])}", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  📤 输出: {self.fmt_tokens(today['output'])}", None, enabled=False))

            # 工作时长
            if work_hours:
                menu_items.append(pystray.MenuItem(f"  ⏱️ 工作时长: {work_hours}h", None, enabled=False))
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
                model_name = m["model"][:20] + "..." if len(m["model"]) > 20 else m["model"]
                menu_items.append(pystray.MenuItem(f"  {model_name}: {self.fmt_tokens(m['total'])}", None, enabled=False))
            menu_items.append(pystray.Menu.SEPARATOR)

        # 近7天
        if week:
            menu_items.append(pystray.MenuItem(f"📅 近7天 Token: {self.fmt_tokens(week['total'])}", None, enabled=False))

        # 近30天
        if month:
            menu_items.append(pystray.MenuItem(f"📆 近30天 Token: {self.fmt_tokens(month['total'])}", None, enabled=False))

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
            # 显示提示
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

        # 刷新间隔
        tk.Label(root, text="刷新间隔 (秒):").pack(pady=5)
        interval_var = tk.StringVar(value=str(self.settings["refresh_interval"]))
        interval_entry = tk.Entry(root, textvariable=interval_var, width=10)
        interval_entry.pack()

        # 数据库路径
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

        # 预警阈值
        tk.Label(root, text="预警阈值 (万):").pack(pady=5)
        warning_var = tk.StringVar(value=str(self.settings["warning_threshold"]))
        warning_entry = tk.Entry(root, textvariable=warning_var, width=10)
        warning_entry.pack()
        tk.Label(root, text="超过此值将弹出通知提醒", fg="gray").pack()

        # 启用预警
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
        """更新图标"""
        while True:
            time.sleep(self.settings["refresh_interval"])
            if self.icon:
                menu = pystray.Menu(*self.build_menu())
                self.icon.menu = menu

    def run(self):
        """运行应用"""
        # 创建图标
        image = self.create_icon()

        # 初始菜单
        menu = pystray.Menu(*self.build_menu())

        # 创建托盘图标
        self.icon = pystray.Icon(
            "ccBar",
            image,
            "ccBar - 点击查看用量",
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
