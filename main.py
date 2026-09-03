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
        "今天的咖啡比昨天的 Bug 多 ☕",
        "代码写得好，不如注释写得好 📖",
        "程序员的浪漫是写个脚本自动化 💌",
        "这个 Bug 是 feature 🎉",
        "今天不想上班，但 Bug 不会自己修 🥲",
        "全世界都在用我的代码（没人知道）🌍",
        "键盘敲得响，工资涨得慢 🎹",
        "代码写得好，头发掉得早 💇",
        "需求文档是什么？能吃吗 🍕",
        "今天又是 Ctrl+C、Ctrl+V 的一天 📋",
        "写代码就像谈恋爱，总有想分手的时候 💔",
        "别催了，代码在编译 ⏳",
        "这个需求很简单，改一下就好了 🔨",
        "今天写了一天代码，全删了重新来 🔄",
        "代码没Bug，是用户不会用 🤷",
        "能跑就行，别管性能 🏎️",
        "先上线再说，出了问题再修 🚀",
        "我写的代码没有 Bug，只有惊喜 🎁",
        "这个重构只需要五分钟 ⏱️",
        "今天把注释全删了，代码更清晰了 🧹",
        "写代码时觉得自己是天才，调试时觉得自己是白痴 🤯",
        "最好的代码是没有代码 🧘",
        "我的代码像诗，但没人能读懂 📜",
        "世界上只有两种代码：能跑的和不能跑的 🎭",
        "这个Bug我本地复现不了啊 🤔",
        "今天优化了1%的性能，花了3天 📊",
        "写代码就是在创造Bug的路上修复Bug 🔄",
        "今天的工作量：改了一个空格 ✏️",
        "需求变更使我快乐（bushi）😈",
        "程序员的日常：看文档→写代码→删代码→看文档 🔁",
        "今天终于把Hello World写出来了 🌍",
        "这代码谁写的？——我写的 😶",
        "技术选型？哪个星星多用哪个 ⭐",
        "写代码最大的敌人是产品经理 👿",
        "代码写完不测，测完不写 🎯",
        "今天也是勤劳搬砖的一天 🧱",
        "代码写得好，年终拿得少 🎄",
        "今天下班了，Bug 明天再说 🌆",
        "写代码就像打游戏，总有一关过不去 🎮",
        "为什么我的代码总是有 Bug？因为我在写代码 🧐",
        "今天写了一行代码，改了十行注释 📝",
        "代码写得好，不如 PPT 做得好 🎨",
        "程序员不需要休息，程序员需要咖啡 ☕",
        "这个需求太简单了，简单到我不会做 🫣",
        "今天又在 Stack Overflow 上抄代码了 📚",
        "代码如人生，总有 return 的一天 🏁",
        "写代码就像呼吸，停不下来（也不想去医院）💨",
        "今天学会了三个框架，忘了一个 🧠",
        "代码写完了，需求又改了 🔀",
        "这个功能我上周就做完了（才怪）🤥",
        "写代码是最好的冥想方式 🧘‍♂️",
        "今天的心态：代码能跑就是胜利 🏆",
        "产品经理又来了，假装不在 👀",
        "今天把项目从零重构到零 🔄",
        "这个需求不合理——合理的是不存在的 🌈",
        "代码是最好的文档（没人写文档）📄",
        "写代码使我年轻（头发除外）👶",
        "今天又学了一个新框架，明天就过时了 📦",
        "代码review的时候才发现自己写的什么 🕵️",
        "这个Bug已经三天了，可能要变成feature 🫠",
        "今天的工作成果：把报错信息改得更好看了 🎨",
        "写代码就像写日记，只不过没人看 📓",
        "今天终于理解了递归——等等，我又忘了 🤯",
        "代码写得好，客户跑不了 🏃‍♂️",
        "这个接口对接了三天，原来是URL错了 🔗",
        "程序员的快乐：终于编译通过了 ✅",
        "今天提交了20次，全是fix typo 📤",
        "写代码就像搭积木，最后一块总是放不下 🧩",
        "代码能跑就行——来自五年前的我 🕰️",
        "今天学会了设计模式，全忘了 🎓",
        "这个需求可以做，但不是现在 📅",
        "写代码就像种树，十年后才能乘凉 🌳",
        "代码是最好的艺术（自认为）🎨",
        "今天的心态：随便写写算了 🤷‍♂️",
        "产品经理说不急——意思是现在就要 🏃‍♀️",
        "写代码使我快乐，Debug 使我成长 💪",
        "这个Bug太诡异了，我决定不管它了 👻",
        "今天终于把测试用例写全了——原来全挂了 ❌",
        "代码写完了，但是没人知道怎么用 🗺️",
        "写代码就像做菜，放多了盐就回不去了 🍳",
        "今天的工作：复制粘贴了一整天 📦",
        "这个需求很简单——我不知道怎么做 🤷‍♀️",
        "代码是最好的沟通方式（和机器）🤖",
        "写代码使我富有（精神上）💎",
        "今天又加班了，为了一个分号 🌙",
        "这个Bug找了两小时，少了个括号 😤",
        "代码写得好，不如会汇报 📊",
        "今天的心态：明天再说 🌛",
        "写代码就像爬山，总有一个坑等着你 ⛰️",
        "今天终于搞定了——但又出了新Bug 🎊",
        "代码是最好的娱乐方式（被迫的）🎪",
        "写代码使我快乐——才怪呢 🙃",
        "这个需求已经改了八遍了，再来一遍 🔂",
        "今天的工作量：回复了30条消息 💬",
        "代码是最好的老师——教你怎么犯错 🎓",
        "写代码就像谈恋爱，总有想放弃的时候 😢",
        "今天终于理解了闭包——又忘了 🔄",
        "这个Bug是别人写的——但代码是我提交的 🤫",
        "代码写完了，终于可以下班了——又来了新需求 🌃",
        "写代码使我年轻——但我看起来很老 🧓",
        "今天的工作：把一个Bug变成三个Bug 🎲",
        "代码是最好的保险——但出Bug不赔 🛡️",
        "写代码就像写小说，总有一个转折点 📖",
        "今天终于把代码写完了——明天重写 📝",
        "这个需求太棒了——但我做不到 👏",
        "代码是最好的朋友——永远不会背叛你（但会报错）🤝",
        "写代码使我快乐——只要不出Bug 😌",
        "今天的工作：写了一行代码，删了十行 🗑️",
        "代码是最好的音乐——编译的声音很悦耳 🎵",
        "写代码就像打怪升级，Bug是Boss 🐉",
        "今天终于把性能优化了——但功能全坏了 🏎️",
        "这个Bug太经典了——经典到不想修 📚",
        "代码写完了——但我不敢提交 😰",
        "写代码使我快乐——当它能跑的时候 🥳",
        "今天的工作：创建了一个新Bug 🐞",
        "代码是最好的故事——但没人想听 📖",
        "写代码就像做实验——总有意想不到的结果 🧪",
        "今天终于把Bug修了——但引入了三个新的 🎭",
        "这个需求很简单——只是我不知道怎么做 🤦",
        "代码是最好的解压方式——如果不出Bug的话 🧘‍♀️",
        "写代码使我快乐——在它能跑之前 😅",
        "今天的工作：删除了500行代码 ✂️",
        "代码是最好的艺术——但观众只有编译器 🎨",
        "写代码就像写诗——但没人欣赏 📝",
        "今天终于把文档写完了——代码还没写 📄",
        "这个Bug找了三天——原来在另一个文件里 📁",
        "代码写完了——但我不记得我写了什么 🤔",
        "写代码使我快乐——直到遇到段错误 💀",
        "今天的工作：把TODO注释全删了 ✔️",
        "代码是最好的投资——回报率未知 📈",
        "写代码就像下棋——一步错步步错 ♟️",
        "今天终于把代码review完了——全是问题 🔍",
        "这个需求很简单——简单到我做不了 🙈",
        "代码是最好的语言——机器的语言 🤖",
        "写代码使我快乐——如果有人帮我Debug的话 😇",
        "今天的工作：修复了一个去年的Bug 🗓️",
        "代码是最好的教育——教你怎么犯错 🎓",
        "写代码就像烹饪——有时会炸厨房 💥",
        "今天终于把部署搞好了——生产环境炸了 💣",
        "这个Bug太深了——深到我选择躺平 🛏️",
        "代码写完了——但编译器不同意 ❌",
        "写代码使我快乐——当测试通过的时候 🎊",
        "今天的工作：学会了新的报错方式 🚨",
        "代码是最好的哲学——存在的意义是什么 🌌",
        "写代码就像打怪——但经验值不涨 👾",
        "今天终于把代码写完了——明天开会讨论 📅",
        "这个需求已经很成熟了——但我还没开始做 🌱",
        "代码写完了——但我不确定能不能跑 🎰",
        "写代码使我快乐——直到看到生产日志 📋",
        "今天的工作：把一行代码改成了五行 📝",
        "代码是最好的学习方式——错误是最好的老师 📖",
        "写代码就像写论文——永远写不完 📜",
        "今天终于把Bug定位了——但不知道怎么修 🎯",
        "这个需求改了需求文档——但代码没改 📄",
        "代码写完了——但我不满意 🤷",
        "写代码使我快乐——在客户打电话之前 📞",
        "今天的工作：写了一个永远不会执行的分支 🌿",
        "代码是最好的挑战——每天都是第一天 🏁",
        "写代码就像做数学题——总有不会做的 ➗",
        "今天终于把架构设计好了——但实现不了 🏗️",
        "这个Bug我修不了——但我会假装在修 🎭",
        "代码写完了——但性能太差了 🐢",
        "写代码使我快乐——当有人star我的项目时 ⭐",
        "今天的工作：把简单的问题复杂化了 🎪",
        "代码是最好的乐趣——当它工作的时候 🎠",
        "写代码就像做手工——但用的是键盘 🎹",
        "今天终于把代码优化了——但更慢了 📉",
        "这个需求很紧急——但我还没吃午饭 🍔",
        "代码写完了——但跑不起来 🏃",
        "写代码使我快乐——直到看到遗留代码 😱",
        "今天的工作：把代码写成了天书 📖",
        "代码是最好的创造——但Bug是最好的破坏 🏚️",
        "写代码就像做体操——总有一个动作做不到 🤸",
        "今天终于把Bug修了——但测试用例全挂了 🧪",
        "这个需求很简单——说的人不是我 😏",
        "代码写完了——但部署文档还没写 📋",
        "写代码使我快乐——在Code Review之前 🙈",
        "今天的工作：把一行代码变成了一个类 🏛️",
        "代码是最好的实践——但实践证明不行 ⚒️",
        "写代码就像做侦探——总要找到那个Bug 🔎",
        "今天终于把功能做完了——但UI不好看 🎨",
        "这个需求已经讨论了一周——但还没结论 🗣️",
        "代码写完了——但我不敢看Code Review的结果 👀",
        "写代码使我快乐——当CI/CD通过的时候 ✅",
        "今天的工作：把一个函数改成了十个函数 🔟",
        "代码是最好的发明——但Bug是最好的学习机会 💡",
        "写代码就像做园丁——总要修剪代码 🌿",
        "今天终于把Bug定位了——原来是缓存问题 📦",
        "这个需求很紧急——但Bug更紧急 🚑",
        "代码写完了——但没有写测试 🧪",
        "写代码使我快乐——当代码通过review的时候 🎉",
        "今天的工作：把代码从V1升级到V2 🔼",
        "代码是最好的艺术——编译器是最严格的评论家 🎭",
        "写代码就像做建筑师——代码是砖头 🧱",
        "今天终于把性能优化了——但增加了复杂性 🧩",
        "这个需求已经很多次了——但这次一定行 🤞",
        "代码写完了——但要重构 🔁",
        "写代码使我快乐——在Deadline之前 ⏰",
        "今天的工作：把代码从Python重写成了Python 🐍",
        "代码是最好的学习——每天都在学新东西 🎓",
        "写代码就像做实验——总要试很多次 🔄",
        "今天终于把Bug修了——但代码更乱了 🌀",
        "这个需求很简单——但我得先学一下 📚",
        "代码写完了——但我有更好的想法了 💡",
        "写代码使我快乐——当它第一次就跑对的时候 🌟",
    ]

    def __init__(self):
        self.icon = None
        self.settings = {
            "refresh_interval": 60,
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

        import random
        # 随机问候语（同 Mac 版）
        greeting = random.choice(self.GREETINGS)
        menu_items.append(pystray.MenuItem(greeting[:16], None, enabled=False))
        menu_items.append(pystray.Menu.SEPARATOR)

        # 今日数据
        if today:
            # 检查预警
            self.check_warning(today)

            today_str = self.fmt_tokens(today["total"])
            menu_items.append(pystray.MenuItem(f"📊 今日: {today_str}", None, enabled=False))

            menu_items.append(pystray.MenuItem(f"  🔢 请求: {today['reqs']}次", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  📥 输入: {self.fmt_tokens(today['input'])}", None, enabled=False))
            menu_items.append(pystray.MenuItem(f"  📤 输出: {self.fmt_tokens(today['output'])}", None, enabled=False))

            # 工作时长
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
            menu_items.append(pystray.MenuItem(f"📅 昨日: {self.fmt_tokens(yesterday['total'])}", None, enabled=False))

        # 近7天
        if week:
            menu_items.append(pystray.MenuItem(f"📅 近7天: {self.fmt_tokens(week['total'])}", None, enabled=False))

        # 近30天
        if month:
            menu_items.append(pystray.MenuItem(f"📆 近30天: {self.fmt_tokens(month['total'])}", None, enabled=False))

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
        """更新图标和标题"""
        while True:
            time.sleep(self.settings["refresh_interval"])
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
