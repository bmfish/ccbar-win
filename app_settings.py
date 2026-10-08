"""ccBar 设置存储（JSON 版）

替换 main.py 里硬编码的 ~/.ccbar/settings.txt 读写：
- JSON 持久化（UTF-8 / ensure_ascii=False / indent=2），键可扩展；文件里不认识的键原样保留
- settings.json 缺失但旧 settings.txt 存在时自动迁移一次；坏行/坏数值一律跳过（旧版这里会崩）
- 文件损坏、字段缺失、类型不对一律回落默认值，绝不抛异常打断托盘启动

用法：
    s = Settings()                 # 默认 ~/.ccbar/settings.json
    s = Settings(tmp_dir)          # 测试注入目录或文件路径
    s.get("refresh_interval")      # 也支持 s["refresh_interval"] / s.refresh_interval
    s.apply({"refresh_interval": "120"})   # 校验→转类型→落盘；返回中文错误标签列表
    s.source_configs()             # [(adapter_id, enabled, path)]，顺序 ccswitch/zcode/trae
"""
import copy
import json
import locale
import os
from pathlib import Path

# 家目录下的设置目录与新旧文件名
CONFIG_DIR = Path.home() / ".ccbar"
FILE_NAME = "settings.json"
LEGACY_NAME = "settings.txt"

# 数据源顺序：与 macOS 版设置页一致（Trae 是 HTTP 源，不在 stats_store 注册表里）
SOURCE_ORDER = ["ccswitch", "zcode", "trae"]

# 应用语言取值（macOS 版 AppLanguage）
APP_LANGUAGES = ("system", "zh", "en")

# ccswitch/zcode 默认库路径从 stats_store 取，避免两处漂移；取不到时用字面量兜底
try:
    from stats_store import SOURCE_REGISTRY as _SOURCE_REGISTRY
except Exception:  # stats_store 暂不可用（并发改动/缺失）也不能拖垮设置模块
    _SOURCE_REGISTRY = []
DEFAULT_PATHS = {getattr(a, "id", ""): (getattr(a, "default_path", "") or "")
                 for a in _SOURCE_REGISTRY}
DEFAULT_PATHS["ccswitch"] = DEFAULT_PATHS.get("ccswitch") or os.path.expanduser("~/.cc-switch/cc-switch.db")
DEFAULT_PATHS["zcode"] = DEFAULT_PATHS.get("zcode") or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
DEFAULT_PATHS["trae"] = ""  # HTTP 源，没有本地库路径

# 设置键 → 默认值。新增键只改这里，读取/校验/迁移/重置自动跟着走。
# 键名与 macOS 版 UserDefaults 一一对应（下划线命名）
DEFAULTS = {
    "refresh_interval": 60,               # 刷新间隔（秒）
    "warning_threshold": 50,              # 预警阈值（万）
    "warning_enabled": True,
    "notify_interval": 1000,              # 通知间隔（万），0=关闭
    "db_path": DEFAULT_PATHS["ccswitch"],
    "ccswitch_enabled": True,
    "zcode_enabled": False,
    "zcode_path": DEFAULT_PATHS["zcode"],
    "trae_enabled": False,
    "trae_sessionid": "",                 # Trae 的 passport sessionid cookie
    "led_red_threshold": 100,             # 闪电 LED 红色门槛（万），0=不变红
    "monthly_budget_usd": 0.0,            # 月度预算（$），0=关闭
    "default_token_price": 0.0,           # 未计费渠道默认单价（$/M tokens），0=关闭
    "auto_weekly_report": True,
    "launch_at_login": False,
    "popover_wide": False,
    "app_language": "system",
    "theme": "默认主题",
    "custom_themes": [],
    "insights_last_page": "费用",
    "last_auto_backup_date": "",
    "last_update_check_date": "",
}

# 源的启用键 / 库路径键（db_path 是旧版单路径设置，对应 ccswitch 源）
_SOURCE_ENABLE_KEYS = {"ccswitch": "ccswitch_enabled", "zcode": "zcode_enabled", "trae": "trae_enabled"}
_SOURCE_PATH_KEYS = {"ccswitch": "db_path", "zcode": "zcode_path"}

# 旧 settings.txt 的 8 个键
LEGACY_KEYS = ("refresh_interval", "db_path", "ccswitch_enabled", "zcode_enabled",
               "zcode_path", "warning_threshold", "warning_enabled", "notify_interval")
_LEGACY_BOOL_KEYS = ("ccswitch_enabled", "zcode_enabled", "warning_enabled")
_LEGACY_INT_KEYS = ("refresh_interval", "warning_threshold", "notify_interval")

_MISSING = object()  # 哨兵：表示“该键不可用，回落到默认值”


# ============================================================
# 取值/校验小工具（模块级，Settings 与迁移逻辑共用同一口径）
# ============================================================

def _as_int(value):
    """→ int；空串、小数串、布尔等一律返回 None（与 macOS Int("") == nil 同口径）"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value, empty_zero=False):
    """→ float；失败返回 None。empty_zero 时空输入按 0（预算/单价，同 macOS）"""
    if value is None or isinstance(value, bool):
        return 0.0 if (empty_zero and value is None) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0 if empty_zero else None
        value = s
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value):
    """→ bool；无法判定返回 None（JSON 里手写的 "true"/0/1 也认）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
        return None
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _as_language(value):
    """→ 取值合法的语言代码；非法返回系统默认"""
    return value if value in APP_LANGUAGES else DEFAULTS["app_language"]


def _clean_value(key, value):
    """载入时按默认值的类型族规整；类型不对返回 _MISSING（丢弃该键，回落默认）。
    未知键原样返回，保证向前兼容。"""
    base = DEFAULTS.get(key, _MISSING)
    if base is _MISSING:
        return value
    if key == "app_language":
        return value if value in APP_LANGUAGES else _MISSING
    if isinstance(base, bool):
        v = _as_bool(value)
        return _MISSING if v is None else v
    if isinstance(base, int):
        v = _as_int(value)
        return _MISSING if v is None else v
    if isinstance(base, float):
        v = _as_float(value, empty_zero=True)
        return _MISSING if v is None else v
    if isinstance(base, str):
        return value if isinstance(value, str) else _MISSING
    if isinstance(base, list):
        return value if isinstance(value, list) else _MISSING
    return value


def _coerce_value(key, value):
    """写入时按默认值类型强转（tkinter Entry 给的字符串 → int/float）；
    转不动就原样存（调用方负责先 validate）。"""
    base = DEFAULTS.get(key, _MISSING)
    if base is _MISSING:
        return value
    if key == "app_language":
        return _as_language(value)
    if isinstance(base, bool):
        v = _as_bool(value)
        return bool(value) if v is None else v
    if isinstance(base, int):
        v = _as_int(value)
        return value if v is None else v
    if isinstance(base, float):
        v = _as_float(value, empty_zero=True)
        return value if v is None else v
    if isinstance(base, str):
        return "" if value is None else (value if isinstance(value, str) else str(value))
    return value


def validate_updates(updates):
    """校验待写入的数值字段，返回中文错误标签列表；空列表=全部合法。
    文案与 macOS 版 SettingsView.save() 完全一致，顺序也一致。"""
    errors = []
    u = updates or {}
    if "refresh_interval" in u:
        v = _as_int(u["refresh_interval"])
        if v is None or not 5 <= v <= 3000:
            errors.append("刷新间隔（5 ~ 3000 秒）")
    if "warning_threshold" in u:
        v = _as_int(u["warning_threshold"])
        if v is None or v <= 0:
            errors.append("预警阈值（正整数，万）")
    if "notify_interval" in u:
        v = _as_int(u["notify_interval"])
        if v is None or v < 0:
            errors.append("通知间隔（≥ 0 的整数，万，0=关闭）")
    if "led_red_threshold" in u:
        v = _as_int(u["led_red_threshold"])
        if v is None or v < 0:
            errors.append("红色门槛（≥ 0 的整数，万，0=不变红）")
    if "monthly_budget_usd" in u:
        v = _as_float(u["monthly_budget_usd"], empty_zero=True)
        if v is None or v < 0:
            errors.append("月度预算（≥ 0 的数字，$，0=关闭）")
    if "default_token_price" in u:
        v = _as_float(u["default_token_price"], empty_zero=True)
        if v is None or v < 0:
            errors.append("默认单价（≥ 0 的数字，$/M tokens，0=关闭）")
    return errors


# ============================================================
# 旧 settings.txt 迁移
# ============================================================

def _read_legacy_text(path):
    """读旧文件文本：先 UTF-8(带/不带 BOM)，再退回 GBK/系统本地编码
    （旧版在 Windows 上按本地编码写盘，中文路径不能按 UTF-8 硬解）"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gbk", locale.getpreferredencoding(False)):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _parse_legacy(path):
    """解析旧 settings.txt 的 key=value 行；坏行/坏数值一律跳过，绝不抛异常"""
    try:
        text = _read_legacy_text(path)
    except OSError:
        return {}
    updates = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.strip().partition("=")
        key, value = key.strip(), value.strip()
        if key not in LEGACY_KEYS:
            continue
        try:
            if key in _LEGACY_BOOL_KEYS:
                updates[key] = value.lower() == "true"
            elif key in _LEGACY_INT_KEYS:
                updates[key] = int(value)
            else:
                updates[key] = value
        except ValueError:
            continue  # 坏数值跳过（旧版 int(value) 会直接崩掉启动）
    return updates


def _migrated_value(key, value):
    """旧值只有自己合法才采用，否则保留默认（坏配置不放大）"""
    if validate_updates({key: value}):
        return _MISSING
    return _coerce_value(key, value)


def _defaults():
    """默认值副本（list/dict 值必须深拷贝，避免调用方改到 DEFAULTS）"""
    return copy.deepcopy(DEFAULTS)


# ============================================================
# Settings
# ============================================================

class Settings:
    """设置存取：dict 语义（get/set/[]）+ 属性语义（settings.theme），落盘走 JSON"""

    def __init__(self, path=None):
        self.path = self._resolve_path(path)
        self._data = _defaults()
        self.reload()

    @staticmethod
    def _resolve_path(path):
        """path 可以是目录（测试注入的临时目录）或文件路径；都归一成 settings.json 路径"""
        if path is None:
            return CONFIG_DIR / FILE_NAME
        p = Path(path)
        if p.is_dir() or str(path).endswith(("/", os.sep)):
            return p / FILE_NAME
        return p

    # ------------------------------------------------------------ 读写

    def get(self, key, default=None):
        """读设置；未知键返回 default"""
        return self._data.get(key, default)

    def set(self, key, value):
        """写单个键（已知数值键顺手转类型，Entry 里的字符串也能直接存）"""
        self._data[key] = _coerce_value(key, value)

    def as_dict(self):
        """全部设置的副本（含未知键）"""
        return copy.deepcopy(self._data)

    def __getitem__(self, key):
        # dict 式读取；未知键返回 None 不抛 KeyError，方便 batch 2 平滑迁移
        return self._data.get(key)

    def __setitem__(self, key, value):
        self.set(key, value)

    def __contains__(self, key):
        return key in self._data

    def __getattr__(self, name):
        # 属性式读取：settings.refresh_interval == settings.get("refresh_interval")
        if name.startswith("_"):
            raise AttributeError(name)
        data = self.__dict__.get("_data")
        if isinstance(data, dict) and name in data:
            return data[name]
        raise AttributeError(name)

    def save(self):
        """写回 JSON（UTF-8 / ensure_ascii=False / indent=2）。
        先写临时文件再替换，避免进程被杀时留下半截文件；落盘失败只告警不抛。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self._data, ensure_ascii=False, indent=2, default=str)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[ccBar] 设置保存失败: {e}")

    def reload(self):
        """丢弃内存改动重新读取；settings.json 缺失时尝试从旧 settings.txt 迁移一次"""
        self._data = _defaults()
        if self.path.exists():
            raw = None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None  # 损坏 → 全默认，绝不抛异常
            if isinstance(raw, dict):
                for key, value in raw.items():
                    v = _clean_value(key, value)
                    if v is not _MISSING:
                        self._data[key] = v
            return self._data

        legacy = self.path.parent / LEGACY_NAME
        if legacy.exists():
            # 一次性迁移：解析出的合法旧值覆盖默认，随后立刻落盘（下次直接读 JSON）
            for key, value in _parse_legacy(legacy).items():
                v = _migrated_value(key, value)
                if v is not _MISSING:
                    self._data[key] = v
            self.save()
        return self._data

    # ------------------------------------------------------------ 校验 / 应用

    def validate(self, updates):
        """校验一批待写入的值，返回中文错误标签列表（空=合法）"""
        return validate_updates(updates)

    def apply(self, updates):
        """校验通过才转类型写入并落盘；有错则不改内存、不落盘，只返回错误列表"""
        updates = dict(updates or {})
        errors = self.validate(updates)
        if errors:
            return errors
        for key, value in updates.items():
            self._data[key] = _coerce_value(key, value)
        self.save()
        return []

    def reset(self):
        """恢复默认值并落盘（与 macOS 版 reset() 同口径：刷新间隔取 30）"""
        self._data = _defaults()
        self._data["refresh_interval"] = 30
        self.save()

    # ------------------------------------------------------------ 数据源

    def source_configs(self):
        """→ [(adapter_id, enabled, path)]，按 ccswitch/zcode/trae 顺序；
        path 留空回落到该源默认路径，~ 一律展开（否则 SQLite ATTACH 打不开）"""
        configs = []
        for sid in SOURCE_ORDER:
            enabled = bool(self.get(_SOURCE_ENABLE_KEYS.get(sid, ""), False))
            raw = self.get(_SOURCE_PATH_KEYS[sid], "") if sid in _SOURCE_PATH_KEYS else ""
            path = (raw or "").strip() if isinstance(raw, str) else ""
            path = os.path.expanduser(path) if path else ""
            if not path:
                path = DEFAULT_PATHS.get(sid, "")
            configs.append((sid, enabled, path))
        return configs

    def enabled_sources(self):
        """已启用的数据源 id 列表（顺序同注册表）"""
        return [sid for sid, enabled, _ in self.source_configs() if enabled]
