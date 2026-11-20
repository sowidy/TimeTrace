"""把本机每天的使用记录上传到服务端。

在整个项目中的位置：
    本模块只依赖 core.storage（取数据）、core.config（地址）和 core.paths 之外
    的标准库，刻意不导入 PySide6 —— 和 exporter.py 一样，上传是在后台线程里
    跑的，可能在界面创建之前就先跑了一次。不碰 Qt 就没有线程安全的问题。

数据从哪来：
    直接用 storage.get_summary_range()，不经过 exporter 导出的 CSV。
    理由：CSV 是「给用户看的另一种呈现」，它的列名、格式都可能为了好看而调整；
    让上传跟着 CSV 走，等于把「内部数据格式」和「展示格式」绑在一起，
    以后 CSV 加一列备注，上传就得跟着改。直接读库更直接，也少一次落盘再读回。

上传的是什么：
    一天一块，块里是这个应用的列表：
        {"day": "2026-10-14",
         "apps": [{"app_name": "pycharm64.exe", "display_name": "PyCharm",
                   "seconds": 7200.5, "launches": 12}]}
    服务端按 (user_key, day) 整块覆盖写入，所以重复上传同一天不会累加时长。

★ 关于「上传哪天」——水位线 + 回看窗口：

    和导出一样用一条「水位线」（存在 settings 表里）记着传到哪天了，但**不能**
    只靠它：导出是「过去的日子不会再变」，导出完就可以把水位线推过去；而上传
    面对的是**今天**，今天的时长还在长。如果传完今天就把水位线推到今天，
    那今天剩下的几个小时就再也不会被传了。

    所以这里传的是两类日期的并集：
        ① 水位线之后的新日子（补传：程序可能关了一周才开）
        ② 最近 RESEND_RECENT_DAYS 天（刷新：今天的数据还在变；
           顺带能自愈「服务端换了库、数据被清了」这种情况）
    重叠部分靠服务端的整块覆盖去掉，重复传不会出错，只会多花一点点流量。

★ 关于隐私：应用汇总默认开启自动上传；网站和应用时段由 tracking_upload
    独立控制，默认关闭。自动上传关闭后不在后台联网，手动上传只发送已授权的数据集。
"""

import json
import platform
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

from core import config
from core.timeutil import beijing_now, beijing_today
from core.storage import (
    get_available_dates,
    get_setting,
    get_summary_range,
    set_setting,
)

# ── settings 表里用到的键 ─────────────────────────────────────

# 本机的匿名身份。首次运行生成一个 UUID 后一直沿用，是服务端区分用户的唯一依据。
# ⚠️ 它同时充当访问凭据（详见 server/app.py 顶部的说明）：拿到这个 UUID 就能
#    读写该用户的数据，所以不要把它发给别人、也不要贴到公开的地方。
_USER_KEY_KEY = "user_key"
# 订阅邮箱。可空 —— 空着就是纯匿名使用，不传 email 字段。
_EMAIL_KEY = "email"
# 是否订阅每日报告邮件："1" 开 / "0" 关。
#
# 和「自动上传」是**两件事**，刻意分开：
#   上传开关管的是「数据要不要出本机」；
#   订阅开关管的是「数据到了服务器之后，要不要再发一封信到你邮箱」。
# 关掉订阅不该影响上传，关掉上传也不该悄悄把订阅退掉。
_SUBSCRIBE_KEY = "subscribe_enabled"
# 「本机的订阅设置还没同步到服务端」的标记："1" 待同步 / "0" 两边一致。
#
# 为什么需要这个标记：上传接口只在**第一次插入** users 行时按「有没有邮箱」
# 定 subscribed，之后不再碰（这样用户退订后不会被每天的上传自动订回来，
# 见 server/db/users.py 的 upsert_user）。代价是「先订阅、后上传」和「离线时
# 改订阅」这两种情况，光靠上传同步不过去，得单独补一次。
_SUBSCRIBE_DIRTY_KEY = "subscribe_dirty"
# 是否开启自动上传："1" 开 / "0" 关
_ENABLED_KEY = "upload_enabled"
# 水位线：已经成功上传到哪一天（ISO 字符串）
_WATERMARK_KEY = "upload_watermark"
# 最近一次上传成功的时间，只用于界面上显示
_LAST_SUCCESS_KEY = "upload_last_success"

# 单次请求最多带几天。服务端允许 400 天，但没必要一次塞那么多：
# 一次请求越短，失败重试的代价越小，出问题时也更容易定位是哪几天。
# 首次运行要补传全部历史时会按这个粒度自动分批。
MAX_DAYS_PER_REQUEST = 31

# 同一个进程里串行化上传，避免「后台定时器正在传」时用户又点了「立即上传」，
# 两个线程同时改水位线。跨进程（用户开了两个客户端）没法用线程锁挡住，
# 但那种情况下两边传的内容也基本一样，最坏结果是重复上传同一天，无害。
_upload_lock = threading.Lock()


# ── 身份 ──────────────────────────────────────────────────────

def get_or_create_user_key():
    """取本机的匿名 ID；第一次调用时生成并存下来。

    uuid4 是完全随机的（122 位），不包含机器名、网卡 MAC 之类的信息，
    所以它本身不泄露「这是谁」。用 uuid4 而不是 uuid1 正是因为这个：
    uuid1 里含 MAC 地址和时间戳，等于把机器指纹报给了服务端。
    """
    key = get_setting(_USER_KEY_KEY)
    if key:
        return key
    key = str(uuid.uuid4())
    set_setting(_USER_KEY_KEY, key)
    return key


def reset_user_key():
    """换一个新的身份。

    用途：用户不想让之后的数据和之前的数据关联起来时。
    注意这不会删除服务端已有的数据 —— 旧 key 下的记录还在那儿，
    而且服务端认 key 不认人，没有任何办法把两边重新对上。
    """
    key = str(uuid.uuid4())
    set_setting(_USER_KEY_KEY, key)
    # 换了身份，水位线必须一起清掉：新 key 在服务端一天数据都没有，
    # 留着旧水位线会让「水位线之前的历史」永远补不上去。
    set_setting(_WATERMARK_KEY, "")
    return key


def get_email():
    """订阅邮箱，没有就返回 None（而不是空串）。"""
    v = (get_setting(_EMAIL_KEY) or "").strip()
    return v or None


def set_email(value):
    """把邮箱存到本地设置里。

    注意这个函数**不发网络请求**：它只管本机记着用户填了什么，
    真正同步给服务端的是 update_subscription()。分开是因为两者会失败
    得不一样 —— 本地记得住，网络可能不通。
    """
    set_setting(_EMAIL_KEY, (value or "").strip())


def is_subscribed():
    """本机是不是「订阅了每日报告」。"""
    return get_setting(_SUBSCRIBE_KEY, "0") == "1"


def set_subscribed(flag):
    set_setting(_SUBSCRIBE_KEY, "1" if flag else "0")


def looks_like_email(value):
    """邮箱的「形状」检查，规则和 server/api/schemas.py 里的**完全一致**。

    故意不写完整正则：合法邮箱地址比大多数人想象的怪得多，严格的
    正则很容易误杀真实地址。而且客户端这边必须和服务端用同一套规则 ——
    客户端更严会把合法地址挡在外面（用户没救），更松则会把注定被
    422 拒掉的东西发出去（用户看到一句看不懂的报错）。
    真要确认邮箱可用，唯一的办法是往它发一封信，那是服务端的事。
    """
    v = (value or "").strip()
    if not v:
        return False
    return "@" in v and not v.startswith("@") and not v.endswith("@")


def subscription_state():
    """本机记着的订阅状态，给界面显示用。

    这是**本地的**副本，不是从服务端读回来的 —— 服务端没有「查自己订阅设置」
    这个公开接口（要读得走管理口令）。两边理论上可能不一致（比如换了台机器
    但沿用了同一个设备 ID），那种情况下重新保存一次就对齐了。
    """
    return {"email": get_email(), "subscribed": is_subscribed()}


def _machine_info():
    """上报给服务端的机器信息，纯粹是为了在后台能认出「这是哪台机器」。

    刻意只报操作系统和主机名，不报用户名、不报 IP —— 这些信息对「按用户
    区分数据」这个目标没有用，多报一份就多一份泄露风险。
    """
    hostname = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        pass  # 极少数环境拿不到主机名，空着即可，不影响上传
    return {
        "hostname": hostname[:255],
        # 例如 "Windows 11"，不带具体版本号，够用了
        "platform": f"{platform.system()} {platform.release()}"[:64],
        "client_version": config.CLIENT_VERSION,
    }


# ── 决定这次要传哪些天 ────────────────────────────────────────

def _watermark():
    """读水位线，返回 date 或 None（从没传过 / 值坏掉了）。"""
    raw = get_setting(_WATERMARK_KEY) or ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        # 被手工改坏了。当没传过处理，最坏结果是重传一遍历史，不会丢数据。
        return None


def pending_days(today=None):
    """算出这次该传哪些天，返回从小到大排好序的 date 列表。

    水位线之后的新日子 + 最近 RESEND_RECENT_DAYS 天，取并集（见模块开头的
    「关于上传哪天」）。只返回本地确实有数据的天 —— 没数据的天不传，
    因为服务端那边「传了但没有应用」会被当成异常跳过。
    """
    today = today or beijing_today()
    available = get_available_dates()
    if not available:
        return []

    days = sorted({date.fromisoformat(d) for d in available})
    watermark = _watermark()
    recent_from = today - timedelta(days=config.RESEND_RECENT_DAYS)

    out = []
    for d in days:
        is_new = watermark is None or d > watermark
        is_recent = d >= recent_from
        if is_new or is_recent:
            out.append(d)
    return out


def _build_days(days):
    """把若干天拼成请求体里的 days 数组。

    这里对所有天一次性调 get_summary_range，而不是逐天调 get_stats，
    是因为它内部会把「有哪些天有数据」和「应用图标缓存」之类的东西
    复用起来，天数多的时候快不少。
    """
    if not days:
        return []
    rows = get_summary_range(days[0].isoformat(), days[-1].isoformat())
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append({
            "app_name": r["app_name"],
            "display_name": r["display_name"] or "",
            # 保留一位小数：秒级浮点数的噪声位没有意义，白占带宽
            "seconds": round(float(r["total_seconds"]), 1),
            "launches": int(r.get("session_count", 0)),
        })
    # 只保留确实有应用的天（range 查询会把区间里其它有数据的天也带出来，
    # 那些天如果不筛掉，就会跟着这次请求一起传过去 —— 其实无害，
    # 但会让「这次传了几天」的日志对不上，排查时容易误会）
    wanted = {d.isoformat() for d in days}
    return [{"day": k, "apps": by_day[k]} for k in sorted(by_day) if k in wanted]


def _chunks(seq, size):
    """把列表按 size 切成若干段（生成器）。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ── HTTP ──────────────────────────────────────────────────────

class UploadError(Exception):
    """上传失败。message 是可以直接显示给用户看的一句话。

    status 是 HTTP 状态码；网络层就失败（连不上、超时、返回的不是 JSON）时
    它是 None。把状态码挂在异常上而不是从 message 里再抠出来，是因为
    「抠字符串」这件事只要文案改一个字就会悄悄失效 —— 那时表现是
    重试策略变了，但没有任何地方报错，最难查。
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _post_json(url, payload, timeout):
    """POST 一个 JSON，返回解析后的响应字典；失败抛 UploadError。

    用标准库的 urllib 而不是 requests：客户端要求尽量少的第三方依赖
    （每多一个依赖，打包体积和「装不上」的概率都涨一点），而这里
    只需要一个 POST，标准库完全够用。

    ⚠️ 这个模块一旦启用 urllib，PyInstaller 就得把 http / email 这些
    间接依赖打进去 —— app.spec 的 excludes 里不能有它们（否则打包后
    一上传就报 ModuleNotFoundError，而且只在打包版里复现，很难查）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"TimeTrace/{config.CLIENT_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 服务端明确回了一个状态码。把 body 读出来 —— FastAPI 会把出错原因
        # 放在 detail 里（比如「user_key 必须是合法的 UUID」），
        # 不带上的话用户只能看到一句「HTTP 422」，没法排查。
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise UploadError(f"服务端返回 {e.code}：{detail or e.reason}",
                          status=e.code)
    except urllib.error.URLError as e:
        # 连不上：DNS 失败、超时、被防火墙挡了都会走这里。
        # 这是最常见的失败，措辞上要说清「是网络问题，不是数据问题」。
        raise UploadError(
            f"连不上服务端（{config.SERVER_HOST}:{config.SERVER_PORT}）：{e.reason}")
    except Exception as e:                       # noqa: BLE001
        raise UploadError(f"上传出错：{type(e).__name__}: {e}")

    try:
        return json.loads(body)
    except ValueError:
        raise UploadError(f"服务端返回的不是 JSON：{body[:200]}")


def _is_retryable(err):
    """判断一次失败值不值得重试。

    只有「网络抖动 / 服务端临时故障」值得重试。4xx 是「你发的东西有问题」，
    重试一万次结果都一样，只会白等 —— 尤其是 422，那是客户端代码的 bug。
    429（限流）是例外：等一会儿再试是对的。
    """
    if err.status is None:
        return True           # 网络层失败，值得再试
    return err.status == 429 or err.status >= 500


def _post_with_retry(url, payload):
    """带指数退避的重试。全部失败时抛最后一次的 UploadError。"""
    delay = config.RETRY_BACKOFF
    last = None
    for attempt in range(config.RETRY_COUNT + 1):
        try:
            return _post_json(url, payload, config.REQUEST_TIMEOUT)
        except UploadError as e:
            last = e
            if attempt == config.RETRY_COUNT or not _is_retryable(e):
                break
            time.sleep(delay)
            delay *= 2      # 2s → 4s → 8s，避免服务器刚重启时所有客户端一起猛冲
    raise last


def fetch_health():
    """探一下服务端活着没有，返回 {"ok": bool, "message": str}。给设置页显示用。

    刻意不抛异常：这是个「显示状态」的功能，连不上就是连不上，
    不应该让界面弹一堆错误。
    """
    url = config.base_url() + config.HEALTH_PATH
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "message": f"无法连接：{type(e).__name__}"}
    if body.get("ok"):
        return {"ok": True, "message": "服务端正常"}
    return {"ok": False, "message": "服务端在跑，但数据库连不上"}


# ── 上传 ──────────────────────────────────────────────────────

def upload_pending(today=None, force=False):
    """把还没传（以及最近几天）的数据传上去，返回结果字典。

    返回：
        {"ok": bool, "reason": str, "days": int, "apps": int, "error": str|None}
        reason 取值：ok / disabled / no_data / nothing_new / error

    **不抛异常**：调用方既有后台线程也有按钮点击的槽函数，两边都不该
    因为「网断了」而崩掉或者弹栈。失败信息放在返回值的 error 里。

    参数：
        today：覆盖「今天」是哪天，方便测试。
        force：True 时忽略 upload_enabled 开关。手动点「立即上传」用这个 ——
               用户主动点的操作，不该被「自动上传已关闭」挡住。
    """
    if not force and get_setting(_ENABLED_KEY, "1") != "1":
        return {"ok": False, "reason": "disabled", "days": 0, "apps": 0,
                "error": None}

    # 同一时刻只允许一个上传在跑
    if not _upload_lock.acquire(blocking=False):
        return {"ok": False, "reason": "busy", "days": 0, "apps": 0,
                "error": "已经在传了"}

    try:
        days = pending_days(today=today)
        if not days:
            has_data = bool(get_available_dates())
            return {"ok": True, "reason": "no_data" if not has_data else "nothing_new",
                    "days": 0, "apps": 0, "error": None}

        user_key = get_or_create_user_key()
        base = {
            "user_key": user_key,
            # 只在「订阅打开」时才带邮箱。退订之后再每天把地址往上传是没必要的：
            # 用户说了不要，我们不该还继续把地址送出去。退订这个动作本身走
            # subscription 接口（见 update_subscription），这里只影响顺带上传。
            #
            # 留着这个字段仍然有价值：万一服务端的库被清空重建，上传时会按
            # 「有没有邮箱」重新插入一行 users，订阅状态跟着自愈。
            "email": get_email() if is_subscribed() else None,
            **_machine_info(),
        }

        batches = list(_chunks(days, MAX_DAYS_PER_REQUEST))
        total_days = 0
        total_apps = 0
        for batch in batches:
            built = _build_days(batch)
            if not built:
                # 理论上到不了这里：pending_days 是从「有数据的日期」里挑的，
                # 挑出来的天不该查不到行。留着是为了万一真出现（比如这一批的
                # 数据刚好被 clear_all_data 清了），宁可少发一个请求，
                # 也不要发一个 days=[] 的请求让服务端回 400。
                continue
            payload = dict(base, days=built)
            resp = _post_with_retry(config.upload_url(), payload)
            total_days += int(resp.get("days_written", 0))
            total_apps += int(resp.get("apps_written", 0))

        # 水位线推到「这次传过的最晚一天」。
        # 注意只在**全部批次都成功**之后才推 —— 中间任何一批失败都会走到
        # 下面的 except，水位线不动，下次从头再来。服务端是整块覆盖，
        # 重传已经传过的批次没有副作用。
        set_setting(_WATERMARK_KEY, batches[-1][-1].isoformat())
        set_setting(_LAST_SUCCESS_KEY, _now_str())

        # 都传成功了，说明服务端这会儿是通的 —— 顺手把之前没同步出去的
        # 订阅设置补上（用户可能在还没上传过、或者断网的时候填了邮箱）。
        # 放在返回值之前，但结果完全不参与返回值：订阅同步失败不该把
        # 一次成功的上传报成失败。
        flush_subscription()

        return {"ok": True, "reason": "ok", "days": total_days,
                "apps": total_apps, "error": None}
    except UploadError as e:
        return {"ok": False, "reason": "error", "days": 0, "apps": 0,
                "error": str(e)}
    except Exception as e:                       # noqa: BLE001
        # 兜住所有异常：这个方法可能在后台线程里被调用，抛出去会把线程干掉。
        return {"ok": False, "reason": "error", "days": 0, "apps": 0,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        _upload_lock.release()


# ── 订阅（每日报告邮件） ──────────────────────────────────────

def update_subscription(email=None, subscribed=True):
    """把「邮箱 + 订阅开关」记到本机，并尽力同步给服务端。

    **本地优先**：先写本机设置，再去调服务端。顺序是刻意的 ——
    「我订阅哪个邮箱」本质上只是本机的一个偏好，和上传数据是两件事
    （见 _SUBSCRIBE_KEY 上面那段）。如果非要等服务端点头才肯记，
    那服务端一挂用户连自己的邮箱都存不住，而这一条并不需要服务端。
    这一条也保证了「除了上传，其它功能都能独立跑」。

    同步失败**不算失败**：本机已经记下了，同时打上「待同步」标记，
    下次上传成功时由 flush_subscription() 补一次（上传成功本身就说明
    服务端是通的）。以前这里在服务端 404 时会把用户挡在
    「请先点立即上传」前面 —— 「先填邮箱、后上传」是很自然的顺序，
    不该是个死胡同。

    返回：
        {"ok": bool, "reason": str, "error": str|None,
         "email": str|None, "subscribed": bool, "synced": bool}
        ok     —— 本机记下了没有（只有 no_email / bad_email 是 False）
        synced —— 服务端是不是也知道了
        reason —— ok / pending_upload / offline / no_email / bad_email

    **不抛异常**：它会跑在后台线程里，抛出去只会把线程干掉。
    """
    email = (email or "").strip()
    subscribed = bool(subscribed)

    if subscribed and not email:
        return {"ok": False, "reason": "no_email", "error": "订阅需要填一个邮箱",
                "email": None, "subscribed": False, "synced": False}
    if email and not looks_like_email(email):
        return {"ok": False, "reason": "bad_email",
                "error": f"「{email}」看起来不是一个邮箱地址",
                "email": email, "subscribed": subscribed, "synced": False}

    # 1) 先落本地。只写本机 SQLite，不会因为网络失败。
    set_email(email)
    set_subscribed(subscribed)

    # 2) 再尽力同步给服务端；没成功就打标记，留给下一次上传补。
    user_key = get_or_create_user_key()
    try:
        _post_with_retry(config.subscription_url(user_key),
                         {"email": email or None, "subscribed": subscribed})
    except UploadError as e:
        set_setting(_SUBSCRIBE_DIRTY_KEY, "1")
        # 404 单独分一类：服务端还没有这台设备（装好之后还没上传过）。
        # 这不是错误，只是「还没轮到同步」—— 第一次上传会把这条一起带上去，
        # 所以文案上不能吓唬用户去排查网络。
        return {"ok": True,
                "reason": "pending_upload" if e.status == 404 else "offline",
                "error": str(e), "email": email or None,
                "subscribed": subscribed, "synced": False}
    except Exception as e:                       # noqa: BLE001
        set_setting(_SUBSCRIBE_DIRTY_KEY, "1")
        return {"ok": True, "reason": "offline",
                "error": f"{type(e).__name__}: {e}", "email": email or None,
                "subscribed": subscribed, "synced": False}

    set_setting(_SUBSCRIBE_DIRTY_KEY, "0")
    return {"ok": True, "reason": "ok", "error": None,
            "email": email or None, "subscribed": subscribed, "synced": True}


def flush_subscription():
    """把本机「待同步」的订阅设置补发给服务端。上传成功后调用。

    单独立一个函数（而不是让上传接口顺手同步）是因为服务端的
    upsert_user 只会在**第一次插入** users 行时按「有没有邮箱」定
    subscribed，之后再不碰 —— 那是为了用户退订后不被每天的上传自动
    订回来。代价是「先订阅后上传」「离线时改订阅」这两种情况光靠上传
    同步不过去，只能专门补一次。

    只做一件小事，而且**不抛异常、绝不影响上传结果** —— 它是搭便车的，
    搭不上不该把车弄坏。返回 True 表示现在两边一致了。
    """
    if not subscription_pending():
        return True
    state = subscription_state()
    return bool(update_subscription(state["email"], state["subscribed"]).get("synced"))


def subscription_pending():
    """本机有没有「改了订阅但还没同步到服务端」的东西。"""
    return get_setting(_SUBSCRIBE_DIRTY_KEY, "0") == "1"


def describe_subscription(result):
    """把 update_subscription 的返回值翻成一句给用户看的话。

    分三种情况说话，不能混：本机存下了没有（ok）、服务端知道没有（synced）、
    以及退订和订阅的说法不一样。含糊其辞的代价是用户以为没收到邮件是程序坏了。
    """
    if not result.get("ok"):
        # no_email / bad_email：这两类连本机都没存，必须说清楚错在哪
        return result.get("error") or "保存失败"

    if result.get("synced"):
        if result.get("subscribed"):
            return f"已订阅：每天 00:03 会把昨天的使用报告发到 {result.get('email')}"
        return "已退订，服务器不会再给你发邮件"

    # 本机已存，服务端还不知道 —— 必须说出来，不能让用户以为已经生效了
    if result.get("reason") == "pending_upload":
        tail = "这台设备还没往服务端传过数据，第一次上传时会一起同步过去"
    else:
        tail = "暂时连不上服务端，下次上传数据时会自动同步过去"
    if result.get("subscribed"):
        return f"已在本机记下 {result.get('email')}；{tail}"
    return f"已在本机退订；{tail}"


def test_connection():
    """兼容旧调用入口：手动同步应用汇总和已授权的明细，并分别报告结果。"""
    return upload_all(force=True)


def upload_all(force=False):
    """Upload applications and opted-in details, reporting each outcome independently.

    Explicit manual uploads bypass only the automatic-upload switch. Website and
    timeline consent switches still apply; one failed dataset does not block others.
    """
    from net import tracking_upload
    try:
        applications = upload_pending(force=force)
    except Exception as exc:
        applications = {"ok": False, "reason": "error", "days": 0, "apps": 0,
                        "error": f"{type(exc).__name__}: {exc}"}
    try:
        details = tracking_upload.run_once(force=force)
    except Exception:
        # Do not expose device tokens or request payloads in exception messages.
        details = {"reason": "error"}
        for kind in ("web", "timeline"):
            if get_setting(f"{kind}_upload_enabled", "0") == "1":
                set_setting(f"{kind}_upload_status", "error")
    details_ok = (details.get("reason") == "disabled" or
                  bool(details) and all(status == "ok" for status in details.values()))
    return {**applications, "apps_ok": applications["ok"], "apps_reason": applications["reason"],
            "reason": "partial_upload" if applications["ok"] and not details_ok else applications["reason"],
            "tracking": details,
            "ok": applications["ok"] and details_ok}


def describe_result(result):
    """把 upload_pending 的返回值翻译成一句给用户看的话。"""
    if "tracking" in result:
        from net import tracking_upload
        applications = {k: v for k, v in result.items() if k != "tracking"}
        applications["ok"] = result["apps_ok"]
        applications["reason"] = result.get("apps_reason", result["reason"])
        lines = ["应用：" + describe_result(applications)]
        details = result["tracking"]
        for kind, title in (("web", "网站"), ("timeline", "应用时段")):
            status = details.get(kind, details.get("reason", "未开启明细上传"))
            lines.append(f"{title}：{tracking_upload.describe_status(status)}")
        return "\n".join(lines)
    if result["ok"]:
        if result["reason"] == "no_data":
            return "本机还没有使用记录，没什么可传的"
        if result["reason"] == "nothing_new":
            return "没有需要上传的新数据"
        return (f"上传成功：{result['days']} 天 / {result['apps']} 条应用记录")
    if result["reason"] == "disabled":
        return "自动上传已关闭"
    if result["reason"] == "busy":
        return "上一次上传还没结束，请稍候"
    return f"上传失败：{result['error']}"


def last_success_text():
    """最近一次上传成功的时间，给界面显示；没传过返回「从未上传」。"""
    v = get_setting(_LAST_SUCCESS_KEY) or ""
    return v or "从未上传"


def upload_status_lines():
    """Show independent timestamps and failures in the settings summary."""
    from net import tracking_upload
    lines = [f"应用最近上传：{last_success_text()}"]
    for kind, title in (("web", "网站"), ("timeline", "应用时段")):
        if get_setting(f"{kind}_upload_enabled", "0") != "1":
            lines.append(f"{title}上传：未开启")
            continue
        status = tracking_upload.describe_status(get_setting(f"{kind}_upload_status", "尚未上传"))
        stamp = get_setting(f"{kind}_upload_last_success", "")
        lines.append(f"{title}上传：{status}" + (f" · 最近成功 {stamp}" if stamp else ""))
    return lines


def _now_str():
    return beijing_now().strftime("%Y-%m-%d %H:%M")


# ── 后台定时器 ────────────────────────────────────────────────

class UploadScheduler:
    """在后台线程里定期上传，和 exporter.ExportScheduler 用法完全一样。

        sched = UploadScheduler()
        sched.start()
        ...
        sched.stop()

    为什么是「定期醒来检查」而不是「每天定时到点传」：
        这个程序不一定会一直在后台跑。如果写死「每天 23:59 传」，用户
        那个点没开机就永远不传了。改成「每小时醒来看看有没有该传的」，
        开机即补传，也不怕漏。
    """

    def __init__(self, interval=None):
        self.interval = interval or config.UPLOAD_CHECK_INTERVAL
        self._running = False
        self._thread = None
        self.last_result = None    # 最近一次的结果字典，供界面显示

    def start(self):
        """启动后台线程。重复调用不会有副作用。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止并等线程退出。最多等 5 秒，避免卡住退出流程。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self):
        while self._running:
            self.run_once()
            day_started = beijing_today()
            # 把长等待切成 1 秒一段，这样 stop() 能较快生效
            slept = 0.0
            while self._running and slept < self.interval:
                time.sleep(1.0)
                slept += 1.0
                # Force a day-end upload after the monitor's 10-second DB checkpoint.
                now = beijing_now()
                if now.date() != day_started and (now.minute >= 1 or now.second >= 30 or now.hour > 0):
                    break

    def run_once(self):
        """执行一次上传。

        异常由 upload_pending 内部兜住，这里再包一层是为了防「取设置项时
        数据库出问题」这类上传之外的异常 —— 后台线程绝不能因为一次异常
        就静悄悄地死掉，那样以后就再也不上传了，而用户毫无察觉。
        """
        try:
            self.last_result = upload_all()
        except Exception as e:                   # noqa: BLE001
            self.last_result = {"ok": False, "reason": "error", "days": 0,
                                "apps": 0, "error": f"{type(e).__name__}: {e}"}
        self.last_tracking_result = self.last_result.get("tracking", {})
        return self.last_result
