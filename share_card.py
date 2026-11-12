"""洞察长图渲染（Pillow 绘制）。

v2.0 起分享页/战报卡/周报卡已移除，本模块只保留洞察页「导出长图」用的
纯函数渲染器：给定 [(标题, [(标签, 值), ...])] 纵向拼成一张 PNG。
不碰 tkinter，方便单测。
"""
import l10n
import themes

# 卡片底色与描边（mac 版 cardFill 0.07/cardBorder 0.10 叠在深底上的等效色）
CARD_BG = "#121214"
CARD_BORDER = "#3D3D3D"
MUTED = "#8C8C8C"

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
