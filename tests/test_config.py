"""server/config.py 的 .env 读取测试。

只测纯函数，外加「ENV_FILE 有没有落在对的目录」这类元信息。**刻意不去改真的
os.environ**：测试进程里塞一个 TIMETRACE_* 出去，会顺着 setdefault 污染后面
导入 config 的其它测试，而这种失败看起来和 .env 毫无关系，最难查。
apply_env 允许传入自己的 dict，就是为了能这样测。
"""
import os
import unittest
from unittest import mock

import config


class TestParseEnvText(unittest.TestCase):
    def test_plain_key_value(self):
        self.assertEqual(config.parse_env_text("A=1\nB=two"),
                         {"A": "1", "B": "two"})

    def test_export_prefix_is_accepted(self):
        """从 shell 教程里抄来的写法不该报错。"""
        self.assertEqual(config.parse_env_text("export A=1"), {"A": "1"})

    def test_comments_and_blank_lines_are_ignored(self):
        text = "# 整行注释\n\n   \n   # 缩进的注释\nA=1\n"
        self.assertEqual(config.parse_env_text(text), {"A": "1"})

    def test_double_quotes_keep_the_spaces_inside(self):
        self.assertEqual(config.parse_env_text('A="  x y  "'), {"A": "  x y  "})

    def test_double_quotes_unescape(self):
        self.assertEqual(config.parse_env_text(r'A="a\nb"'), {"A": "a\nb"})
        self.assertEqual(config.parse_env_text(r'A="C:\\dir"'), {"A": "C:\\dir"})

    def test_single_quotes_do_not_unescape(self):
        """单引号按 shell 的规矩原样保留，否则 `'a\\nb'` 会被误伤。"""
        self.assertEqual(config.parse_env_text(r"A='a\nb'"), {"A": r"a\nb"})

    def test_inline_comment_needs_a_space_before_the_hash(self):
        self.assertEqual(config.parse_env_text("A=1 # 说明"), {"A": "1"})
        # 密码里的 # 不算注释 —— 砍错了就是「密码明明对却认证失败」
        self.assertEqual(config.parse_env_text("A=ab#cd"), {"A": "ab#cd"})

    def test_value_may_contain_equals(self):
        """按第一个 = 切分，base64 之类的尾巴不能被吃掉。"""
        self.assertEqual(config.parse_env_text("A=ab=="), {"A": "ab=="})

    def test_lines_without_equals_are_skipped_not_fatal(self):
        self.assertEqual(config.parse_env_text("JUSTAWORD\nA=1\n=2"), {"A": "1"})

    def test_empty_value_stays_empty(self):
        """空值原样是空串，于是 _env() 会退回默认值（见 _env 的说明）。"""
        self.assertEqual(config.parse_env_text("A="), {"A": ""})

    def test_keys_are_not_prefixed_automatically(self):
        """只认全名：配置项在 os.environ 里叫什么，这里就得写什么。"""
        self.assertEqual(config.parse_env_text("MYVAR=1"), {"MYVAR": "1"})

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(config.parse_env_text("  A  =  1  "), {"A": "1"})


class TestApplyEnv(unittest.TestCase):
    def test_missing_keys_are_filled(self):
        env = {}
        config.apply_env({"A": "1"}, env)
        self.assertEqual(env, {"A": "1"})

    def test_existing_keys_win(self):
        """真环境变量优先于 .env —— 模块开头第 1 条承诺的就是这个。"""
        env = {"A": "from-shell"}
        config.apply_env({"A": "from-dotenv", "B": "2"}, env)
        self.assertEqual(env, {"A": "from-shell", "B": "2"})


class TestEnvFile(unittest.TestCase):
    def test_env_file_sits_next_to_config(self):
        self.assertEqual(config.ENV_FILE,
                         os.path.join(os.path.dirname(os.path.abspath(config.__file__)),
                                      ".env"))

    def test_loaded_flag_matches_reality(self):
        self.assertEqual(config.ENV_FILE_LOADED, os.path.exists(config.ENV_FILE))

    def test_missing_file_is_not_an_error(self):
        """部署时可能根本不放 .env，配置全从真环境变量来。"""
        self.assertIsNone(config.load_env_file(os.path.join(os.path.dirname(config.ENV_FILE),
                                                            ".env.does-not-exist")))

    @unittest.skipUnless(config.ENV_FILE_LOADED, "本机没有 server/.env")
    def test_env_file_keys_are_all_known_names(self):
        """挡住最阴的一种写错：键名少个前缀，于是那项静默地永远用默认值。

        判定办法是不硬编码清单 —— 把 config.py 的源码当成「合法键名」的
        真相来源，.env 里出现一个源码里查不到的名字就报错。
        """
        self._assert_keys_are_known(config.ENV_FILE)

    def test_example_file_keys_are_all_known_names(self):
        """.env.example 是给人抄的模板，它写错等于把别人带进沟里。"""
        self._assert_keys_are_known(os.path.join(os.path.dirname(config.ENV_FILE),
                                                 ".env.example"))

    def _assert_keys_are_known(self, path):
        with open(path, "r", encoding="utf-8-sig") as f:
            keys = config.parse_env_text(f.read())
        self.assertTrue(keys, "%s 里一个配置项都没解析出来" % path)
        with open(config.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        for key in keys:
            with self.subTest(key=key):
                canonical = "TIMETRACE_" + key[len("USAGETIME_"):] if key.startswith("USAGETIME_") else key
                self.assertIn('"%s"' % canonical, source,
                              "%s 里的 %s 在 config.py 里查不到，可能是键名写错了"
                              % (os.path.basename(path), key))

    def test_env_file_is_gitignored(self):
        """.env 里是明文密码，被 add 进来就等于把生产密码发到 GitHub 上。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))
        with open(os.path.join(root, ".gitignore"), "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f]
        self.assertIn(".env", lines)


class TestPasswordHasNoHardcodedDefault(unittest.TestCase):
    def test_dotenv_value_reaches_the_constant(self):
        """整条链走一遍：.env 文本 → parse_env_text → apply_env → _env。"""
        env = {}
        config.apply_env(
            config.parse_env_text("TIMETRACE_DB_PASSWORD=s3cret # 注释"), env)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config._env("TIMETRACE_DB_PASSWORD", ""), "s3cret")

    def test_legacy_key_remains_supported_but_new_key_takes_priority(self):
        with mock.patch.dict(os.environ, {"USAGETIME_TEST_ALIAS": "old"}):
            self.assertEqual(config._env("TIMETRACE_TEST_ALIAS", "default"), "old")
            with mock.patch.dict(os.environ, {"PCTIME_TEST_ALIAS": "mid"}):
                self.assertEqual(config._env("TIMETRACE_TEST_ALIAS", "default"), "mid")
                with mock.patch.dict(os.environ, {"TIMETRACE_TEST_ALIAS": "new"}):
                    self.assertEqual(config._env("TIMETRACE_TEST_ALIAS", "default"), "new")

    def test_config_source_has_no_literal_password(self):
        """密码不该再有写死的默认值 —— 有的话 .env 就成了摆设。"""
        with open(config.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('"123456"', source)


if __name__ == "__main__":
    unittest.main()
