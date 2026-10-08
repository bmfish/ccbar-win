"""分享卡渲染：今日战报卡 + 用量周报卡（Pillow 绘制，二维码用自带的 qr 模块）。

与 macOS 版 UsageShareCard 同款版式：品牌行 + 大数字 + 7 天趋势 + 三列统计 +
页脚，右下角嵌 GitHub 二维码（mac 用 CoreImage，这边用纯 Python 编码器）。

渲染是纯函数（给定数据出图），不碰 tkinter，方便单测与周报落盘复用。
"""
import os

import l10n
import themes
import qr

# 卡片底色与描边（mac 版 cardFill 0.07/cardBorder 0.10 叠在深底上的等效色）
CARD_BG = "#121214"
CARD_BORDER = "#3D3D3D"
MUTED = "#8C8C8C"
FAINT = "#555555"
QR_URL = "https://github.com/bmfish/ccbar-win"

# 中文字体候选：Windows（真机）优先，其余是给非 Windows 本机预览/测试兜底的
_FONTS_BOLD = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/arialbd.ttf",
               "/System/Library/Fonts/PingFang.ttc",
               "/System/Library/Fonts/STHeiti Medium.ttc",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
_FONTS = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf",
          "/System/Library/Fonts/PingFang.ttc",
          "/System/Library/Fonts/Hiragino Sans GB.ttc",
          "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]


def pil_font(size, bold=False):
    """PIL 中文字体：微软雅黑优先，逐级回落（都找不到才用默认位图字体）"""
    from PIL import ImageFont
    for path in (_FONTS_BOLD if bold else []) + _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fmt_tokens(n):
    return l10n.format_tokens(int(n or 0))


def _qr_image(url, size):
    """二维码图（白底 + 深色模块），失败返回 None（卡片少个角标也比崩掉好）"""
    from PIL import Image
    try:
        modules = qr.matrix_size(url, "M")
        border = 2
        # 按模块数选放大倍数并缩到目标尺寸，保证二维码铺满白框且不模糊
        scale = max(1, int(round(float(size) / (modules + 2 * border))))
        img = qr.to_png(url, scale=scale, border=border, ec="M",
                        dark="#121214", light="#FFFFFF")
        if img.size != (size, size):
            img = img.resize((size, size), Image.NEAREST)
        return img
    except Exception:
        return None


def card_image(title, date_text, big_label, big_value, stats, trend,
               width=640, height=420, accent="#E86D45", big_number=None,
               url=QR_URL, qr_size=64):
    """通用卡片：stats = [(标签, 值)]，trend = [(标签, 数值)]（可为空）"""
    from PIL import Image, ImageDraw

    accent = accent or "#E86D45"
    big_number = big_number or accent
    img = Image.new("RGB", (width, height), CARD_BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, width - 8, height - 8], radius=16,
                        outline=CARD_BORDER, width=1)

    pad = 36
    f_brand = pil_font(22, bold=True)
    f_mid = pil_font(15)
    f_big = pil_font(52, bold=True)
    f_small = pil_font(12)
    f_value = pil_font(18, bold=True)

    d.text((pad, 32), "CCBar", font=f_brand, fill=accent)
    d.text((width - pad, 38), title, font=f_mid, fill=MUTED, anchor="ra")
    d.text((pad, 78), date_text, font=f_small, fill=MUTED)

    d.text((pad, 116), big_label, font=f_mid, fill=MUTED)
    d.text((pad, 142), big_value, font=f_big, fill=big_number)

    # 趋势折线（面积填充用同色压暗近似）
    if len(trend) >= 2:
        top, bottom = height * 0.57, height * 0.71
        values = [v for _, v in trend]
        maxv = max(values) or 1
        step = (width - pad * 2) / (len(trend) - 1)
        pts = [(pad + i * step, bottom - (v / maxv) * (bottom - top))
               for i, (_, v) in enumerate(trend)]
        fill_pts = pts + [(pts[-1][0], bottom), (pts[0][0], bottom)]
        d.polygon(fill_pts, fill=themes.blend(accent, CARD_BG, 0.22))
        d.line(pts, fill=accent, width=2, joint="curve")

    # 三列统计
    col_w = (width - pad * 2) / max(len(stats), 1)
    y_label, y_value = height * 0.78, height * 0.83
    for i, (label, value) in enumerate(stats[:3]):
        x = pad + i * col_w
        d.text((x, y_label), label, font=f_small, fill=MUTED)
        d.text((x, y_value), value, font=f_value, fill="#FFFFFF")

    d.line([(pad, height - 52), (width - pad, height - 52)], fill="#2A2A2A")
    d.text((pad, height - 42), "CCBar · github.com/bmfish/ccbar-win",
           font=f_small, fill=FAINT)

    # 右下角二维码（白底圆角，与 mac 版一致）
    if qr_size > 0:
        payload = _qr_image(url, qr_size)
        if payload is not None:
            box = qr_size + 8
            x1, y1 = width - pad - box, height - pad - box
            d.rounded_rectangle([x1, y1, x1 + box, y1 + box], radius=6,
                                fill="#FFFFFF")
            img.paste(payload, (x1 + 4, y1 + 4))
    return img


def share_card_image(today_total, week_total, month_total, all_total, trend,
                     today_credits=0.0, now=None, theme=None,
                     url=QR_URL, credits_label="积分"):
    """今日战报卡：今日消耗 + 近7天/近30天/累计（有积分时额外列一行）"""
    from datetime import datetime

    tokens = themes.design_tokens(theme) if theme else themes.design_tokens(None)
    now = now or datetime.now()
    stats = [("近 7 天", _fmt_tokens(week_total)),
             ("近 30 天", _fmt_tokens(month_total)),
             ("累计", _fmt_tokens(all_total))]
    return card_image(
        title="AI 用量战报",
        date_text=now.strftime("%Y-%m-%d"),
        big_label="今日消耗",
        big_value=_fmt_tokens(today_total),
        stats=stats,
        trend=trend or [],
        width=640, height=420,
        accent=tokens["BRAND"],
        big_number=tokens["BIG_NUMBER"],
        url=url)


def weekly_card_image(date_text, total, reqs, peak, trend, theme=None,
                      url=QR_URL):
    """用量周报卡：周消耗 + 日均/峰值/请求数（与 mac 版 weeklyShareCard 同字段）"""
    tokens = themes.design_tokens(theme) if theme else themes.design_tokens(None)
    total = int(total or 0)
    return card_image(
        title="AI 用量周报",
        date_text=date_text,
        big_label="周消耗",
        big_value=_fmt_tokens(total),
        stats=[("日均", _fmt_tokens(total // 7)),
               ("峰值", _fmt_tokens(peak)),
               ("请求数", str(int(reqs or 0)))],
        trend=trend or [],
        width=460, height=360,
        accent=tokens["BRAND"],
        big_number=tokens["BIG_NUMBER"],
        url=url, qr_size=56)


def png_bytes(img):
    """PIL 图 → PNG bytes（周报落盘用）"""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def weekly_card_png(date_text, total, reqs, peak, trend, theme=None):
    """weekly_report.generate_if_needed 的渲染回调：直接返回 PNG bytes"""
    return png_bytes(weekly_card_image(date_text, total, reqs, peak, trend,
                                       theme=theme))


def insight_long_image(blocks, width=720, theme=None):
    """洞察长图：把各页要点纵向拼成一张图（mac 版「导出长图」的简化版）

    blocks = [(标题, [(标签, 值), ...])]，返回 PIL 图。
    """
    from PIL import Image, ImageDraw

    tokens = themes.design_tokens(theme) if theme else themes.design_tokens(None)
    accent = tokens["BRAND"]
    pad, row_h, header_h = 28, 26, 40
    height = pad
    for _, rows in blocks:
        height += header_h + max(len(rows), 1) * row_h + 12
    height += pad

    img = Image.new("RGB", (width, height), CARD_BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, width - 8, height - 8], radius=16,
                        outline=CARD_BORDER, width=1)
    f_title = pil_font(15, bold=True)
    f_row = pil_font(13)
    f_head = pil_font(24, bold=True)

    d.text((pad, pad - 4), "CCBar", font=f_head, fill=accent)
    y = pad + 44
    for title, rows in blocks:
        d.text((pad, y), title, font=f_title, fill=accent)
        y += header_h - 8
        for label, value in rows:
            d.text((pad + 8, y), label, font=f_row, fill=MUTED)
            d.text((width - pad - 8, y), value, font=f_row,
                   fill="#FFFFFF", anchor="ra")
            y += row_h
        y += 12
    return img
