"""界面语言（中文 / English / 跟随系统）——与 macOS 版 L10n.swift 同款机制。

key = 中文原文（代码里现存的字符串），value = 英文；缺词条回落中文，
允许渐进式翻译：新增界面先写中文，再来这里补英文。

实现说明：macOS 版把语言偏好写进 UserDefaults、重启后生效；Windows 版由
app_settings 的 app_language 键决定（"system" 时看系统界面语言），
set_language() 可在运行时切换，设置页保存后立即重画即可。
"""
import os

# 语言偏好取值（与 macOS 版 AppLanguage 一致）
LANGUAGES = ("system", "zh", "en")


def system_language():
    """系统界面语言 → "zh" / "en"（判不出来按中文，与 macOS 版默认一致）"""
    # Windows：GetUserDefaultUILanguage 的 LANGID 低 10 位，0x09 = 英文，0x04 = 中文
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        if langid == 0x09:
            return "en"
        if langid == 0x04:
            return "zh"
    except Exception:
        pass
    # 非 Windows / 取不到时退化为环境变量判断
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value.lower().startswith("en"):
            return "en"
    return "zh"


def resolves_to_english(pref):
    """语言偏好 → 是否走英文界面"""
    if pref == "en":
        return True
    if pref == "zh":
        return False
    return system_language() == "en"


def set_language(pref):
    """设置当前界面语言（不落盘，落盘由 app_settings 负责）"""
    global _english
    _english = resolves_to_english(pref)
    return _english


def is_english():
    return _english


def L(zh):
    """取词条：中文原文 → 英文；缺词条回落中文"""
    if _english:
        return L10N_EN.get(zh, zh)
    return zh


def language_display_name(pref):
    """设置页下拉里显示的语言名（跟随系统按当前语言显示）"""
    if pref == "zh":
        return "中文"
    if pref == "en":
        return "English"
    return L("跟随系统")


def format_tokens(n):
    """Token 数量：中文用 万/亿，英文用 K/M/B（与 macOS 版 L10n.formatTokens 逐字一致）"""
    n = int(n)
    if _english:
        if n >= 1_000_000_000:
            return "%.2fB" % (n / 1_000_000_000)
        if n >= 1_000_000:
            return "%.2fM" % (n / 1_000_000)
        if n >= 10_000:
            return "%.1fK" % (n / 1_000)
        return str(n)
    if n >= 100_000_000:
        return "%.2f亿" % (n / 100_000_000)
    if n >= 10_000:
        return "%d万" % (n // 10_000)
    return str(n)


# 当前是否英文界面（set_language 维护）
_english = False


# MARK: - 英文词条表（284 条，从 macOS 版 L10n.swift 逐条搬移）

L10N_EN = {
    "\n恢复方式：退出 ccBar 后用备份文件替换\n~/Library/Application Support/ccbar/ccbar.db": "\nTo restore: quit ccBar and replace\n~/Library/Application Support/ccbar/ccbar.db with the backup",
    "\n数据源分布:\n": "\nBy Source:\n",
    "\n昨日：%@": "\nYesterday: %@",
    "\n模型分布:\n": "\nBy Model:\n",
    "\n请求数：%d": "\nRequests: %d",
    " · 估算 ": " · est. ",
    " · 已用预算 ": " · budget used ",
    " · 积分 %@/%@": " · credits %@/%@",
    "AI 用量周报": "AI Usage Weekly",
    "AI 用量周报（上周）": "AI Usage Weekly (last week)",
    "AI 用量战报": "AI Usage Report",
    "ATTACH 失败（库可能被占用或损坏）": "Attach failed (locked or corrupted)",
    "Git commit -m '又一个 Bug' 🔧": "git commit -m 'yet another bug' 🔧",
    "Token 总量: %@\n": "Total Tokens: %@\n",
    "Top1 占比": "Top 1 Share",
    "ccBar 今日用量统计\n": "ccBar Today's Usage\n",
    "ccBar 用量统计": "ccBar Usage",
    "ccBar 设置": "ccBar Settings",
    "ccbar-按月汇总.csv": "ccbar-by-month.csv",
    "ccbar-近30天.csv": "ccbar-30days.csv",
    "ccbar-近7天.csv": "ccbar-7days.csv",
    "「从」模型的所有明细行会并入「到」模型，操作不可撤销": "All rows of the source model will be merged into the target. This cannot be undone.",
    "万": "×10K",
    "上周周报已生成": "Last week's report is ready",
    "上次自动备份：%@": "Last auto-backup: %@",
    "不是 ccBar 导出的明细 CSV（表头不符）": "Not a ccBar detail CSV (header mismatch)",
    "不是有效的 ccBar 主题包": "Not a valid ccBar theme pack",
    "不是有效的 ccBar 统计库（缺 usage_log 表或文件打不开）": "Not a valid ccBar stats DB (usage_log missing or unreadable)",
    "个": "sessions",
    "主题「%@」已删除": "Theme \"%@\" deleted",
    "主题「%@」已加入可选列表": "Theme \"%@\" added to the list",
    "主题「%@」已覆盖更新": "Theme \"%@\" updated",
    "主题包已保存到：": "Theme pack saved to:",
    "主题风格": "Theme",
    "产品经理说很简单 🤡": "\"It's simple,\" said the PM 🤡",
    "今天": "Today",
    "今天不出 Bug，明天出什么 🎯": "No bugs today? Tomorrow then 🎯",
    "今天也是充满 Bug 的一天 🐛": "Another day full of bugs 🐛",
    "今天也要加油写 Bug 哦 ✨": "Keep shipping bugs ✨",
    "今天的需求明天再做 🌙": "Today's ticket, tomorrow's problem 🌙",
    "今日 Token 已达 %@（每%d万通知一次）": "Today's tokens reached %@ (every %d×10K)",
    "今日 Token 用量已达 %@，超过预警阈值 %d万": "Today's tokens reached %@ (warning line: %d×10K)",
    "今日会话": "Sessions Today",
    "今日各渠道": "Today by channel",
    "今日暂无请求": "No requests today",
    "今日暂无逐时数据": "No hourly data today",
    "今日消耗": "Today's burn",
    "今日用量": "Today",
    "今日积分": "Today",
    "今日详情": "Today Detail",
    "今日费用": "Today",
    "今日：%@": "Today: %@",
    "从": "From",
    "代码如诗，Bug 如风 🌸": "Code is poetry, bugs are wind 🌸",
    "代码能跑就行 🏃": "It compiles, ship it 🏃",
    "以下字段无效：": "Invalid fields:",
    "以后再说": "Later",
    "使用量最大的模型（近 30 天）": "Top Model (30d)",
    "保存": "Save",
    "保存为图片": "Save PNG",
    "先实现，再优化（永远不优化）⏳": "Make it work, optimize never ⏳",
    "全部模型": "All Models",
    "全部渠道": "All Channels",
    "共读取 %d 行 · 新增 %d 行 · 跳过 %d 行（重复或非法）": "Read %d rows · %d new · %d skipped (dup/invalid)",
    "共读取 %d 行 · 新增 %d 行（重复自动跳过）": "Read %d rows · %d new (duplicates skipped)",
    "写代码不如谈恋爱 💕": "Touch grass > touch keyboard 💕",
    "写代码使我快乐（并不）🎭": "Coding sparks joy (no) 🎭",
    "分享": "Share",
    "删除": "Delete",
    "删除成功": "Deleted",
    "刷新": "Refresh",
    "刷新间隔": "Refresh Interval",
    "刷新间隔（5 ~ 3000 秒）": "Refresh interval (5 – 3000 s)",
    "前往下载": "Download",
    "剩余 ": "Left ",
    "单日峰值（近 30 天）": "Peak Day (30d)",
    "单次刷新增量达到变红，0=不变红": "icon turns red at this refresh delta, 0=off",
    "历史总量": "All Time",
    "去干活吧，流水会记住每一笔 💪": "Go build something — the ledger remembers 💪",
    "发现新版本 v%@": "New version v%@",
    "取消": "Cancel",
    "合并": "Merge",
    "合并到": "Merge Into",
    "合并完成": "Merge Complete",
    "合并数据库…": "Merge Database…",
    "合计": "Total",
    "启用用量预警": "Enable usage warning",
    "周消耗": "Week Burn",
    "命中率": "Hit Rate",
    "回到今天": "Today",
    "在浏览器登录 trae.cn 后，从 DevTools 的 Cookie 里复制 sessionid 的值": "Sign in at trae.cn in your browser, then copy the sessionid cookie value from DevTools",
    "备份失败": "Backup failed",
    "备份完成": "Backup complete",
    "备份数据": "Backup",
    "复制": "Copy",
    "复制今日统计": "Copy Today's Stats",
    "复制到剪贴板": "Copy Image",
    "多机合并（导入另一台机器的统计库）": "Multi-machine Merge (import another Mac's stats DB)",
    "大小写、厂商前缀不同的同名模型都已一致": "Same-name models differing by case or vendor prefix are already consistent",
    "天": "days",
    "好的": "OK",
    "实测 ": "measured ",
    "宽版弹窗（380pt）": "Wide popover (380pt)",
    "导入": "Import",
    "导入 CSV": "Import CSV",
    "导入失败": "Import failed",
    "导入完成": "Import complete",
    "导入成功": "Imported",
    "导出": "Export",
    "导出 CSV": "Export CSV",
    "导出失败": "Export failed",
    "导出完成": "Export complete",
    "导出长图": "Export Full Image",
    "展开全部 %d 个模型": "Show all %d models",
    "峰值": "Peak",
    "工时": "Work Hours",
    "已合并 %d 组 · 改写 %d 行明细": "Merged %d groups · rewrote %d rows",
    "已填写（保存后生效）": "Filled (applied on save)",
    "已复制到剪贴板": "Copied to clipboard",
    "已存到 CCBar 周报目录，点击打开洞察中心查看": "Saved to the weekly folder — tap to open Insights",
    "已改写 %d 行明细": "Rewrote %d rows",
    "已用 / 共": "used of",
    "已经是最新版本（v%@）": "Up to date (v%@)",
    "已超预算 ": "Over budget by ",
    "已连接": "Connected",
    "平均 %d 分钟 · 最长 %d 分钟": "avg %d min · longest %d min",
    "应用": "Apps",
    "开机自动启动": "Launch at Login",
    "当前版本 v%@。前往 GitHub Releases 下载最新 DMG。": "Current v%@. Get the latest DMG from GitHub Releases.",
    "性价比榜（近 30 天）": "Value Ranking (30d)",
    "总 Token": "Tokens",
    "手动合并…": "Merge manually…",
    "手动合并模型": "Merge Models Manually",
    "打不开（被占用或损坏）": "Can't open (locked or corrupted)",
    "打开周报目录": "Open Weekly Folder",
    "打开备份目录": "Open Backup Folder",
    "打开面板": "Open Panel",
    "技术债也是债 💸": "Tech debt is still debt 💸",
    "按当前速率 · 本月已用 %@": "at current rate · %@ so far",
    "按当前速率到 24:00 约 %@": "At current rate, ~%@ by 24:00",
    "按当前速率预计 ": "Projected at current pace: ",
    "按月汇总": "By Month",
    "接入 Trae 并产生用量后展示积分消耗": "Credit usage shows up once Trae is connected and used",
    "收起": "Collapse",
    "数据源": "Data Sources",
    "数据迁移（明细 CSV）": "Data Migration (detail CSV)",
    "整理模型": "Tidy Models",
    "文件不存在": "File not found",
    "新的设置将在下次刷新时生效": "New settings take effect on next refresh",
    "无法保存": "Can't save",
    "日均": "Daily Avg",
    "日均用量（近 30 天）": "Daily Avg (30d)",
    "日期": "Date",
    "日预算": "Daily budget",
    "时间": "Time",
    "明细已导出到：": "Exported to:",
    "星期": "Weekday",
    "星期分布（近 90 天）": "By Weekday (90d)",
    "昨天": "Yesterday",
    "昨日": "Yesterday",
    "晒用量就是最好的宣传 ✨": "Flexing your usage is the best ad ✨",
    "暂无数据": "No data",
    "暂无数据\n请检查设置里的数据源连接": "No data yet\nCheck data sources in Settings",
    "最佳月": "Best Month",
    "最活跃时段（近 30 天）": "Peak Hours (30d)",
    "月份": "Month",
    "月均": "Monthly Avg",
    "月度预算": "Monthly Budget",
    "月度预算（≥ 0 的数字，$，0=关闭）": "Monthly budget (≥ 0, $, 0=off)",
    "月费用预算，0=关闭；费用页显示进度与预算线": "Monthly cost budget, 0=off; shows progress bar & budget line on Cost page",
    "未启用": "Disabled",
    "未找到": "No data",
    "未知": "Unknown",
    "未计费渠道（ZCode 等）按此单价折算，0=关闭": "Unmetered channels (ZCode etc.) priced at this rate, 0=off",
    "未连接": "Not connected",
    "未配置登录": "Not signed in",
    "本周用量": "This Week",
    "本月已花 ": "Spent ",
    "本月预算 $%.2f": "Monthly Budget $%.2f",
    "构成": "Component",
    "标题 30 秒快照 vs 弹窗实时查": "title 30s snapshot vs live query",
    "校验失败": "Validation failed",
    "检查失败，稍后再试，或直接到 GitHub Releases 页面查看": "Check failed, try again later or visit GitHub Releases",
    "检查更新": "Check Updates",
    "模型": "Model",
    "模型分布": "Models",
    "模型分布详情": "Model Breakdown",
    "模型数": "Models",
    "模型编年史": "Model Chronicle",
    "模型费用排行（近 30 天）": "Cost by model (30 days)",
    "次": "reqs",
    "每周一自动生成到周报目录 🗓": "Auto-generated every Monday 🗓",
    "每周一自动生成用量周报": "Auto-generate weekly report",
    "每天一个 key": "one key per day",
    "每小时用量": "Hourly Usage",
    "每累计N万通知，0=关闭": "notify per N×10K, 0=off",
    "没有需要合并的模型": "Nothing to merge",
    "洞察": "Insights",
    "洞察中心": "Insights",
    "流水": "Timeline",
    "测试？什么测试？ 🎲": "Tests? What tests? 🎲",
    "浏览": "Browse",
    "渠道": "Channels",
    "用量预警": "Usage Warning",
    "界面语言": "Language",
    "登录已过期，请重新提供 sessionid": "Session expired, paste a fresh sessionid",
    "码农的一天从咖啡开始 ☕": "Dev day starts with coffee ☕",
    "确定": "OK",
    "秒": "s",
    "积分": "credits",
    "积分为 Trae 官方计费口径；历史数据自接入起最多回溯 90 天": "Credits follow Trae's official billing; history goes back up to 90 days",
    "积分余额（官方账单）": "Credit Balance (official)",
    "筛选": "Filter",
    "粘贴 trae.cn 登录后的 sessionid cookie 值": "paste sessionid cookie from trae.cn",
    "粘贴后将在保存后生效": "Applied after save",
    "累计": "Total",
    "红色门槛": "Red Line",
    "红色门槛（≥ 0 的整数，万，0=不变红）": "Red line (≥ 0, ×10K, 0=off)",
    "线上出 Bug 了？不可能 🚫": "Prod bug? Impossible 🚫",
    "统计库已备份到：": "Database backed up to:",
    "统计库未打开或目标位置不可写": "Database not open or target not writable",
    "统计数据已复制，可直接粘贴使用": "Stats copied, ready to paste",
    "缓存 Token: %@": "Cache: %@",
    "缓存创建": "Cache Create",
    "缓存命中": "Cache Hit",
    "缓存命中率（近 30 天）": "Cache Hit Rate (30d)",
    "缓存读": "Cache Read",
    "缺少必需表": "Required tables missing",
    "自动合并同名模型": "Auto-merge same-name models",
    "菜单栏动画伴侣（小猫随用量跑动）": "Menu bar pet (cat runs with usage)",
    "菜单栏表情分级（🙂→🥵）": "Menu bar emoji tiers (🙂→🥵)",
    "表结构正确（保存后生效）": "Schema OK (applied on save)",
    "设置": "Settings",
    "设置已保存": "Settings saved",
    "该日暂无请求": "No requests on this day",
    "请求": "Reqs",
    "请求数": "Requests",
    "请求数量: %d\n": "Requests: %d\n",
    "费用": "Cost",
    "费用/积分": "Cost/Credits",
    "费用按 cc-switch 记录的单价折算；ZCode 渠道官方未计费，不计入": "Cost uses cc-switch pricing; ZCode is unmetered and not included",
    "费用按 cc-switch 记录的单价折算；未计费渠道可在设置里配默认单价估算": "Cost uses cc-switch pricing; unmetered channels can be estimated via default price in Settings",
    "超出时通知": "notify when exceeded",
    "趋势": "Trend",
    "跟随系统": "System",
    "输入": "Input",
    "输入 Token: %@\n": "Input: %@\n",
    "输出": "Output",
    "输出 Token: %@\n": "Output: %@\n",
    "近 30 天": "30 Days",
    "近 30 天 Token 构成": "Token Mix — last 30 days",
    "近 30 天应用分布（堆叠）": "By App — last 30 days (stacked)",
    "近 30 天渠道用量（堆叠）": "By channel — last 30 days (stacked)",
    "近 30 天积分走势": "Credits — last 30 days",
    "近 30 天费用走势": "Cost — last 30 days",
    "近 7 天": "7 Days",
    "近 90 天用量热力图": "90-Day Heatmap",
    "近30天": "30 Days",
    "近30天用量": "Last 30 Days",
    "近7天": "7 Days",
    "近7天用量": "Last 7 Days",
    "近7天详情": "7-Day Detail",
    "这个功能很简单的 🎪": "This feature is simple 🎪",
    "这个接口我三分钟就写完 ⚡": "Three-minute API ⚡",
    "这个需求一天就能做完 📝": "One-day task, for sure 📝",
    "连接中…": "Connecting…",
    "连续使用": "Streak",
    "退出": "Quit",
    "选择 %@ 数据库文件": "Choose %@ database file",
    "选择另一台机器的 ccbar.db（统计库），明细将按主键去重合并": "Choose another Mac's ccbar.db; rows are deduplicated by primary key",
    "通知间隔": "Notify Every",
    "通知间隔（≥ 0 的整数，万，0=关闭）": "Notify interval (≥ 0, ×10K, 0=off)",
    "重启后生效": "restart to apply",
    "重命名": "Rename",
    "重命名主题": "Rename Theme",
    "重构？先加个 if 吧 🤔": "Refactor? Just add an if 🤔",
    "重置": "Reset",
    "需求又改了，习惯就好 🫠": "Specs changed again, classic 🫠",
    "预警阈值": "Warn Threshold",
    "预警阈值（正整数，万）": "Warning threshold (positive, ×10K)",
    "预计本月消耗": "Projected This Month",
    "默认单价": "Default Price",
    "默认单价（≥ 0 的数字，$/M tokens，0=关闭）": "Default price (≥ 0, $/M tokens, 0=off)",
    "🎉 用量里程碑": "🎉 Usage Milestone",
}


# MARK: - Windows 端补充词条（macOS 的 L10n.swift 里没有这些文案）
#
# 设置页新项、周报目录、多机合并、主题包管理、托盘菜单、详情窗口等 Windows 独有界面，
# 以及为模板化 f-string 预备的整串词条。同样遵循"缺词条回落中文"，允许后续继续补。
L10N_EN.update({
    "外观": "Appearance",
    "提醒": "Alerts",
    "数据": "Data",
    "行为": "Behavior",
    "费用估算": "Cost estimate",
    "刷新间隔 (秒):": "Refresh interval (s):",
    "范围 5 - 3000": "range 5 - 3000",
    "预警阈值 (万):": "Warning threshold (x10k):",
    "超过此值将弹出通知提醒": "Notify when exceeded",
    "通知间隔 (万):": "Milestone interval (x10k):",
    "每累计N万通知一次，0=关闭": "Notify every N x10k, 0=off",
    "红色门槛 (万):": "Red LED delta (x10k):",
    "单次刷新增量达到即变红，0=不变红": "Turn red on this per-refresh delta, 0=off",
    "月度预算 ($):": "Monthly budget ($):",
    "0=关闭预算显示": "0=hide budget",
    "默认单价 ($/M):": "Default price ($/M):",
    "未计费渠道按此估算，0=关闭": "Estimate unmetered channels, 0=off",
    "（当前按 $%g/M tokens 估算）": "(estimated at $%g/M tokens)",
    "上次自动备份：%s": "Last auto backup: %s",
    "从未": "never",
    "多机合并（另一台机器的统计库）": "Merge another machine's stats DB",
    "明细 CSV 迁移（导入幂等：主键去重）": "Detail CSV migration (idempotent by primary key)",
    "主题包读取失败：%s": "Failed to read theme pack: %s",
    "新名称：": "New name:",
    "主题「%s」已加入可选列表（保存后生效）": "Theme \\\"%s\\\" added (takes effect after saving)",
    "内置主题不能重命名": "Built-in themes cannot be renamed",
    "内置主题不能删除": "Built-in themes cannot be deleted",
    "当前版本不支持多机合并": "This version cannot merge databases",
    "模型不足两个，无需合并": "Fewer than two models, nothing to merge",
    "设置未保存": "Settings not saved",
    "请检查：\\n· ": "Please check:\\n· ",
    "设置已保存，将在下次刷新时生效": "Settings saved; they take effect on the next refresh",
    "保存失败": "Save failed",
    "保存完成": "Saved",
    "选择数据库文件": "Choose database file",
    "选择另一台机器的 ccbar.db": "Choose another machine's ccbar.db",
    "不是有效的 ccBar 统计库（缺 usage_log 表或打不开）": "Not a valid ccBar stats DB (usage_log missing or unreadable)",
    "共读取 %d 行 · 新增 %d 行（主键去重）": "Read %d rows · added %d (deduplicated by primary key)",
    "统计库已备份到：\\n": "Stats DB backed up to:\\n",
    "\\n\\n恢复方式：退出 ccBar 后用备份文件替换\\n~/.ccbar/ccbar.db": "\\n\\nTo restore: quit ccBar and replace\\n~/.ccbar/ccbar.db with the backup",
    "备份统计库": "Back up stats DB",
    "已导出 %d 行到：\\n%s": "Exported %d rows to:\\n%s",
    "导出明细": "Export details",
    "导入明细": "Import details",
    "导出流水": "Export timeline",
    "导出洞察长图": "Export insights image",
    "导出主题包": "Export theme pack",
    "导入主题包": "Import theme pack",
    "导出成功": "Exported",
    "已导出到：\\n%s": "Exported to:\\n%s",
    "已保存到：\\n%s": "Saved to:\\n%s",
    "ccBar 主题包": "ccBar theme pack",
    "选择日期": "Choose date",
    "输入日期（YYYY-MM-DD）：": "Enter date (YYYY-MM-DD):",
    "请按 YYYY-MM-DD 输入，例如 2026-08-01": "Use YYYY-MM-DD, e.g. 2026-08-01",
    "日期格式不对": "Invalid date format",
    "📊 今日用量": "📊 Today",
    "📊 今日: ": "📊 Today: ",
    "📅 昨日: ": "📅 Yesterday: ",
    "📅 近7天: ": "📅 7 days: ",
    "📆 近30天: ": "📆 30 days: ",
    "  🔢 请求: ": "  🔢 Requests: ",
    "  💾 缓存命中: ": "  💾 Cache hit: ",
    "  ⏱️ 时长: ": "  ⏱️ Duration: ",
    "📊 今日暂无数据": "📊 No data today",
    "未找到数据源": "No data source",
    "🌶️ 未找到数据源，请去设置": "🌶️ No data source, open Settings",
    "去设置": "Open settings",
    "ccBar - 未找到数据": "ccBar - no data",
    "🤖 模型分布": "🤖 Models",
    "📈 洞察中心": "📈 Insights",
    "💾 备份数据": "💾 Back up data",
    "⬇️ 检查更新": "⬇️ Check for updates",
    "⚙️ 设置": "⚙️ Settings",
    "🔄 刷新": "🔄 Refresh",
    "❌ 退出": "❌ Quit",
    "🔥 连续使用": "🔥 Streak",
    "时长": "Duration",
    "用量": "Usage",
    "总token": "Total tokens",
    "%d 天": "%d days",
    "%s 积分": "%s credits",
    "%d 个 · 平均 %d 分钟 · 最长 %d 分钟": "%d sessions · avg %d min · longest %d min",
    "请求数量: ": "Requests: ",
    "Token 总量: ": "Total tokens: ",
    "输入 Token: ": "Input: ",
    "输出 Token: ": "Output: ",
    "统计数据已复制到剪贴板": "Stats copied to clipboard",
    "已复制": "Copied",
    "复制失败": "Copy failed",
    "剪贴板不可用，已保存到临时文件：\\n%s": "Clipboard unavailable; saved to a temp file:\\n%s",
    "🫧 里程碑": "🫧 Milestone",
    " tokens！今日已达 ": " tokens! Today reached ",
    "万通知一次）": "0k per notification)",
    "今日 Token 用量已达 ": "Today's token usage reached ",
    "，超过预警阈值 ": ", above the warning threshold ",
    "发现新版本": "Update available",
    "最新版本 v": "Latest v",
    "，当前 v": ", current v",
    "\\n是否前往下载？": "\\nOpen the download page?",
    "已经是最新版本（v": "Already up to date (v",
    "每小时用量详情": "Hourly usage",
    "渠道（近 30 天）": "Channels (30 days)",
    "日预算 ": "Daily budget ",
    "本月已花": "Spent this month",
    "实测 %s · 估算 %s": "Measured %s · estimated %s",
    "按当前速率 · 本月已用 ": "At current rate · used this month ",
    "按当前速率预计 %s · 已用预算 %.0f%%": "Projected %s at current rate · %.0f%% of budget",
    "‹ 前一天": "‹ Prev day",
    "后一天 ›": "Next day ›",
    "整理模型 ▾": "Tidy models ▾",
    "成功": "Success",
    "提示": "Notice",
    "周报生成失败:": "Weekly report failed: ",
    "Trae 同步失败:": "Trae sync failed: ",
    "通知发送失败:": "Notification failed: ",
    "开机自启设置失败:": "Setting autostart failed: ",
    "UI 回调异常: ": "UI callback error: ",
    "GUI 线程未就绪，忽略 UI 请求": "GUI thread not ready; UI request ignored",
    "周一": "Mon",
    "周二": "Tue",
    "周三": "Wed",
    "周四": "Thu",
    "周五": "Fri",
    "周六": "Sat",
    "周日": "Sun",
    "%d月%d日": "%d/%d",
    "合并失败": "Merge failed",
    "默认主题": "Default",
    "卡哇伊 01": "Kawaii 01",
    "海蓝": "Ocean",
    "翠绿": "Forest",
    "星空紫": "Purple",
    "CRT 终端": "CRT Terminal",
    "已导出 %d 行明细到：\\n%s": "Exported %d rows to:\\n%s",
    "%d时": "%02d:00",
    "按当前速率到 24:00 约 %s": "At this rate, ~%s by 24:00",
    "已超预算 $%s": "Over budget by $%s",
    "剩余 $%s": "Remaining $%s",
    "已用预算 %.0f%%": "%.0f%% of budget used",
    "日均 %.0f 分钟 · 最长 %.0f 分钟": "avg %.0f min · longest %.0f min",
    "重开窗口后生效": "Applies to newly opened windows",
})

# MARK: - 批次 4（i18n 全覆盖）补充：把 main.py 里新包裹的字面量补齐英文

# 问候语原文表（原文即词条 key）。main.py 的 CcBarTray.GREETINGS 直接引用它，
# 在显示时才过 L()，避免模块导入期就把语言定死成中文。
GREETINGS = (
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
)

L10N_EN.update({
    "  ⏱️ 时长: %sh": "  ⏱️ Time: %sh",
    "  💾 缓存命中: %.1f%%": "  💾 Cache hit: %.1f%%",
    "  🔢 请求: %d次": "  🔢 Requests: %d",
    " · 积分 %s/%s": " · credits %s/%s",
    "%d 个": "%d sessions",
    "%d次": "%d reqs",
    "+%s tokens！今日已达 %s（每%d万通知一次）":
        "+%s tokens! %s today (every %d×10K)",
    "Token 总量: %s\n": "Total Tokens: %s\n",
    "ccbar-模型分布.csv": "ccbar-model-breakdown.csv",
    "ccbar-每小时.csv": "ccbar-hourly.csv",
    "今日 Token 用量已达 %s，超过预警阈值 %d万":
        "Today's tokens reached %s (warning line: %d×10K)",
    "今日会话  %d 个 · 平均 %d 分钟 · 最长 %d 分钟":
        "Sessions today  %d · avg %d min · longest %d min",
    "剪贴板不可用，已保存到临时文件：\n%s":
        "Clipboard unavailable; saved to a temp file:\n%s",
    "导出%s": "Export %s",
    "导出历史总量": "Export All Time",
    "导出模型分布": "Export Model Breakdown",
    "导出每小时用量": "Export Hourly Usage",
    "峰值时段": "Peak Hour",
    "已保存到：\n%s": "Saved to:\n%s",
    "已导出 %d 行到：\n%s": "Exported %d rows to:\n%s",
    "已导出 %d 行明细到：\n%s": "Exported %d rows to:\n%s",
    "已导出到：\n%s": "Exported to:\n%s",
    "已用 / 共 %s": "used of %s",
    "已经是最新版本（v%s）": "Up to date (v%s)",
    "总Token": "Tokens",
    "最新版本 v%s，当前 v%s\n是否前往下载？":
        "Latest v%s, current v%s\nDownload now?",
    "统计库已备份到：\n%s\n\n恢复方式：退出 ccBar 后用备份文件替换\n~/.ccbar/ccbar.db":
        "Database backed up to:\n%s\n\nTo restore: quit ccBar and replace\n"
        "~/.ccbar/ccbar.db with the backup",
    "请检查：\n· ": "Check:\n· ",
    "输入 Token: %s\n": "Input: %s\n",
    "输出 Token: %s\n": "Output: %s\n",
    "📅 昨日: %s": "📅 Yesterday: %s",
    "📅 近7天: %s": "📅 7 days: %s",
    "📆 近30天: %s": "📆 30 days: %s",
    "📈 历史总量: %s": "📈 All time: %s",
    "📊 今日: %s": "📊 Today: %s",
})

L10N_EN.update({
    "今日：%s": "Today: %s",
    "昨日：%s": "Yesterday: %s",
    "请求数：%d": "Requests: %d",
    "宽版弹窗（380pt）": "Wide popover (380pt)",
    "ccBar - 未找到数据": "ccBar - no data",
})
