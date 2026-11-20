"""测试 storage.py 的数据库逻辑：跨天拆分、应用合并、显示名、数据清理。

这些测试会真的建数据库、真的读写——但用的是临时目录里的文件，
不会碰你本机那份 usage.db（见 setUp）。

setUp / tearDown 是 unittest 的固定用法：
    setUp 在每个测试方法运行前自动执行，用来准备环境
    addCleanup 注册的函数会在测试结束后自动执行，用来恢复环境
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from core import storage


class StorageDateTests(unittest.TestCase):
    def setUp(self):
        # 建一个临时目录，TemporaryDirectory 会在 cleanup 时把目录连文件一起删掉
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # 记住原来的数据库路径，测试完要还原，避免影响其它测试或真实数据
        self.original_db_path = storage.DB_PATH
        self.addCleanup(setattr, storage, "DB_PATH", self.original_db_path)
        # 把模块级变量指向临时数据库。
        # 之所以能这样改：storage 里的函数都是运行时读取 DB_PATH 的
        storage.DB_PATH = os.path.join(self.temp_dir.name, "usage.db")
        storage.init_db()

    def test_fragment_crossing_midnight_is_split_between_days(self):
        """跨过午夜的一条片段，应该被拆成两天各占一半。

        23:59:55 ~ 次日 00:00:05 共 10 秒，拆分后两天各 5 秒。
        """
        storage.record_fragment(
            "example.exe",
            "Example",
            "C:/Example/example.exe",
            "2026-08-10T23:59:55",
            "2026-08-11T00:00:05",
            10.0,
        )

        self.assertEqual(storage.get_daily_total("2026-08-10"), 5.0)
        self.assertEqual(storage.get_daily_total("2026-08-11"), 5.0)
        # 每一天各拿到一条片段，而不是两天公用同一条
        self.assertEqual(len(storage.get_fragments_for_date("2026-08-10")), 1)
        self.assertEqual(len(storage.get_fragments_for_date("2026-08-11")), 1)

    def test_ads_processes_are_merged_and_use_the_main_executable(self):
        """Keysight ADS 的多个进程要合并成一行，并且用指定的那个 exe 取图标。

        hpeesofdds.exe 和 hpeesofde.exe 合并后统一显示为 hpeesofde.exe，
        但图标路径要取 hpeesofdds.exe（因为它的图标更好看，
        这条规则写在 storage._PREFERRED_ICON_EXECUTABLES 里）。
        """
        storage.record_fragment(
            "hpeesofde.exe", "ADS", "D:/ADS/bin/hpeesofde.exe",
            "2026-08-10T10:00:00", "2026-08-10T10:01:00", 60.0,
        )
        storage.record_fragment(
            "hpeesofdds.exe", "ADS", "D:/ADS/bin/hpeesofdds.exe",
            "2026-08-10T10:01:00", "2026-08-10T10:03:00", 120.0,
        )

        stats = storage.get_stats("2026-08-10")

        # 两个进程应该合并成一行，而不是两行
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["app_name"], "hpeesofde.exe")
        self.assertEqual(stats[0]["display_name"], "Keysight ADS")
        self.assertEqual(stats[0]["total_seconds"], 180.0)  # 60 + 120
        self.assertEqual(stats[0]["exe_path"], "D:/ADS/bin/hpeesofdds.exe")

    def test_wechat_legacy_rule_is_migrated_to_weixin(self):
        """老版本的微信规则应该被自动修正到 weixin.exe（数据迁移测试）。

        模拟老用户库里存着 wechatappex.exe → wechat.exe 这条旧规则，
        再跑一次 init_db()，它应该被改成指向 weixin.exe。
        """
        storage.add_app_rule("wechatappex.exe", "wechat.exe", "微信")

        storage.init_db()  # 模拟「程序升级后重新启动」
        rules = storage.get_app_rules()

        self.assertEqual(rules["wechatappex.exe"], "weixin.exe")
        # 顺着规则链走：wechat.exe → weixin.exe
        self.assertEqual(
            storage.canonical_app_name("wechat.exe", rules),
            "weixin.exe",
        )

    def test_codex_package_chatgpt_process_is_merged_into_codex(self):
        """Codex 桌面版的进程名也叫 ChatGPT.exe，要靠 exe 路径区分出来。

        exe 路径里含 "OpenAI.Codex_" 的 chatgpt.exe 应该被归到 codex.exe，
        这样它才不会跟真正的 ChatGPT 混在一起。
        """
        path = (
            "C:/Program Files/WindowsApps/"
            "OpenAI.Codex_26.803.5235.0_x64/app/ChatGPT.exe"
        )
        storage.record_fragment(
            "chatgpt.exe", "Codex", path,
            "2026-08-10T10:00:00", "2026-08-10T10:01:00", 60.0,
        )
        storage.record_fragment(
            "codex.exe", "Codex", "C:/Apps/Codex.exe",
            "2026-08-10T10:01:00", "2026-08-10T10:02:00", 60.0,
        )

        stats = storage.get_stats("2026-08-10")

        self.assertEqual(len(stats), 1)  # 两条合并成一条
        self.assertEqual(stats[0]["app_name"], "codex.exe")
        self.assertEqual(stats[0]["total_seconds"], 120.0)

    @patch("system.icons.get_shortcut_info")
    def test_known_system_name_beats_unrelated_shortcut(self, shortcut_info):
        """内置中文名表的优先级要高于快捷方式名。

        场景：桌面上有个叫「Origin 2024 示例数据」的快捷方式指向 explorer.exe。
        如果按快捷方式命名，资源管理器就会被显示成「Origin 2024 示例数据」。
        正确行为是直接用内置名字「文件资源管理器」，根本不去查快捷方式。
        """
        # 这里给的返回值是调用方「不该用到」的，用来验证它确实没被调用
        shortcut_info.return_value = {
            "display_name": "Origin 2024 示例数据",
            "icon_path": "C:/Windows/explorer.exe",
            "icon_index": 0,
        }

        name = storage.get_display_name("explorer.exe")

        self.assertEqual(name, "文件资源管理器")
        # 关键断言：既然内置表里已经有名字，就不该再去查快捷方式
        shortcut_info.assert_not_called()

    def test_init_removes_current_and_legacy_self_from_statistics(self):
        """统计器自己的窗口不能出现在统计结果里（升级时要清理历史脏数据）。"""
        storage.record_fragment(
            "usagetracker.exe", "UsageTracker", "C:/UsageTracker.exe",
            "2026-08-10T10:00:00", "2026-08-10T10:01:00", 60.0,
        )
        storage.record_fragment(
            "timetrace.exe", "TimeTrace", "C:/TimeTrace.exe",
            "2026-08-10T10:01:00", "2026-08-10T10:02:00", 60.0,
        )
        storage.record_fragment(
            "pctime.exe", "PcTime", "C:/PcTime.exe",
            "2026-08-10T10:02:00", "2026-08-10T10:03:00", 60.0,
        )

        storage.init_db()  # init_db 里会执行清理

        self.assertEqual(storage.get_daily_total("2026-08-10"), 0)
        self.assertEqual(storage.get_stats("2026-08-10"), [])

    def test_summary_range_merges_and_filters_by_date(self):
        """get_summary_range 应该按日期范围过滤，并复用应用合并逻辑。"""
        # 微信的两个进程（wechatappex.exe / weixin.exe）会合并成一行「微信」
        storage.record_fragment(
            "wechatappex.exe", "微信", "C:/WeChat/WeChatAppEx.exe",
            "2026-08-10T10:00:00", "2026-08-10T10:01:00", 60.0,
        )
        storage.record_fragment(
            "weixin.exe", "微信", "C:/WeChat/weixin.exe",
            "2026-08-10T10:01:00", "2026-08-10T10:02:00", 60.0,
        )
        storage.record_fragment(
            "chrome.exe", "Chrome", "C:/Chrome/chrome.exe",
            "2026-08-11T10:00:00", "2026-08-11T10:02:00", 120.0,
        )

        rows = storage.get_summary_range("2026-08-10", "2026-08-11")

        # 两天两条：08-10 的微信（合并后 120 秒）、08-11 的 Chrome
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-08-10")
        self.assertEqual(rows[0]["display_name"], "微信")
        self.assertEqual(rows[0]["total_seconds"], 120.0)
        self.assertEqual(rows[1]["date"], "2026-08-11")
        self.assertEqual(rows[1]["app_name"], "chrome.exe")

        # 收窄范围到 08-11，只剩 Chrome 那一行
        only_11 = storage.get_summary_range("2026-08-11", "2026-08-11")
        self.assertEqual(len(only_11), 1)
        self.assertEqual(only_11[0]["app_name"], "chrome.exe")


if __name__ == "__main__":
    unittest.main()
