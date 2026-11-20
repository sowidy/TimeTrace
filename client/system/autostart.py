"""开机自启管理：通过写 Windows 注册表让程序开机时自动运行。

Windows 有一个约定的注册表位置，只要往里面写一条「启动项」，系统登录后就会
自动执行它。本模块负责增删查这条记录。

注册表路径说明：
    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
    这个位置是「只对当前用户生效」的自启项，不需要管理员权限即可修改
    （如果写到 HKEY_LOCAL_MACHINE 下面就需要管理员权限了）。
"""
import os
import sys
import winreg

APP_NAME = "TimeTrace"  # 注册表里的值名称，也是任务管理器「启动」页看到的项
_LEGACY_APP_NAMES = ("PcTime", "UsageTracker")
KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _get_command():
    """拼出开机时应该执行的命令行字符串。

    分两种运行方式：
        1. 打包成 exe 运行：直接执行 exe 本身。
        2. 用 Python 源码运行：需要 "python.exe 脚本路径" 这样两条信息。
    两种写法都用双引号包起来，因为路径里可能包含空格。
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后，sys.executable 就是 exe 自己的路径
        return f'"{sys.executable}"'
    python = sys.executable
    # 用 abspath 是因为 sys.argv[0] 可能只是 "main.py" 这种相对路径，
    # 而开机启动时工作目录不确定，相对路径会找不到文件。
    script = os.path.abspath(sys.argv[0])
    return f'"{python}" "{script}"'


def is_autostart_enabled() -> bool:
    """查询注册表里有没有我们的自启项。有 → True，没有（或读不到）→ False。"""
    try:
        # winreg 的 key 对象支持 with 语法，退出时会自动关闭句柄
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY_PATH, 0, winreg.KEY_READ) as key:
            for name in (APP_NAME, *_LEGACY_APP_NAMES):
                try:
                    winreg.QueryValueEx(key, name)
                    return True
                except FileNotFoundError:
                    continue
            return False
    except (FileNotFoundError, OSError):
        # 值不存在时 QueryValueEx 会抛 FileNotFoundError
        return False


def enable_autostart():
    """写入自启项，让程序开机自动运行。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        # REG_SZ 表示普通字符串类型的注册表值
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _get_command())
        for name in _LEGACY_APP_NAMES:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def disable_autostart():
    """删除自启项。如果本来就不存在，静默忽略（删除是幂等的）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            for name in (APP_NAME, *_LEGACY_APP_NAMES):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass


def migrate_autostart():
    """打包版首次启动时，将旧启动项指向当前 TimeTrace.exe。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY_PATH, 0, winreg.KEY_READ) as key:
            has_legacy = False
            for name in _LEGACY_APP_NAMES:
                try:
                    winreg.QueryValueEx(key, name)
                except (FileNotFoundError, OSError):
                    continue
                has_legacy = True
                break
            if not has_legacy:
                return
    except (FileNotFoundError, OSError):
        return
    try:
        enable_autostart()
    except OSError:
        pass  # 没有注册表写权限时保留旧项，不能影响客户端启动。


def toggle_autostart():
    """在「开」和「关」之间切换，并返回切换后的状态。

    返回值不是猜的，而是切换后重新读一遍注册表，确保返回的是真实状态
    （比如写注册表被安全软件拦了，这里就能如实反映出来）。
    """
    if is_autostart_enabled():
        disable_autostart()
    else:
        enable_autostart()
    return is_autostart_enabled()
