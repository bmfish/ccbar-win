"""开机自启（Windows 用户级 Run 键）——对齐 macOS 版 Settings.setLaunchAtLogin。

macOS 用 SMAppService 注册登录项；Windows 侧没有等价的系统 API，
按惯例写 HKCU\\...\\Run 键（只影响当前用户，不需要管理员权限），
值就是启动命令：

  - PyInstaller 打包后：直接就是 exe 路径；
  - 源码运行：`"<python>" "<main.py>"`（都要带引号，路径可能含空格）。

注册表操作通过 backend 注入，非 Windows（或测试里）可以换成假实现，
所以本模块能在任何平台导入与单测。
"""
import sys

APP_NAME = "ccBar"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(executable=None, script=None):
    """构造 Run 键里的启动命令（带引号，兼容含空格的安装路径）"""
    if executable is None:
        executable = sys.executable or "python.exe"
    if getattr(sys, "frozen", False):
        # PyInstaller 打包：sys.executable 就是 ccBar.exe
        return '"%s"' % executable
    if script is None:
        import os
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return '"%s" "%s"' % (executable, script)


def _backend():
    """真机后端：懒加载 winreg，非 Windows 直接抛 OSError 由上层兜住"""
    import winreg

    class _WinReg:
        @staticmethod
        def get(name):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    return value
            except FileNotFoundError:
                return None
            except OSError:
                return None

        @staticmethod
        def set(name, value):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

        @staticmethod
        def delete(name):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass

    return _WinReg


def is_enabled(backend=None):
    """当前是否已登记自启"""
    try:
        backend = backend or _backend()
        return bool(backend.get(APP_NAME))
    except Exception:
        return False


def enable(backend=None, command=None):
    """登记自启。返回 (是否成功, 人话提示)"""
    try:
        backend = backend or _backend()
    except Exception:
        return False, "仅 Windows 支持开机自启"
    try:
        backend.set(APP_NAME, command or launch_command())
        return True, "已设置为开机自启"
    except Exception as e:
        return False, "设置开机自启失败：%s" % e


def disable(backend=None):
    """注销自启。返回 (是否成功, 人话提示)"""
    try:
        backend = backend or _backend()
    except Exception:
        return False, "仅 Windows 支持开机自启"
    try:
        backend.delete(APP_NAME)
        return True, "已关闭开机自启"
    except Exception as e:
        return False, "关闭开机自启失败：%s" % e


def apply(enabled, backend=None, command=None):
    """按设置值同步系统状态（保存设置时调用）"""
    if enabled:
        return enable(backend=backend, command=command)
    return disable(backend=backend)
