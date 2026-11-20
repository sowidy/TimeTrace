"""客户端的运行时配置：服务端地址、上传节奏、接口路径。

这里只放「配置项」，不放业务逻辑。真正的上传逻辑在 net/uploader.py。
地址、端口这些集中在本文件，改服务器改这一处就行 —— 或者更省事：改 `.env`
（见下），连本文件都不用动。

取值顺序，先命中的赢：

1. **真正的环境变量** —— 命令行 set、快捷方式里塞的环境变量；
2. **`.env` 文件** —— 候选位置见 core/paths.py 的 env_file_candidates()：
   先找程序目录（打包后是 exe 旁边，跑源码时是 client/），找不到再退到
   数据目录（%APPDATA%\\TimeTrace\\；旧安装仍能读取原目录）；
3. **下面代码里的默认值**。

⚠️ **没有 .env 是正常情况**：发出去的压缩包里不会带它，客户端必须能这样照常
跑。所以 .env 是「想改才写」，不是运行前提。

⚠️ 全是导入期算一次的模块级常量，改完要**重启客户端**才生效。
"""
import os

from core import envfile, paths

# 导入期就把 .env 灌进 os.environ，下面那些 _env() 才取得到。
# ENV_FILE 记着实际用的是哪一份（没有就是 None）—— 设置页会显示它。
# 为什么要显示：客户端没有日志文件，「我改了一份 .env 却没生效」这种情况
# 只能靠「程序读的到底是哪个文件」来定位。
ENV_FILE = envfile.load_env()


def env_file_label():
    """`ENV_FILE` 的短描述，给界面显示用；没有 .env 时返回 None。

    为什么不直接把 `ENV_FILE` 显示出来：一个完整的
    `D:\\Develop\\...\\client\\.env` 在设置页里会折成两三行，而用户真正
    需要一眼看懂的只有一件事 —— 读的是「程序旁边那份」还是「%APPDATA% 那份」。
    完整路径挂在控件的 tooltip 上，想看细节也看得到。
    """
    if not ENV_FILE:
        return None
    folder = os.path.dirname(ENV_FILE)
    if folder == paths.program_dir():
        return "程序目录下的 .env"
    if folder == paths.app_data_dir():
        if folder.endswith("TimeTrace"):
            return "%APPDATA%\\TimeTrace\\.env"
        if folder.endswith("PcTime"):
            return "%APPDATA%\\PcTime\\.env"
        return "%APPDATA%\\UsageTracker\\.env"
    # 正常情况下到不了这里（ENV_FILE 只可能来自那两个候选位置）。
    # 真到了就原样显示，不要编一个可能是错的短标签。
    return ENV_FILE


def _env(name, default):
    """读环境变量；没设置或设成空字符串时返回默认值。

    空字符串也当「没设置」，和服务端 config.py 保持一致：set VAR= 在
    Windows 上很常见，那时候我们想要的是默认值，不是空值。
    """
    value = os.environ.get(name)
    if (value is None or value == "") and name.startswith("TIMETRACE_"):
        # 兼容旧键：先 TIMETRACE_*，再 PCTIME_*，最后 USAGETIME_*。
        suffix = name[len("TIMETRACE_"):]
        value = os.environ.get("PCTIME_" + suffix)
        if value is None or value == "":
            value = os.environ.get("USAGETIME_" + suffix)
    return default if value is None or value == "" else value


def _env_int(name, default):
    """读整数配置；值不是数字就退回默认值，**不抛异常**。

    为什么要吞掉错误：打包版是 console=False，导入期抛异常的后果是
    「双击了没反应」——用户看不到任何提示，也没法截图给我看。
    退回默认值至少程序能起来，而设置页显示着 .env 的路径，能顺着查到。
    """
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name, default):
    """读浮点配置；坏值退回默认值。理由同 _env_int。"""
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# ── 服务端 ──

# 数据上传的目标服务器。改地址只改这里一处，或者写进 .env 的
# TIMETRACE_SERVER_HOST。
SERVER_HOST = _env("TIMETRACE_SERVER_HOST", "127.0.0.1")

# 服务端（FastAPI / uvicorn）的监听端口。
# 注意 3306 是 MySQL 自己的端口，不是这个；服务端跑在 8000。
SERVER_PORT = _env_int("TIMETRACE_SERVER_PORT", 8000)

# Origin/base path only: /api/v1 is appended by endpoint helpers.
SERVER_URL = _env("TIMETRACE_SERVER_URL", "")

# 接口路径。服务端把版本前缀写在路径里（/api/v1/...），
# 以后接口有不兼容的改动时可以开 /api/v2 而不影响老客户端。
#
# 这一组**刻意不做成可配置的**：它是客户端和服务端之间的协议细节，
# 不是某个部署的环境差异。真要改（开 v2）就得两边一起改代码，
# 光改一边的 .env 只会让客户端连不上。
API_PREFIX = "/api/v1"
UPLOAD_PATH = API_PREFIX + "/upload"
HEALTH_PATH = API_PREFIX + "/health"
# 订阅接口。路径后面还要接一个 /{设备ID}，所以这里只存前缀，
# 拼接放在 subscription_url() 里做。
SUBSCRIPTION_PATH = API_PREFIX + "/subscription"

# 单次请求的超时（秒）。上传的数据量很小（一天几十行），
# 所以这个值主要是在「服务器不可达」时决定多久放弃，不用给太长。
REQUEST_TIMEOUT = _env_int("TIMETRACE_REQUEST_TIMEOUT", 15)

# 上传失败后的重试次数和退避间隔（秒）。
# 每次失败后等 RETRY_BACKOFF * 2^n 秒再试，避免服务器刚重启时
# 所有客户端一起猛冲。
RETRY_COUNT = _env_int("TIMETRACE_RETRY_COUNT", 3)
RETRY_BACKOFF = _env_float("TIMETRACE_RETRY_BACKOFF", 2.0)


def base_url():
    """服务端根 URL，支持 HTTPS、IPv6 和反向代理路径前缀。"""
    if SERVER_URL:
        from urllib.parse import urlsplit
        value = SERVER_URL.strip().rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("TIMETRACE_SERVER_URL 必须是无凭据、无 query 的 HTTP(S) 根 URL")
        parsed.port  # validate port
        if parsed.path.rstrip("/").endswith(API_PREFIX):
            value = value[:-len(API_PREFIX)]
        return value
    return f"http://{SERVER_HOST}:{SERVER_PORT}"


def secure_server():
    """供界面标识 HTTPS 或本机回环连接，不限制已启用的 HTTP 上传。"""
    from urllib.parse import urlsplit
    import ipaddress
    parsed = urlsplit(base_url())
    if parsed.scheme == "https":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname == "localhost"


def upload_url():
    """上传接口的完整 URL。"""
    return base_url() + UPLOAD_PATH


def subscription_url(user_key):
    """某个设备的订阅设置接口的完整 URL。

    user_key 会被 URL 转义：正常情况下它是个 UUID（不需要转义），
    但这个值是本地文件里的，理论上可能被改动过，转义一下免得拼出一个
    语义完全不同的 URL。
    """
    from urllib.parse import quote
    return f"{base_url()}{SUBSCRIPTION_PATH}/{quote(str(user_key), safe='')}"


# ── 上传节奏 ──

# 后台线程多久检查一次该不该上传（秒）。
# 需求是「每日一次」，但检查频率不等于上传频率：真正决定「今天传过没有」
# 的是 receipts 里的日期水位线（见 uploader.py）。这里 1 小时检查一次，
# 是为了让「程序刚开机就把昨天补传上去」这件事尽快发生，
# 而不是要每小时都传一次。
UPLOAD_CHECK_INTERVAL = _env_int("TIMETRACE_UPLOAD_CHECK_INTERVAL", 60 * 60)

# ── 客户端标识 ──

# 客户端自己的版本号，会跟着上传一起报给服务端，
# 方便服务端以后做「老版本客户端要升级」这类判断。
#
# 同样是刻意不可配置的：它描述的是「这份 exe 是哪个版本编译出来的」，
# 是事实而不是偏好。服务端会拿它和 MIN_CLIENT_VERSION 比对，
# 让用户随手改掉反而会让这个判断失效。
CLIENT_VERSION = "1.0.2"

# 再传一次已经传过的天数（天）。用于容忍「服务端换了库/数据被清了」
# 这种情况：每次上传会顺带把这几天重传一遍，成本极低但能自愈。
RESEND_RECENT_DAYS = _env_int("TIMETRACE_RESEND_RECENT_DAYS", 3)
