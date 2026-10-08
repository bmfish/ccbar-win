"""开机自启：注册表后端可注入，非 Windows 上也能跑（真机后端懒加载 winreg）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autostart  # noqa: E402


class FakeRegistry:
    """假注册表：{名称: 值}"""

    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class BrokenRegistry:
    def get(self, name):
        return None

    def set(self, name, value):
        raise OSError("拒绝访问")

    def delete(self, name):
        raise OSError("拒绝访问")


class TestLaunchCommand(unittest.TestCase):
    def test_frozen_uses_executable_only(self):
        old = getattr(sys, "frozen", None)
        sys.frozen = True
        try:
            self.assertEqual(autostart.launch_command(executable="C:/App/ccBar.exe"),
                             '"C:/App/ccBar.exe"')
        finally:
            if old is None:
                del sys.frozen
            else:
                sys.frozen = old

    def test_source_run_quotes_both_paths(self):
        """源码运行：python + main.py，路径含空格也要能起得来"""
        cmd = autostart.launch_command(executable="C:/Program Files/Python/python.exe",
                                      script="C:/My App/main.py")
        self.assertEqual(cmd, '"C:/Program Files/Python/python.exe" "C:/My App/main.py"')

    def test_source_run_defaults_to_repo_main(self):
        cmd = autostart.launch_command(executable="python.exe")
        self.assertIn("main.py", cmd)
        self.assertTrue(cmd.startswith('"python.exe" "'))


class TestEnableDisable(unittest.TestCase):
    def setUp(self):
        self.reg = FakeRegistry()

    def test_enable_then_is_enabled_then_disable(self):
        self.assertFalse(autostart.is_enabled(self.reg))
        ok, msg = autostart.enable(self.reg, command='"ccBar.exe"')
        self.assertTrue(ok, msg)
        self.assertEqual(self.reg.values[autostart.APP_NAME], '"ccBar.exe"')
        self.assertTrue(autostart.is_enabled(self.reg))

        ok, msg = autostart.disable(self.reg)
        self.assertTrue(ok, msg)
        self.assertFalse(autostart.is_enabled(self.reg))

    def test_disable_is_idempotent(self):
        ok, _ = autostart.disable(self.reg)
        self.assertTrue(ok, "本来就没登记也不能报错")

    def test_apply_follows_setting_flag(self):
        autostart.apply(True, backend=self.reg, command='"ccBar.exe"')
        self.assertTrue(autostart.is_enabled(self.reg))
        autostart.apply(False, backend=self.reg)
        self.assertFalse(autostart.is_enabled(self.reg))

    def test_registry_errors_are_reported_not_raised(self):
        ok, msg = autostart.enable(BrokenRegistry())
        self.assertFalse(ok)
        self.assertIn("失败", msg)
        ok, msg = autostart.disable(BrokenRegistry())
        self.assertFalse(ok)

    def test_run_key_is_per_user(self):
        """只写 HKCU，不动 HKLM（不需要管理员权限）"""
        self.assertTrue(autostart.RUN_KEY.startswith("Software\\Microsoft\\Windows"))
        self.assertNotIn("HKEY_LOCAL_MACHINE", autostart.RUN_KEY)


@unittest.skipIf(sys.platform == "win32", "Windows 上真后端可用")
class TestNonWindowsFallback(unittest.TestCase):
    def test_default_backend_reports_unsupported(self):
        """非 Windows：不抛异常，返回人话提示（mac 版是 SMAppService，不适用）"""
        self.assertFalse(autostart.is_enabled())
        ok, msg = autostart.enable()
        self.assertFalse(ok)
        self.assertIn("Windows", msg)


if __name__ == "__main__":
    unittest.main()
