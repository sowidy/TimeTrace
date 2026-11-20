"""读客户端的 `.env` 配置文件。

和 server/config.py 里那份是**同一套语法、刻意保留的两份实现**（不是一个
共享模块）。这么选的理由是打包：客户端的模块搜索路径就是 client/，一旦要
引用 client/ 外面的目录就得改 app.spec 的 pathex，而那种错只在「重新打包后的
exe」上才暴露，源码模式反而一切正常。几十行稳定的解析代码，换打包链路上少
一个失败点，划算。

代价是两边改语法要记得改两处 —— `tests/test_envfile.py` 里有一条测试把同一个
输入喂给两份实现、要求结果逐字相同，就是用来兜住这个代价的。

用法（只在 core/config.py 里调用一次，导入期生效）：

    path = load_env()

支持的写法：
    KEY=value          TIMETRACE_SERVER_HOST=127.0.0.1
    export KEY=value   export TIMETRACE_SERVER_HOST=127.0.0.1
    KEY="value"        双引号：保留空格，认 \\n \\t \\\\ \\" 转义
    KEY='value'        单引号：原样保留，一个转义都不做
    # 注释、空行

不支持多行值、`${OTHER}` 变量插值，以及没有 `=` 的裸键行（这些行被跳过）。
"""
import os

from core import paths

_DOUBLE_QUOTE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


def _unescape(value):
    """处理双引号里的 \\n \\t \\\\ 这类转义（单引号里不转，见 parse_env_text）。"""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in _DOUBLE_QUOTE_ESCAPES:
            out.append(_DOUBLE_QUOTE_ESCAPES[value[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_comment(value):
    """无引号的值：把行尾的 ` # 注释` 砍掉。

    要求 # 前面有个空格，这样 `KEY=ab#cd` 里的 # 才不会被误当注释。
    """
    hash_at = value.find(" #")
    return (value[:hash_at] if hash_at >= 0 else value).rstrip()


def parse_env_text(text):
    """把 .env 的内容解析成 {键: 值} 字典。

    认不出来的行**直接跳过**而不是报错：.env 是给人手写的，敲错一行不该让
    客户端起不来（那一项退回默认值而已）。键名要写全，不自动补前缀。
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            inner = value[1:-1]
            out[key] = _unescape(inner) if value[0] == '"' else inner
        else:
            out[key] = _strip_comment(value)
    return out


def apply_env(mapping, environ=None):
    """把键值灌进环境变量，**已经存在的键一律不动**。

    用 setdefault 而不是 `env[k] = v`：真环境变量优先，这样临时试一个值
    用 `set TIMETRACE_SERVER_HOST=...` 仍然有效。
    """
    env = os.environ if environ is None else environ
    for key, value in mapping.items():
        env.setdefault(key, value)


def load_env_file(path):
    """读一个指定的 .env 并生效。返回路径；文件不存在返回 None（不算错误）。"""
    try:
        # utf-8-sig 而不是 utf-8：记事本存 UTF-8 会加 BOM，不处理的话第一个
        # 键名会悄悄带上不可见的 ﻿，那一项就永远配不上、只会用默认值。
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    apply_env(parse_env_text(text))
    return path


def load_env():
    """按 core/paths.py 给的优先级找 .env，读第一个存在的。

    返回真正用到的路径，一个都没有就返回 None —— 客户端**必须**能在没有
    .env 的情况下照常跑（打包发出去的那份里不会带 .env），所以这里不报错。
    """
    for candidate in paths.env_file_candidates():
        if load_env_file(candidate):
            return candidate
    return None
