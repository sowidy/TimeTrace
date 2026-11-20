"""客户端 .env 支持的测试。

这个文件里最要紧的是 TestParsersAgree：客户端的解析器是**刻意**从服务端那份
复制来的（理由见 client/core/envfile.py 开头），代价就是两份实现可能悄悄漂移 ——
同一个 .env 在服务端和客户端解析出不同结果，而且症状极难查（一边好一边坏）。
那条测试把同一批输入同时喂给两份实现、要求结果逐字相同，就是给这个代价兜底。
"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

# tests/__init__.py 把 client/ 插在 sys.path 前面、server/ 追加在后面，
# 所以 `import config` 拿到的是 server/config.py，客户端的要写 core.config。
import config as server_config
from core import config as client_config
from core import envfile, paths


class TestParsersAgree(unittest.TestCase):
    """两份 .env 解析器必须逐字一致 —— 漂移了就在这里红。"""

    # 挑的都是两边写得不一样就会露馅的地方：引号、转义、注释、空值、异常行。
    CASES = [
        "A=1",
        "A=1\nB=two",
        "export A=1",
        "# 整行注释\n\n   \n   # 缩进的注释\nA=1\n",
        'A="  x y  "',
        r'A="a\nb"',
        r'A="C:\\dir"',
        r"A='a\nb'",
        "A=1 # 说明",
        "A=ab#cd",
        "A=ab==",
        "JUSTAWORD\nA=1\n=2",
        "A=",
        "MYVAR=1",
        "  A  =  1  ",
        # 引号优先于注释：`#` 夹在引号里时不能被当成注释砍掉。
        # 这个顺序是有讲究的，反过来写 `A="a # b"` 会被截成 `"a`。
        'A="a # b"',
    ]

    def test_parse_env_text_agrees(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(envfile.parse_env_text(text),
                                 server_config.parse_env_text(text))

    def test_apply_env_agrees(self):
        for mapping, existing in (({"A": "1"}, {}),
                                  ({"A": "dotenv", "B": "2"}, {"A": "shell"})):
            with self.subTest(mapping=mapping):
                client_env, server_env = dict(existing), dict(existing)
                envfile.apply_env(mapping, client_env)
                server_config.apply_env(mapping, server_env)
                self.assertEqual(client_env, server_env)


class TestClientEnvFile(unittest.TestCase):
    def test_candidates_collapse_in_source_mode(self):
        """program_dir() 和 app_data_dir() 在源码模式下是同一个目录，要去重。"""
        self.assertEqual(paths.env_file_candidates(),
                         [os.path.join(paths.program_dir(), ".env")])

    def test_candidates_in_frozen_mode_put_program_dir_first(self):
        """打包后 exe 旁边优先，%APPDATA% 兜底。

        frozen / sys.executable / app_data_dir 全部替换掉，免得真去碰
        %APPDATA%（测试不该往用户的用户目录里写东西）。
        """
        fake_exe = r"C:\Apps\TimeTrace\TimeTrace.exe"
        fake_data = r"C:\Users\x\AppData\Roaming\TimeTrace"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", fake_exe), \
                mock.patch.object(paths, "app_data_dir", lambda: fake_data):
            self.assertEqual(paths.program_dir(), r"C:\Apps\TimeTrace")
            self.assertEqual(paths.env_file_candidates(),
                             [r"C:\Apps\TimeTrace\.env",
                              os.path.join(fake_data, ".env")])

    def test_frozen_data_dir_prefers_legacy_database_even_if_new_directory_exists(self):
        with tempfile.TemporaryDirectory() as appdata:
            legacy = os.path.join(appdata, "UsageTracker")
            current = os.path.join(appdata, "TimeTrace")
            os.makedirs(legacy)
            os.makedirs(current)
            with open(os.path.join(legacy, "usage.db"), "wb") as db:
                db.write(b"old database")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                self.assertEqual(paths.app_data_dir(), legacy)
                with open(os.path.join(current, "usage.db"), "wb") as db:
                    db.write(b"new database")
                self.assertEqual(paths.app_data_dir(), current)

    def test_frozen_data_dir_prefers_newer_legacy_pctime_over_usagetracker(self):
        """两个旧目录都有数据库时，取较新的 PcTime（而不是最旧的 UsageTracker）。"""
        with tempfile.TemporaryDirectory() as appdata:
            old = os.path.join(appdata, "UsageTracker")
            mid = os.path.join(appdata, "PcTime")
            current = os.path.join(appdata, "TimeTrace")
            os.makedirs(old)
            os.makedirs(mid)
            os.makedirs(current)
            with open(os.path.join(old, "usage.db"), "wb") as db:
                db.write(b"oldest database")
            with open(os.path.join(mid, "usage.db"), "wb") as db:
                db.write(b"middle database")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                self.assertEqual(paths.app_data_dir(), mid)

    def test_frozen_new_install_uses_timetrace_directory(self):
        with tempfile.TemporaryDirectory() as appdata:
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                self.assertEqual(paths.app_data_dir(), os.path.join(appdata, "TimeTrace"))

    def test_no_env_file_returns_none(self):
        """客户端必须能在没有 .env 的情况下照常跑（发出去的包里不带它）。"""
        with mock.patch.object(paths, "env_file_candidates",
                               lambda: [os.path.join(paths.client_dir(),
                                                     ".env.does-not-exist")]):
            self.assertIsNone(envfile.load_env())

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(envfile.load_env_file(
            os.path.join(paths.client_dir(), ".env.does-not-exist")))

    def test_bad_int_falls_back_instead_of_crashing(self):
        """打包版是 console=False，导入期抛错等于「双击没反应」，用户看不到原因。"""
        with mock.patch.dict(os.environ, {"TIMETRACE_TEST_BAD": "abc"}):
            self.assertEqual(client_config._env_int("TIMETRACE_TEST_BAD", 7), 7)
        with mock.patch.dict(os.environ, {"TIMETRACE_TEST_GOOD": "9"}):
            self.assertEqual(client_config._env_int("TIMETRACE_TEST_GOOD", 7), 9)

    def test_empty_string_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"TIMETRACE_TEST_EMPTY": ""}):
            self.assertEqual(client_config._env("TIMETRACE_TEST_EMPTY", "兜底"),
                             "兜底")

    def test_legacy_env_key_is_fallback_but_new_key_wins(self):
        with mock.patch.dict(os.environ, {"USAGETIME_TEST_ALIAS": "old"}, clear=False):
            self.assertEqual(client_config._env("TIMETRACE_TEST_ALIAS", "default"), "old")
            with mock.patch.dict(os.environ, {"PCTIME_TEST_ALIAS": "mid"}):
                self.assertEqual(client_config._env("TIMETRACE_TEST_ALIAS", "default"), "mid")
                with mock.patch.dict(os.environ, {"TIMETRACE_TEST_ALIAS": "new"}):
                    self.assertEqual(client_config._env("TIMETRACE_TEST_ALIAS", "default"), "new")

    def test_label_recognises_program_dir(self):
        target = os.path.join(paths.program_dir(), ".env")
        with mock.patch.object(client_config, "ENV_FILE", target):
            self.assertEqual(client_config.env_file_label(), "程序目录下的 .env")

    def test_label_recognises_app_data_dir(self):
        """验「退到 %APPDATA% 那份」这一支。

        必须把 program_dir() 换成别的目录：源码模式下 program_dir() 和
        app_data_dir() 指向同一个 client/，否则命中的永远是「程序目录」那一支。
        把这一个函数换掉，正好还原出打包模式下的真实情形
        （exe 在别处、数据在 %APPDATA%）。
        """
        target = os.path.join(r"C:\Users\x\AppData\Roaming\TimeTrace", ".env")
        with mock.patch.object(paths, "program_dir", lambda: r"C:\Apps\TimeTrace"), \
                mock.patch.object(paths, "app_data_dir", lambda: os.path.dirname(target)), \
                mock.patch.object(client_config, "ENV_FILE", target):
            self.assertEqual(client_config.env_file_label(),
                             "%APPDATA%\\TimeTrace\\.env")

    def test_label_recognises_legacy_pctime_dir(self):
        """旧目录 PcTime 里的 .env，标签要认出来是 PcTime 那份。"""
        target = os.path.join(r"C:\Users\x\AppData\Roaming\PcTime", ".env")
        with mock.patch.object(paths, "program_dir", lambda: r"C:\Apps\TimeTrace"), \
                mock.patch.object(paths, "app_data_dir", lambda: os.path.dirname(target)), \
                mock.patch.object(client_config, "ENV_FILE", target):
            self.assertEqual(client_config.env_file_label(),
                             "%APPDATA%\\PcTime\\.env")

    def test_label_is_short_even_for_a_deep_path(self):
        """认不出的位置原样显示 —— 但绝不编一个可能是错的短标签。"""
        weird = os.path.join(r"C:\somewhere\else", ".env")
        with mock.patch.object(client_config, "ENV_FILE", weird):
            self.assertEqual(client_config.env_file_label(), weird)

    def test_no_label_without_env_file(self):
        with mock.patch.object(client_config, "ENV_FILE", None):
            self.assertIsNone(client_config.env_file_label())


class TestClientExampleFile(unittest.TestCase):
    def test_example_file_keys_are_all_known_names(self):
        """挡住最阴的一种写错：键名少个前缀，于是那项静默地永远用默认值。

        不硬编码清单 —— 把 core/config.py 的源码当成「合法键名」的真相来源。
        """
        path = os.path.join(paths.client_dir(), ".env.example")
        with open(path, "r", encoding="utf-8-sig") as f:
            keys = envfile.parse_env_text(f.read())
        self.assertTrue(keys, ".env.example 里一个配置项都没解析出来")
        with open(client_config.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        for key in keys:
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, source,
                              ".env.example 里的 %s 在 core/config.py 里查不到，"
                              "可能是键名写错了" % key)

    def test_defaults_are_unchanged(self):
        """.env 支持的这份改动不该动到任何一个默认值。

        **不能**直接断言 ``client_config.SERVER_HOST`` 的当前值：那样用户改了自己
        的 client/.env 之后这条测试就会变红，而改 .env 恰恰是这个功能存在的意义。
        所以要在一个「没有 .env、也没有 TimeTrace/旧版环境变量」的干净环境里重新
        导入一次，验完再导入回来。
        """
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("TIMETRACE_", "USAGETIME_"))}
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(paths, "env_file_candidates", lambda: []):
            importlib.reload(client_config)
            try:
                self.assertIsNone(client_config.ENV_FILE)
                self.assertEqual(client_config.SERVER_HOST, "127.0.0.1")
                self.assertEqual(client_config.SERVER_PORT, 8000)
                self.assertEqual(client_config.REQUEST_TIMEOUT, 15)
                self.assertEqual(client_config.RETRY_COUNT, 3)
                self.assertEqual(client_config.RETRY_BACKOFF, 2.0)
                self.assertEqual(client_config.UPLOAD_CHECK_INTERVAL, 60 * 60)
                self.assertEqual(client_config.RESEND_RECENT_DAYS, 3)
                self.assertEqual(client_config.CLIENT_VERSION, "1.0.2")
                self.assertEqual(client_config.API_PREFIX, "/api/v1")
            finally:
                # 必须还原：reload 是原地重执行，模块对象还是同一个，
                # 别的测试文件（test_uploader.py 里 `from core import config`）
                # 拿到的就是它。留着干净环境的状态会污染它们。
                importlib.reload(client_config)


if __name__ == "__main__":
    unittest.main()
