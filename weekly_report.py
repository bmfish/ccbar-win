"""周报：每周一自动生成"上一个完整周"的用量周报 PNG。

与 macOS 版 WeeklyReport 同款语义：
  - 报告周 = 上一个完整周（上周一 ~ 上周日）；
  - 文件名日期 = 报告周之后那个周一（= 本周一），一周内任何一天补生成都是同一个文件；
  - 幂等靠"文件已存在即跳过"——没有调度状态，周一没开机下次启动自动补；
  - 该周无用量不记账，下次再试。

这里只放纯逻辑（日期窗口 / 路径 / 幂等 / 通知时机），PNG 渲染与系统通知由调用方
注入，因此本模块不依赖 tkinter/PIL，可以直接单测。
"""
import os
from datetime import datetime, timedelta

# 周报输出目录（在"文档"下，与 macOS 版同名同结构）
DIR_NAME = "CCBar 周报"


def last_week_window(now=None):
    """上一个完整周的 days_ago 窗口 (from, to)，均含、不含今日实时。

    from > to：from 是上周一（days_ago 更大），to 是上周日。
    例：今天周一 → (7, 1)；今天周日 → (13, 7)。
    """
    now = now or datetime.now()
    days_since_monday = now.weekday()          # Monday=0 … Sunday=6
    return (days_since_monday + 7, days_since_monday + 1)


def report_key(now=None):
    """报告标识（也是文件名里的日期）= 报告周之后那个周一"""
    now = now or datetime.now()
    _, to = last_week_window(now)
    return (now - timedelta(days=to - 1)).strftime("%Y-%m-%d")


def file_name(now=None):
    return "ccbar-weekly-%s.png" % report_key(now)


def directory(documents_dir=None):
    """周报目录 ~/Documents/CCBar 周报/（测试可传 documents_dir 覆盖）"""
    base = documents_dir or os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(base, DIR_NAME)


_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def short_date(day, english=False):
    """medium 风格日期：中文 2026年8月1日 / 英文 Aug 1, 2026"""
    if english:
        return "%s %d, %d" % (_MONTHS_EN[day.month - 1], day.day, day.year)
    return "%d年%d月%d日" % (day.year, day.month, day.day)


def date_range_text(now=None, english=False):
    """周报卡上的日期区间："起始 ~ 结束"（均含）"""
    now = now or datetime.now()
    frm, to = last_week_window(now)
    start = now - timedelta(days=frm)
    end = now - timedelta(days=to)
    return "%s ~ %s" % (short_date(start, english), short_date(end, english))


def trend_labels(now=None):
    """报告周 7 天的横轴标签（MM-dd 形式，与 macOS 版 date.suffix(5) 一致）"""
    now = now or datetime.now()
    frm, to = last_week_window(now)
    days = [now - timedelta(days=d) for d in range(frm, to - 1, -1)]
    return [d.strftime("%m-%d") for d in days]


def generate_if_needed(store, render_card, output_dir=None, notify=None,
                       now=None, auto=True, english=False):
    """生成"上一个完整周"的周报；返回写出的文件路径，跳过/失败返回 None。

    store   ：需要 query_window_stats(days_ago_from, days_ago_to) 和
              query_daily_tokens_between(days_ago_from, days_ago_to)
    render_card：render_card(date_text, total, reqs, peak, trend) → PNG bytes 或 None
    notify  ：notify(title, body)，仅在真正写出文件后调用一次
    auto    ：设置里的"自动生成周报"开关
    """
    if not auto:
        return None
    now = now or datetime.now()
    out_dir = output_dir or directory()
    path = os.path.join(out_dir, file_name(now))
    # 幂等：文件已存在就跳过（删掉文件可在下次启动重新生成）
    if os.path.exists(path):
        return None

    frm, to = last_week_window(now)
    stats = store.query_window_stats(frm, to)
    total = int((stats or {}).get("total", 0) or 0)
    if total <= 0:
        return None       # 该周没有用量：不记账，下次刷新再试

    daily = store.query_daily_tokens_between(frm, to) or []
    peak = max((t for _, t in daily), default=0)
    labels = trend_labels(now)
    trend = [(labels[i] if i < len(labels) else d, t) for i, (d, t) in enumerate(daily)]

    png = render_card(date_range_text(now, english), total,
                      int(stats.get("reqs", 0) or 0), peak, trend)
    if not png:
        return None

    os.makedirs(out_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)

    if notify:
        try:
            notify("上周周报已生成", "已存到 CCBar 周报目录，点击打开洞察中心查看")
        except Exception:
            pass      # 通知失败不影响周报本身
    return path
