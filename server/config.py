"""服务端配置：数据库连接、监听地址、管理口令。

取值顺序，先命中的赢：

1. **真正的环境变量** —— 命令行 `set`、docker `-e`、systemd `Environment=`；
2. **同目录的 `.env` 文件**（模板见 `.env.example`），本模块导入时自动读；
3. **下面代码里的默认值** —— 不含任何密码，够本机跑起来而已。

所以日常改配置**改 `.env` 就够了**，不用碰代码，也不用每次启动都带一长串 set：

    PCTIME_DB_HOST=127.0.0.1
    PCTIME_DB_PASSWORD=......
    PCTIME_ADMIN_TOKEN=......

第 1 条压过第 2 条是刻意的，不是顺序凑巧：临时试一个值还是 `set` 更顺手，
而要是 .env 反过来能盖掉它，就会变成「明明 set 了却不生效」这种最难查的故障。

⚠️ `.env` 是明文存密码的地方，`load_env_file` 只负责读它，不负责加密。
仓库的 .gitignore 已经忽略它了 —— 别用 `git add -f` 硬塞进去。

⚠️ 改完要**重启服务端**才生效：下面全是导入期算一次的模块级常量。
"""
import os


def _env(name, default):
    """读环境变量；没设置或设成空字符串时返回默认值。

    空字符串也当「没设置」，是因为 set VAR= 在 Windows 上很常见，
    那时候我们想要的是默认值，不是空值。
    """
    value = os.environ.get(name)
    if (value is None or value == "") and name.startswith("PCTIME_"):
        value = os.environ.get("USAGETIME_" + name[len("PCTIME_"):])
    return default if value is None or value == "" else value


# ── .env 加载 ─────────────────────────────────────────────────
#
# 为什么不用 python-dotenv：它做的事就是下面这几十行，而服务端一共只装
# fastapi / uvicorn / pymysql / cryptography 四个包，为了读一个自己就能解析的
# 文本格式再多引一个依赖不划算 —— 还得为它多写一行 requirements。
# 代价是语法支持得比它窄：不处理多行值、`${OTHER}` 变量插值，以及没有 `=` 的裸键行。

# .env 和本文件放一起，跟着 server/ 目录走，不用管启动时 cwd 在哪。
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

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

    要求 # 前面有个空格。这样 `PASSWORD=abc#def` 里的 # 才不会被误当注释 ——
    密码里带 # 并不罕见，砍错了就是「密码明明对却认证失败」。
    """
    hash_at = value.find(" #")
    return (value[:hash_at] if hash_at >= 0 else value).rstrip()


def parse_env_text(text):
    """把 .env 的内容解析成 {键: 值} 字典。

    认这几种写法，其余的行**直接跳过**：.env 是给人手写的，敲错一行不该让
    整个服务端起不来（那条配置会退回默认值，被下面的 ENV_FILE_LOADED 日志反衬出来）。

        KEY=value          PCTIME_DB_HOST=127.0.0.1
        export KEY=value   export PCTIME_DB_HOST=127.0.0.1   （照抄 shell 写法也不会错）
        KEY="value"        双引号：值里的空格保留，支持 \\n \\t \\\\ \\" 转义
        KEY='value'        单引号：值原样保留，一个转义都不做（和 shell 一致）
        # 注释、空行

    键名**不**自动补 `PCTIME_` 前缀：这里只认全名，和 os.environ 里对得上。
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

    用 setdefault 而不是 `env[k] = v`，就是为了让真环境变量保持优先
    （见模块开头第 1 条）。想验证的话：`set PCTIME_DB_HOST=X` 之后
    .env 里写别的值也不会生效。
    """
    env = os.environ if environ is None else environ
    for key, value in mapping.items():
        env.setdefault(key, value)


def load_env_file(path=None):
    """读 .env 并生效。返回读到的路径；文件不存在返回 None（不算错误）。

    「文件不存在」是正常情况而不是异常：打包部署时可能根本不放 .env，
    配置全从真环境变量来。
    """
    path = ENV_FILE if path is None else path
    try:
        # utf-8-sig 而不是 utf-8：记事本存 UTF-8 会加 BOM，不处理的话第一个
        # 键名会悄悄带上不可见的 ﻿，于是那一项就永远配不上、只会用默认值。
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    apply_env(parse_env_text(text))
    return path


ENV_FILE_LOADED = load_env_file() is not None


# ── 数据库 ────────────────────────────────────────────────────

DB_HOST = _env("PCTIME_DB_HOST", "127.0.0.1")
DB_PORT = int(_env("PCTIME_DB_PORT", "3306"))
DB_USER = _env("PCTIME_DB_USER", "pctime")
DB_NAME = _env("PCTIME_DB_NAME", "pctime")

# 密码**没有**默认值，只能从环境变量或 .env 来。这是这次改动里唯一一处
# 默认值的变化：以前这里写着明文口令，等于把生产密码写死在代码里。
# 留空的表现是连不上（pymysql 报 Access denied），很吵，正好。
# 真实的那个值在 server/.env（已被 .gitignore 忽略）。
DB_PASSWORD = _env("PCTIME_DB_PASSWORD", "")

# utf8mb4 而不是 utf8：MySQL 的 "utf8" 其实只有 3 字节，存不下 emoji 和
# 一部分生僻字。应用显示名里出现这些字符时，"utf8" 会直接报错或截断。
DB_CHARSET = "utf8mb4"

# Existing installations can initialize with a DDL account once, then run with
# SELECT/INSERT/UPDATE/DELETE only. Missing tables remain a startup error.
DB_INIT_SCHEMA = _env("PCTIME_DB_INIT_SCHEMA", "1").lower() not in ("0", "false", "no")

# ⚠️ 用 root 连数据库是能跑，但不应该长期这样：
#    root 对整台 MySQL 有全部权限，一旦服务端被攻破（比如接口有注入漏洞），
#    攻击者拿到的不只是 pctime 这个库，而是整台数据库服务器。
#    正确做法是建一个只对本库有增删改查权限的专用账号：
#
#        CREATE USER 'pctime'@'%' IDENTIFIED BY '<强密码>';
#        GRANT SELECT, INSERT, UPDATE, DELETE ON pctime.* TO 'pctime'@'%';
#
#    然后 PCTIME_DB_USER / PCTIME_DB_PASSWORD 指向它。


# ── HTTP 监听 ─────────────────────────────────────────────────

# 0.0.0.0 表示监听所有网卡，局域网/公网才能访问到。
# 只想本机访问就改成 127.0.0.1。
HOST = _env("PCTIME_HOST", "127.0.0.1")

# 3306 被 MySQL 占了，所以服务端用 8000。
# ⚠️ 这个端口必须在服务器的安全组/防火墙里放行，否则客户端连不上——
#    如果需要外部直连，还需检查安全组和防火墙；反向代理部署时按需开放代理端口。
PORT = int(_env("PCTIME_PORT", "8000"))


# ── 管理接口口令 ──────────────────────────────────────────────

# 「列出所有用户」这类管理接口要带上 X-Admin-Token 请求头，值就是它。
# 留空 = 管理接口直接关闭（返回 403）。默认留空是故意的：
# 宁愿管理接口不能用，也不要让它默认裸奔在公网上。
ADMIN_TOKEN = _env("PCTIME_ADMIN_TOKEN", "")


# ── 上传接口的限制 ────────────────────────────────────────────

# 单次上传最多允许几天、每天最多几个应用。用来防止有人构造一个超大请求
# 把内存/数据库打满。正常客户端一次只传「补传的这几天」，远低于这两个数。
MAX_DAYS_PER_UPLOAD = int(_env("PCTIME_MAX_DAYS", "400"))
MAX_APPS_PER_DAY = int(_env("PCTIME_MAX_APPS", "2000"))


# ── 客户端版本 ────────────────────────────────────────────────

# 客户端上传时会带自己的版本号，服务端不用管；这里定义的是「服务端认为
# 最低可用的客户端版本」，目前只是记录下来，没有强制校验。
MIN_CLIENT_VERSION = "1.0.0"


# ── 每日邮件推送 ──────────────────────────────────────────────
#
# 这一块默认是**关闭**的：没有 SMTP_HOST 就没有「发信人」，硬发只会报错，
# 所以没配就整体不启用，服务端行为跟以前完全一样。
#
#    PCTIME_SMTP_HOST=smtp.qq.com
#    PCTIME_SMTP_USER=you@qq.com
#    PCTIME_SMTP_PASSWORD=<授权码，不是登录密码>
#    PCTIME_MAIL_FROM=you@qq.com
#
# ⚠️ 注意腾讯/网易这类邮箱的 SMTP 密码是「授权码」，要在邮箱设置里单独开
#    并生成，不是账号登录密码。填登录密码会一直认证失败。

SMTP_HOST = _env("PCTIME_SMTP_HOST", "")
SMTP_USER = _env("PCTIME_SMTP_USER", "")
SMTP_PASSWORD = _env("PCTIME_SMTP_PASSWORD", "")

# 465：连上就加密（SMTP_SSL）。587：先明文握手再 STARTTLS 升级。
# 默认按端口自动判断（587 走 STARTTLS，其它端口走 SSL），因为这两个端口号
# 本身就绑定了各自的加密方式；真想覆盖再用 PCTIME_SMTP_SSL=0/1。
SMTP_PORT = int(_env("PCTIME_SMTP_PORT", "465"))
_SMTP_SSL_ENV = _env("PCTIME_SMTP_SSL", "").lower()
SMTP_USE_SSL = (
    _SMTP_SSL_ENV not in ("0", "false", "no")
    if _SMTP_SSL_ENV
    else SMTP_PORT != 587
)

# 发信人。留空就用 SMTP_USER（绝大多数邮箱要求这两者一致，否则被拒）。
MAIL_FROM = _env("PCTIME_MAIL_FROM", "") or SMTP_USER
MAIL_FROM_NAME = _env("PCTIME_MAIL_FROM_NAME", "TimeTrace 使用报告")

# 配了 SMTP_HOST 才算启用。PCTIME_MAIL_ENABLED=0 可以强制关掉。
MAIL_ENABLED = bool(SMTP_HOST and MAIL_FROM) and \
    _env("PCTIME_MAIL_ENABLED", "1").lower() not in ("0", "false", "no")

# 进程内定时器（每天到点自己发）。关掉它但保留邮件功能，是为了支持
# 「用外部 cron 定时调 POST /api/v1/admin/daily-mail」这种部署方式 ——
# 那时进程内再来一个定时器就重复了。
MAIL_SCHEDULER_ENABLED = MAIL_ENABLED and \
    _env("PCTIME_MAIL_SCHEDULER", "1").lower() not in ("0", "false", "no")

# 每天几点发。默认次日 00:03，给客户端保留 3 分钟完成前一天最后的数据上报。
MAIL_SEND_TIME = _env("PCTIME_MAIL_TIME", "00:03:00")

# Additional mail sections are independent and gracefully degrade on missing data.
MAIL_TRACKING_ENABLED = _env("PCTIME_MAIL_TRACKING", "1").lower() not in ("0", "false", "no")

# 补发窗口：服务器半夜重启/宕机导致某天没发出去时，允许在后续几次运行里
# 把它补上。见 mail/scheduler.py 里关于「触发日与报告日不同」的说明。
MAIL_CATCHUP_DAYS = int(_env("PCTIME_MAIL_CATCHUP_DAYS", "2"))

# 一个星期里哪一天发周报，取值 0..6 对应**周一..周日**（跟 date.weekday() 的
# 编号一致，不是 isoweekday() 的 1..7）。默认 6 = 周日。
#
# 那天发的是「以该天结尾的 7 天」的聚合 —— 默认就是周一..周日；其余六天各发
# 一封当天的日报。所以**一周里每天恒定一封**。
#
# 正因为「报告种类」是这一天的纯函数，mail_log 的 UNIQUE KEY (user_key, day)
# 依然够用：周日那天只可能长出一封周报，不会有第二封日报来撞这个键。
# 见 mail/__init__.py 里那段论证。
#
# ⚠️ 别在周中改这个值：那天的记录可能已经被日报占了键，改完之后那一周的周报
#    会被 was_mail_sent 挡掉、发不出去。真要改就在两次发送之间改。
MAIL_WEEKLY_DAY = int(_env("PCTIME_MAIL_WEEKLY_DAY", "6"))

# 纯文本备用版本的网站明细上限；网站图表按时长动态展开。
MAIL_MAX_APPS = int(_env("PCTIME_MAIL_MAX_APPS", "60"))
MAIL_MAX_SITES = int(_env("PCTIME_MAIL_MAX_SITES", "40"))
# Largest categories shown separately in stacked charts; remaining time is Other.
MAIL_CHART_SERIES = max(1, min(12, int(_env("PCTIME_MAIL_CHART_SERIES", "8"))))

# 图表中文标签要用的中文字体文件。留空时自动找：Windows 用微软雅黑，
# Linux 找 Noto CJK / 文泉驿；找不到就回退到英文键名。
MAIL_CHART_CJK_FONT = _env("PCTIME_MAIL_CHART_CJK_FONT", "")

# 单次 SMTP 操作的超时（秒）。设短一点，免得某个收件方的服务器卡住时
# 把整个发送线程一直吊在那里。
SMTP_TIMEOUT = int(_env("PCTIME_SMTP_TIMEOUT", "20"))
