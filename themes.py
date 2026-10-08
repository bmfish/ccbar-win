"""主题系统（6 套内置 + 自定义 JSON 主题包）——与 macOS 版 Design.swift 同款。

主题字典的键名刻意沿用 macOS 主题包的 JSON 字段名（accent / data / bigNumber /
trend / models / glowRadius / ...），这样社区主题包在两端可以互相导入导出，
不需要任何字段转换。

颜色一律用 hex 字符串存放（主题包直接可读可改），tkinter 侧需要的实色由
design_tokens() 换算——tkinter 不支持透明度，所以卡片填充/边框按 macOS 的
alpha 叠在背景色上取等效色。
"""
import json
import re
import uuid

FORMAT = "ccbar-theme"
VERSION = 1

# 主题包字段（导出时按此顺序写，导入时缺字段逐项回落默认主题）
THEME_KEYS = ("id", "name", "accent", "data", "bigNumber", "trend", "models",
              "glowRadius", "glowAlpha", "cardFillAlpha", "cardBorderAlpha",
              "separatorAlpha", "bigNumberWeight", "scanlines")

# 图表渐变色带（蓝 → 青 → 绿 → 黄 → 品牌色 → 粉，与 macOS GradientPalette 一致）
GRADIENT_HEAD = ["#4D85F2", "#59C7B8", "#66D17A", "#F2C24D"]
GRADIENT_TAIL = ["#E66B94"]

# 内置主题的 id 就是显示名（沿用 macOS 版，老偏好无需迁移）
BUILTIN_THEMES = [
    {
        "id": "默认主题", "name": "默认主题",
        "accent": "#E86E45", "data": "#F2B373", "bigNumber": "",
        "trend": ["#007AFF", "#AF52DE", "#32ADE2"],
        "models": ["#E86E45", "#598CF2", "#40C78C", "#F29E4D", "#9980FA", "#F266AD"],
        "glowRadius": 14, "glowAlpha": 0.30, "cardFillAlpha": 0.06,
        "cardBorderAlpha": 0.10, "separatorAlpha": 0.10,
        "bigNumberWeight": "bold", "scanlines": False,
    },
    {
        "id": "卡哇伊 01", "name": "卡哇伊 01",
        "accent": "#F25994", "data": "#FFFFFF", "bigNumber": "#FF6BA6",
        "trend": ["#66CCA6", "#8099F2", "#BF80F2"],
        "models": ["#FF73A6", "#66A6F2", "#B380F2", "#66D199", "#FFA64D", "#F2CC66"],
        "glowRadius": 20, "glowAlpha": 0.45, "cardFillAlpha": 0.08,
        "cardBorderAlpha": 0.15, "separatorAlpha": 0.15,
        "bigNumberWeight": "heavy", "scanlines": False,
    },
    {
        "id": "海蓝", "name": "海蓝",
        "accent": "#2E8CF2", "data": "#59BFFF", "bigNumber": "#409EFF",
        "trend": ["#40B3E6", "#6680E6", "#33CCBF"],
        "models": ["#2E8CF2", "#1ABFD9", "#80A6F2", "#73CC8C", "#D98C40", "#A666E6"],
        "glowRadius": 14, "glowAlpha": 0.30, "cardFillAlpha": 0.06,
        "cardBorderAlpha": 0.10, "separatorAlpha": 0.10,
        "bigNumberWeight": "bold", "scanlines": False,
    },
    {
        "id": "翠绿", "name": "翠绿",
        "accent": "#29B86B", "data": "#66E08C", "bigNumber": "#38CC7A",
        "trend": ["#33A673", "#73BF4D", "#1A99B3"],
        "models": ["#29B86B", "#338C59", "#80CC4D", "#1AA6BF", "#D9A633", "#B36633"],
        "glowRadius": 14, "glowAlpha": 0.30, "cardFillAlpha": 0.06,
        "cardBorderAlpha": 0.10, "separatorAlpha": 0.10,
        "bigNumberWeight": "bold", "scanlines": False,
    },
    {
        "id": "星空紫", "name": "星空紫",
        "accent": "#8C52F2", "data": "#B88CFF", "bigNumber": "#9E66FF",
        "trend": ["#9973E6", "#CC59D9", "#598CE6"],
        "models": ["#8C52F2", "#CC59F2", "#4D80F2", "#F26699", "#73CCA6", "#F2A64D"],
        "glowRadius": 14, "glowAlpha": 0.30, "cardFillAlpha": 0.06,
        "cardBorderAlpha": 0.10, "separatorAlpha": 0.10,
        "bigNumberWeight": "bold", "scanlines": False,
    },
    {
        "id": "CRT 终端", "name": "CRT 终端",
        "accent": "#4DF28C", "data": "#99FFBF", "bigNumber": "",
        "trend": ["#66E68C", "#D9E666", "#4DD9B3"],
        "models": ["#4DF28C", "#33BF66", "#99FFBF", "#E6E659", "#26994D", "#73E6D9"],
        "glowRadius": 12, "glowAlpha": 0.40, "cardFillAlpha": 0.05,
        "cardBorderAlpha": 0.20, "separatorAlpha": 0.10,
        "bigNumberWeight": "bold", "scanlines": True,
    },
]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def default_theme():
    """默认主题（找不到偏好时的回落）"""
    return dict(BUILTIN_THEMES[0])


def all_themes(custom=()):
    """内置 + 自定义，设置页下拉用这个顺序"""
    return [dict(t) for t in BUILTIN_THEMES] + [dict(t) for t in (custom or [])]


def find(theme_id, custom=()):
    """按 id 找主题，找不到回落默认主题"""
    for theme in all_themes(custom):
        if theme.get("id") == theme_id:
            return theme
    return default_theme()


# ------------------------------------------------------------ 颜色工具


def parse_hex(value, fallback="#FFFFFF"):
    """hex 字符串 → (r, g, b)。

    与 macOS 版 NSColor(hex:) 同款宽容度：可省 #、支持 3 位缩写；
    非法值回落 fallback（mac 回落白色，绝不抛异常把设置页搞崩）。
    """
    if not isinstance(value, str):
        value = ""
    m = _HEX_RE.match(value.strip())
    if not m:
        return parse_hex(fallback, "#FFFFFF") if fallback != "#FFFFFF" else (255, 255, 255)
    body = m.group(1)
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))


def to_hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def is_valid_hex(value):
    return isinstance(value, str) and bool(_HEX_RE.match(value.strip()))


def blend(color, background, alpha):
    """把 color 以 alpha 不透明度叠到 background 上（tkinter 没有透明度）"""
    c = parse_hex(color)
    b = parse_hex(background)
    alpha = max(0.0, min(1.0, float(alpha)))
    return to_hex(tuple(c[i] * alpha + b[i] * (1 - alpha) for i in range(3)))


# ------------------------------------------------------------ tkinter 令牌


def design_tokens(theme, background="#1C1C1C"):
    """主题 → Windows 端 Design 令牌（main.Design 按此逐项覆盖）。

    背景色不进主题包（mac 版背景固定），但卡片填充/边框/分隔线按主题的
    alpha 叠在背景上取等效实色。
    """
    theme = theme or default_theme()
    accent = theme.get("accent") or default_theme()["accent"]
    data = theme.get("data") or accent
    models = list(theme.get("models") or []) or list(default_theme()["models"])
    trend = list(theme.get("trend") or []) or list(default_theme()["trend"])
    fill = float(theme.get("cardFillAlpha", 0.06))
    border = float(theme.get("cardBorderAlpha", 0.10))
    sep = float(theme.get("separatorAlpha", 0.10))

    return {
        "BRAND": accent,
        "DATA": data,
        "BIG_NUMBER": theme.get("bigNumber") or accent,
        "GRADIENT": GRADIENT_HEAD + [accent] + GRADIENT_TAIL,
        "MODEL_COLORS": models,
        "TREND": {"yesterday": trend[0], "week": trend[1],
                  "month": trend[2], "total": accent},
        "CARD_FILL": blend("#FFFFFF", background, fill),
        "CARD_BORDER": blend("#FFFFFF", background, border),
        "SEPARATOR": blend("#FFFFFF", background, sep),
        "BTN_BG": blend("#FFFFFF", background, min(fill + 0.02, 1.0)),
        "BTN_BG_HOVER": blend("#FFFFFF", background, min(fill + 0.10, 1.0)),
        "ROW_HOVER": blend("#FFFFFF", background, max(fill - 0.02, 0.0)),
        "GLOW_RADIUS": theme.get("glowRadius", 14),
        "GLOW_ALPHA": theme.get("glowAlpha", 0.30),
        "BIG_NUMBER_WEIGHT": theme.get("bigNumberWeight", "bold"),
        "SCANLINES": bool(theme.get("scanlines", False)),
    }


# ------------------------------------------------------------ 主题包 JSON


def normalize(theme):
    """补齐缺字段、修正非法取值（导入主题包与读自定义主题都走这里）"""
    base = default_theme()
    out = {}
    for key in THEME_KEYS:
        value = theme.get(key, base.get(key))
        if key in ("trend",):
            value = list(value) if isinstance(value, (list, tuple)) else []
            if len(value) != 3:
                value = list(base["trend"])
        elif key == "models":
            value = list(value) if isinstance(value, (list, tuple)) else []
            if not value:
                value = list(base["models"])
        elif key == "id":
            value = value if isinstance(value, str) else ""
        elif key == "name":
            value = value if isinstance(value, str) and value.strip() else base["name"]
        elif key == "scanlines":
            value = bool(value)
        elif key == "bigNumberWeight":
            value = "heavy" if value == "heavy" else "bold"
        elif key in ("glowRadius", "glowAlpha", "cardFillAlpha",
                     "cardBorderAlpha", "separatorAlpha"):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(base[key])
        out[key] = value
    return out


def theme_from_json(text):
    """解析主题包 JSON。不是 ccbar 主题包抛 ValueError；其余字段宽容回落"""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError("主题包不是合法 JSON: %s" % e)
    if not isinstance(raw, dict):
        raise ValueError("主题包必须是 JSON 对象")
    fmt = raw.get("format")
    if fmt is not None and fmt != FORMAT:
        raise ValueError("不是 ccBar 主题包")
    theme = normalize(raw)
    # 自定义主题持久化带 id，主题包则重新发 id（与 macOS 版一致）
    theme["id"] = raw.get("id") or ("custom-%s" % uuid.uuid4())
    return theme


def theme_to_json(theme, indent=2):
    """导出主题包（字段名与 macOS 版完全一致，两端可互相导入）"""
    theme = normalize(theme)
    payload = {"format": FORMAT, "version": VERSION}
    for key in THEME_KEYS:
        payload[key] = theme[key]
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def upsert_custom(custom, theme):
    """加入/更新自定义主题：同名视为同一套（沿用原 id），避免重复项"""
    theme = normalize(theme)
    out = [dict(t) for t in (custom or [])]
    for i, existing in enumerate(out):
        if existing.get("name") == theme["name"]:
            theme["id"] = existing.get("id") or theme["id"]
            out[i] = theme
            return out
    out.append(theme)
    return out


def rename_custom(custom, theme_id, new_name):
    out = [dict(t) for t in (custom or [])]
    for theme in out:
        if theme.get("id") == theme_id:
            theme["name"] = new_name
    return out


def delete_custom(custom, theme_id):
    return [dict(t) for t in (custom or []) if t.get("id") != theme_id]


def is_custom(theme_id, custom=()):
    """内置主题不允许重命名/删除"""
    return any(t.get("id") == theme_id for t in (custom or []))
