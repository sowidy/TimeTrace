"""测试 autostart.py 里「开机启动命令行」的拼接。

关键点：用源码方式运行时，命令行里必须写脚本的**绝对路径**。
因为开机自启时程序的工作目录是不确定的，写相对路径（比如 "main.py"）
会导致系统找不到文件、启动失败。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

from system import autostart


class AutostartTests(unittest.TestCase):
    def test_enable_replaces_legacy_startup_entry(self):
        registry_key = MagicMock()
        registry_key.__enter__.return_value = registry_key
        with patch.object(autostart.winreg, "OpenKey", return_value=registry_key), \
                patch.object(autostart, "_get_command", return_value='"C:/Apps/TimeTrace.exe"'), \
                patch.object(autostart.winreg, "SetValueEx") as set_value, \
                patch.object(autostart.winreg, "DeleteValue") as delete_value:
            autostart.enable_autostart()
        self.assertEqual(set_value.call_args.args[1], "TimeTrace")
        self.assertEqual(
            [c.args for c in delete_value.call_args_list],
            [(registry_key, "PcTime"), (registry_key, "UsageTracker")])

    def test_migrate_legacy_entry_for_frozen_client(self):
        registry_key = MagicMock()
        registry_key.__enter__.return_value = registry_key
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(autostart.winreg, "OpenKey", return_value=registry_key), \
                patch.object(autostart.winreg, "QueryValueEx", return_value=('old', 1)), \
                patch.object(autostart, "enable_autostart") as enable:
            autostart.migrate_autostart()
        enable.assert_called_once_with()

    def test_source_command_uses_an_absolute_script_path(self):
        """故意先切到别的目录，再检查拼出来的命令是否仍是绝对路径。

        做法：把工作目录换成一个临时目录，同时把 sys.argv 假装成
        ["main.py"]（相对路径），看 _get_command() 会不会把它转成绝对路径。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            try:
                # 切到临时目录，这样相对路径 "main.py" 会解析到临时目录里
                os.chdir(temp_dir)
                # 括号里可以一次写多个 with，等价于层层嵌套的 with。
                # create=True：sys 上原本没有 frozen 属性（跑源码时没有），
                # 让 patch 临时给它加上，否则 patch 会报「属性不存在」
                with (
                    patch.object(sys, "frozen", False, create=True),
                    patch.object(sys, "argv", ["main.py"]),
                    patch.object(sys, "executable", "C:/Python/python.exe"),
                ):
                    command = autostart._get_command()
            finally:
                # 无论断言是否失败，都必须把工作目录切回去，
                # 否则会影响后面其它测试的文件读写
                os.chdir(previous_cwd)

        # 期望的结果：临时目录下的 main.py 绝对路径
        expected_script = os.path.join(temp_dir, "main.py")
        self.assertEqual(
            command,
            f'"C:/Python/python.exe" "{expected_script}"',
        )


if __name__ == "__main__":
    unittest.main()
