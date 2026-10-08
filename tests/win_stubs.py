"""非 Windows 环境下导入 main 的桩件。

main.py 只在方法内部使用 pystray / win10toast / pyperclip / ctypes.windll，
导入期只需要这三个模块存在。真机（Windows）装了真包就用真的。
"""
import sys
import types

_STUBS = {
    "pystray": ("Icon", "Menu", "MenuItem"),
    "win10toast": ("ToastNotifier",),
    "pyperclip": ("copy", "paste"),
}


def install():
    for name, attrs in _STUBS.items():
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            for attr in attrs:
                setattr(mod, attr, type(attr, (), {}))
            sys.modules[name] = mod
