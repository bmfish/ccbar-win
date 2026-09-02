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
            "db_path": DB_PATH
        }
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
    
    def save_settings(self):
        """保存设置"""
        config_dir = Path.home() / ".ccbar"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "settings.txt"
        
        with open(config_file, "w") as f:
            f.write(f"refresh_interval={self.settings['refresh_interval']}\n")
            f.write(f"db_path={self.settings['db_path']}\n")
    
    def create_icon(self, text="⚡"):
        """创建托盘图标"""
        # 创建简单图标
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
        """查询今日统计"""
        db_path = self.settings["db_path"]
        if not os.path.exists(db_path):
            return None
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            if days == 0:
                # 今日
                date_str = datetime.now().strftime("%Y-%m-%d")
                cursor.execute("""
                    SELECT 
                        COUNT(*) as reqs,
                        COALESCE(SUM(input_tokens), 0) as input,
                        COALESCE(SUM(output_tokens), 0) as output,
                        COALESCE(SUM(cache_creation_input_tokens), 0) as cache_create,
                        COALESCE(SUM(cache_read_input_tokens), 0) as cache_read
                    FROM proxy_request_logs
                    WHERE date(created_at) = date(?)
                """, (date_str,))
            else:
                # 近 N 天
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cursor.execute("""
                    SELECT 
                        COUNT(*) as reqs,
                        COALESCE(SUM(input_tokens), 0) as input,
                        COALESCE(SUM(output_tokens), 0) as output,
                        COALESCE(SUM(cache_creation_input_tokens), 0) as cache_create,
                        COALESCE(SUM(cache_read_input_tokens), 0) as cache_read
                    FROM proxy_request_logs
                    WHERE date(created_at) >= date(?)
                """, (start_date,))
            
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
        # 今日数据
        today = self.query_day_stats(0)
        if not today:
            return "未找到数据源"
        
        total_str = self.fmt_tokens(today["total"])
        return f"⚡ {total_str}"
    
    def build_menu(self):
        """构建菜单"""
        # 获取数据
        today = self.query_day_stats(0)
        week = self.query_day_stats(7)
        month = self.query_day_stats(30)
        
        menu_items = []
        
        # 标题
        menu_items.append(pystray.MenuItem("ccSwitch 用量", None, enabled=False))
        menu_items.append(pystray.Menu.SEPARATOR)
        
        # 今日数据
        if today:
            today_str = self.fmt_tokens(today["total"])
            menu_items.append(pystray.MenuItem(f"今日: {today_str}", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  请求: {today['reqs']}次", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  输入: {self.fmt_tokens(today['input'])}", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  输出: {self.fmt_tokens(today['output'])}", None, enabled=False))
        else:
            menu_items.append(pystray.MenuItem("今日: 无数据", None, enabled=False))
        
        menu_items.append(pystray.Menu.SEPARATOR)
        
        # 近7天
        if week:
            week_str = self.fmt_tokens(week["total"])
            menu_items.append(pystray.MenuItem(f"近7天: {week_str}", None, enabled=False))
        
        # 近30天
        if month:
            month_str = self.fmt_tokens(month["total"])
            menu_items.append(pystray.MenuItem(f"近30天: {month_str}", None, enabled=False))
        
        menu_items.append(pystray.Menu.SEPARATOR)
        
        # 设置
        menu_items.append(pystray.MenuItem("设置", self.show_settings))
        menu_items.append(pystray.MenuItem("退出", self.quit_app))
        
        return menu_items
    
    def show_settings(self, icon, item):
        """显示设置窗口"""
        import tkinter as tk
        from tkinter import ttk, filedialog
        
        root = tk.Tk()
        root.title("ccBar 设置")
        root.geometry("400x250")
        
        # 刷新间隔
        tk.Label(root, text="刷新间隔(秒):").pack(pady=5)
        interval_var = tk.StringVar(value=str(self.settings["refresh_interval"]))
        interval_entry = tk.Entry(root, textvariable=interval_var, width=10)
        interval_entry.pack()
        
        # 数据库路径
        tk.Label(root, text="数据库路径:").pack(pady=5)
        path_var = tk.StringVar(value=self.settings["db_path"])
        path_entry = tk.Entry(root, textvariable=path_var, width=40)
        path_entry.pack()
        
        def browse():
            filename = filedialog.askopenfilename(
                title="选择数据库文件",
                filetypes=[("SQLite", "*.db"), ("All", "*.*")]
            )
            if filename:
                path_var.set(filename)
        
        tk.Button(root, text="浏览", command=browse).pack(pady=5)
        
        def save():
            try:
                self.settings["refresh_interval"] = int(interval_var.get())
                self.settings["db_path"] = path_var.get()
                self.save_settings()
                root.destroy()
            except ValueError:
                tk.messagebox.showerror("错误", "间隔必须是数字")
        
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
                # 更新菜单
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
