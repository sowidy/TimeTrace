"""数据层：所有数据库读写、以及「应用名归一化」和「时长格式化」都放在这里。

用 SQLite（Python 自带，不需要单独装数据库服务器），整个数据库就是一个
usage.db 文件，放在程序目录（打包后优先放在 %APPDATA%\\TimeTrace\\ 下）。

一共有 5 张表：
    usage_fragments  原始的「片段」记录，一条 = 某段时间用了某个应用
    app_usage        按天汇总的累计时长，查询排行时直接读它，比每次现算快
    ignored_apps     用户手动「忽略」的应用名单
    app_rules        应用归一化规则（把一堆辅助进程合并成同一个应用）
    settings         键值对形式的配置项

关于「片段（fragment）」：
    监控线程记的是一条条原始片段，比如「13:00:00 ~ 13:00:47 在用 Chrome」。
    片段会被按天切开（跨午夜的那条会被拆成两条），然后累加进 app_usage。

关于线程安全：
    SQLite 连接对象不能跨线程共用，所以每次操作都新建一个连接（见 _get_conn）。
    写操作加上 _lock，避免多个线程同时写导致 database is locked 报错。
"""
import sqlite3
import os
import sys
from contextlib import contextmanager
from datetime import date, timedelta, datetime
from threading import Lock

# 数据库、导出目录这些路径统一由 paths.py 决定，见该模块的说明。
# （DB_PATH 仍保留在这里，因为 tests/test_storage.py 会直接替换这个变量
# 来指向临时数据库，所以它必须是本模块的属性。）
from core.paths import db_path as _db_path
from core.timeutil import beijing_today


# 模块级常量：导入本模块时就算好路径。
# 测试里会直接替换这个变量来指向临时数据库文件。
DB_PATH = _db_path()
_lock = Lock()

# 内置默认中文名（用户自定义规则优先级更高）
# 键是进程名（小写），值是展示给用户看的中文名
_APP_NAMES = {
    "chrome.exe": "Chrome 浏览器",
    "msedge.exe": "Edge 浏览器",
    "firefox.exe": "Firefox 浏览器",
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code",
    "codex.exe": "Codex",
    "explorer.exe": "文件资源管理器",
    "wechat.exe": "微信",
    "wechatappex.exe": "微信",
    "weixin.exe": "微信",
    "baidunetdisk.exe": "百度网盘",
    "baidunetdiskunite.exe": "百度网盘",
    "hpeesofde.exe": "Keysight ADS",
    "hpeesofdds.exe": "Keysight ADS",
    "hpeesofsess.exe": "Keysight ADS",
    "qq.exe": "QQ",
    "dingtalk.exe": "钉钉",
    "feishu.exe": "飞书",
    "slack.exe": "Slack",
    "spotify.exe": "Spotify",
    "telegram.exe": "Telegram",
    "notepad.exe": "记事本",
    "notepad++.exe": "Notepad++",
    "photos.exe": "照片",
    "snippingtool.exe": "截图工具",
    "python.exe": "Python",
    "pythonw.exe": "Python",
    "cmd.exe": "命令提示符",
    "windowsterminal.exe": "终端",
    "pwsh.exe": "PowerShell",
    "7zg.exe": "7-Zip",
    "winrar.exe": "WinRAR",
    "gameviewer.exe": "UU远程",
    "anydesk.exe": "AnyDesk",
    "teamviewer.exe": "TeamViewer",
    "steam.exe": "Steam",
    "wegame.exe": "WeGame",
    "tyty.exe": "Tyty",
    "shellexperiencehost.exe": "开始菜单",
    "openwith.exe": "打开方式",
    "searchui.exe": "搜索",
    "textinputhost.exe": "输入法",
    "leagueclientux.exe": "英雄联盟",
}

# 预先转成小写的副本，查表时不用每次再 .lower()（进程名大小写不固定）
_APP_NAMES_LOWER = {k.lower(): v for k, v in _APP_NAMES.items()}

# 默认合并规则（会被 DB 中的 app_rules 覆盖）
# 格式：{子进程名: 归并到的父进程名}
# 例：英雄联盟会同时跑 4 个进程，这里统一算到 leagueclientux.exe 名下，
#     统计表里就只显示一行「英雄联盟」而不是四行。
_DEFAULT_APP_GROUPS = {
    "leagueclientux.exe":     "leagueclientux.exe",
    "league of legends.exe":  "leagueclientux.exe",
    "riotclientux.exe":       "leagueclientux.exe",
    "riotclientservices.exe": "leagueclientux.exe",
    "wechatappex.exe":        "wechat.exe",
    "wechat.exe":             "weixin.exe",
    "weixin.exe":             "weixin.exe",
    "baidunetdiskunite.exe":  "baidunetdisk.exe",
    "baidunetdisk.exe":       "baidunetdisk.exe",
    "hpeesofdds.exe":         "hpeesofde.exe",
    "hpeesofsess.exe":        "hpeesofde.exe",
    "hpeesofde.exe":          "hpeesofde.exe",
    "code - insiders.exe":    "code.exe",
}

# 运行时缓存
# 规则表内容基本不变，但查询很频繁（每次取图标都要用），所以缓存在内存里；
# 任何修改规则的函数都会把它置为 None 来让缓存失效。
_app_groups_cache = None

# 少数应用「合并后」的图标应该用哪个 exe 来提取。
# Keysight ADS 的主进程 hpeesofde.exe 自己没有好看的图标，
# 用兄弟进程 hpeesofdds.exe 的图标更合适。
_PREFERRED_ICON_EXECUTABLES = {
    "hpeesofde.exe": "hpeesofdds.exe",
}


@contextmanager
def _get_conn():
    """提供一个数据库连接，用完自动提交并关闭。

    用 @contextmanager 写成了上下文管理器，所以可以这样用：
        with _get_conn() as conn:
            conn.execute(...)

    with 块正常结束时自动 commit；抛异常时自动 rollback（撤销本次修改），
    这样不会出现「写了一半」的脏数据。finally 保证无论如何都关闭连接。

    注意 row_factory = sqlite3.Row：让查询结果支持按列名取值，
    比如 row["app_name"] 而不只是 row[0]，代码可读性好很多。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL 是一种日志模式：读写可以同时进行，监控线程写数据时界面查询不会卡住
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """建表并初始化默认数据。程序启动时调用一次，重复调用也安全。

    这里做的不只是建表，还包含几处「数据迁移」：老版本升级上来时，
    把旧的规则/脏数据修正成新格式。
    """
    global _app_groups_cache
    _app_groups_cache = None  # 建表可能改动规则，让缓存失效
    from core.site_storage import init_db as init_site_db
    init_site_db()
    # with 里同时用两个上下文管理器：先拿锁（串行化写操作），再拿连接
    with _lock, _get_conn() as conn:
        # CREATE TABLE IF NOT EXISTS：表已存在就什么都不做，所以可以反复调用
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_fragments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                app_name TEXT NOT NULL,
                window_title TEXT,
                exe_path TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                is_idle INTEGER DEFAULT 0
            )
        """)
        # 索引：绝大多数查询都是「按日期 + 应用名」过滤，建索引后快很多
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_frag_date_app
            ON usage_fragments(date, app_name)
        """)
        # 检查 app_usage 表是否有正确的列
        # 老版本的 app_usage 表结构不一样，这里读一下列名，
        # 缺少新列就把旧表删掉重建（反正汇总数据可以重新算出来）
        need_recreate = False
        try:
            cols = conn.execute("PRAGMA table_info(app_usage)").fetchall()
            col_names = {c[1] for c in cols}  # c[1] 是列名
            if "total_seconds" not in col_names or "session_count" not in col_names:
                need_recreate = True
        except Exception:
            pass

        if need_recreate:
            conn.execute("DROP TABLE IF EXISTS app_usage")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                app_name TEXT NOT NULL,
                total_seconds REAL NOT NULL DEFAULT 0,
                session_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(date, app_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ignored_apps (
                app_name TEXT PRIMARY KEY
            )
        """)
        # 应用合并/重命名规则表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_rules (
                child_name TEXT PRIMARY KEY,
                parent_name TEXT NOT NULL,
                display_name TEXT
            )
        """)
        # 设置表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # 初始化默认设置
        # INSERT OR IGNORE：已存在的键不会被覆盖，所以用户改过的设置能保留
        defaults = {
            "save_window_title": "0",  # 是否记录窗口标题（默认关闭，更保护隐私）
            "idle_threshold": "300",   # 空闲判定阈值（秒）
            "first_run_done": "0",     # 是否已完成首次引导
            "autostart": "0",          # 是否开机自启
            # 自动把使用时间导出成 CSV（默认开启，见 exporter.py）
            "export_enabled": "1",
            # 上次导出到哪一天，用来决定下次要补导哪些日期。
            # 空字符串表示还没导过，exporter 会从最早的数据开始补齐。
            "export_watermark": "",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        # 始终补齐新版本加入的默认规则，已有自定义规则不会被覆盖。
        for child, parent in _DEFAULT_APP_GROUPS.items():
            # 只有当子进程和父进程不同名时才给它一个显示名，
            # 同名的话显示名由父进程自己那一行决定。
            display = _APP_NAMES_LOWER.get(child) if child != parent else None
            conn.execute(
                "INSERT OR IGNORE INTO app_rules (child_name, parent_name, display_name) VALUES (?, ?, ?)",
                (child, parent, display),
            )
        # 旧版本曾把 WeChatAppEx 映射到 wechat.exe；新版统一到 weixin.exe。
        # 这条 UPDATE 专门修正老用户库里遗留的错误映射。
        conn.execute(
            """UPDATE app_rules
               SET parent_name = 'weixin.exe', display_name = ?
               WHERE child_name = 'wechatappex.exe'
                 AND parent_name = 'wechat.exe'""",
            (_APP_NAMES_LOWER["weixin.exe"],),
        )
        # 旧版本可能把统计器自己的窗口计入使用时间，升级时清理这些记录。
        # 用占位符拼 SQL（而不是直接拼字符串）是防 SQL 注入的标准做法；
        # 这里虽然值写死在代码里，但保持一致的习惯没有坏处。
        self_apps = ("timetrace.exe", "pctime.exe", "usagetracker.exe", "usagetime.exe")
        placeholders = ",".join("?" for _ in self_apps)  # 生成 "?,?"
        conn.execute(
            f"DELETE FROM usage_fragments WHERE LOWER(app_name) IN ({placeholders})",
            self_apps,
        )
        conn.execute(
            f"DELETE FROM app_usage WHERE LOWER(app_name) IN ({placeholders})",
            self_apps,
        )


# ── 设置 ──────────────────────────────────────────────────────
# settings 表就是一个简单的键值对仓库，用来存用户偏好。

def get_setting(key, default=None):
    """读取设置项；不存在时返回 default。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row:
        return row["value"]
    return default


def set_setting(key, value):
    """写入设置项（存在则覆盖）。值会统一转成字符串存储。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def get_all_settings():
    """一次性读出所有设置，返回 {键: 值} 字典。"""
    with _get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def is_first_run():
    """是否还没走过首次运行引导。"""
    return get_setting("first_run_done", "0") != "1"


def mark_first_run_done():
    """标记首次引导已完成，之后不再弹引导窗。"""
    set_setting("first_run_done", "1")


# ── 应用规则 ──────────────────────────────────────────────────

def get_app_rules():
    """返回 {child: parent} 字典，合并内置默认 + DB 自定义。

    结果会缓存在 _app_groups_cache 里。调用方拿到的是缓存里的同一个对象，
    **不要直接修改返回值**，否则会污染缓存。
    """
    global _app_groups_cache
    if _app_groups_cache is not None:
        return _app_groups_cache
    rules = dict(_DEFAULT_APP_GROUPS)  # 先复制一份内置默认规则
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT child_name, parent_name FROM app_rules"
        ).fetchall()
    # 数据库里的规则覆盖内置默认（用户自定义优先）
    # 键和值都转小写，因为进程名大小写不稳定
    for r in rows:
        rules[r["child_name"].lower()] = r["parent_name"].lower()
    _app_groups_cache = rules
    return rules


def canonical_app_name(app_name, rules=None, exe_path=""):
    """沿合并规则找到最终应用名，并防止循环规则卡死。

    比如 wechatappex.exe → wechat.exe → weixin.exe，一路追到没有规则为止，
    最后返回 weixin.exe，这样所有微信相关进程都会汇总到同一行。

    seen 集合用来记录走过的名字：万一用户配了 A→B、B→A 这种循环规则，
    循环检测能让函数正常退出而不是死循环。
    """
    rules = get_app_rules() if rules is None else rules
    current = app_name.lower()
    # 特殊处理：OpenAI 的 Codex 桌面版进程名也叫 ChatGPT.exe，
    # 只能靠 exe 路径里是否包含 "openai.codex_" 来区分，否则会被算成 ChatGPT。
    if (current == "chatgpt.exe" and
            "openai.codex_" in exe_path.lower()):
        current = "codex.exe"
    seen = set()
    while current not in seen:
        seen.add(current)
        parent = rules.get(current, current)  # 没有规则就指向自己
        if parent == current:
            break  # 已经是最终名字了
        current = parent
    return current


def get_app_rules_with_display():
    """返回 [(child, parent, display_name), ...] 用于 UI 展示。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT child_name, parent_name, display_name FROM app_rules ORDER BY parent_name"
        ).fetchall()
    # 转成普通元组列表，不让 Row 对象泄漏到 UI 层去
    return [(r["child_name"], r["parent_name"], r["display_name"]) for r in rows]


def add_app_rule(child_name, parent_name, display_name=None):
    """新增或更新一条合并规则。写入后让内存缓存失效。"""
    global _app_groups_cache
    _app_groups_cache = None
    with _lock, _get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO app_rules (child_name, parent_name, display_name)
               VALUES (?, ?, ?)""",
            (child_name.lower(), parent_name.lower(), display_name),
        )


def remove_app_rule(child_name):
    """删除一条合并规则。child_name 是主键，所以直接按它删。"""
    global _app_groups_cache
    _app_groups_cache = None
    with _lock, _get_conn() as conn:
        conn.execute(
            "DELETE FROM app_rules WHERE child_name = ?",
            (child_name.lower(),),
        )


# ── 崩溃恢复 ──────────────────────────────────────────────────

def get_last_unfinished_fragment():
    """获取最后一次未正常结束的片段（end_time 晚于当前时间前 10 分钟的不算）。
    实际上我们检查最近一条 fragment，如果它的 end_time 距现在超过 poll_interval*2，
    说明程序可能异常退出时丢失了中间片段。返回 (app_name, end_time) 或 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT app_name, end_time FROM usage_fragments
               WHERE is_idle = 0
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()  # 按 id 倒序取第一条 = 最新的一条
    if row:
        # 返回字典而不是 Row 对象，方便调用方直接当普通数据用
        return {"app_name": row["app_name"], "end_time": row["end_time"]}
    return None


# ── 记录 ──────────────────────────────────────────────────────

def record_fragment(app_name, window_title, exe_path, start_iso, end_iso,
                    duration, is_idle=False):
    """写入一条使用片段（单个版本，内部转调批量接口）。

    参数用 iso 结尾，表示传入的是 ISO 格式的时间字符串而不是 datetime 对象。
    """
    record_fragments_batch([{
        "app_name": app_name,
        "window_title": window_title,
        "exe_path": exe_path,
        "start_time": start_iso,
        "end_time": end_iso,
        "duration": duration,
        "is_idle": is_idle,
    }])


def _split_fragment_by_day(fragment):
    """按北京时间午夜拆分片段，保证每日统计归属正确。

    为什么要拆：如果一条记录是 23:59:55 ~ 次日 00:00:05，直接归属到某一天
    就不对了（「今日使用时长」会算错）。拆成两条后，每天各得 5 秒。

    返回一个列表，里面是拆分后的片段（可能 0 条、1 条或多条）。
    """
    try:
        start = datetime.fromisoformat(fragment["start_time"])
        end = datetime.fromisoformat(fragment["end_time"])
    except (KeyError, TypeError, ValueError):
        # 时间格式不对时不能丢数据：原样返回，日期就用今天兜底
        fallback = dict(fragment)
        fallback["date"] = beijing_today().isoformat()
        return [fallback]

    if end <= start:
        # 时长非正（时钟被改过或数据异常），丢弃
        return []

    parts = []
    cursor = start  # 游标：当前切到哪儿了
    while cursor < end:
        # 算出「下一个午夜」的时刻
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # 本段的结束点 = 原始结束时间 和 下一个午夜 中较早的那个
        part_end = min(end, next_midnight)
        part = dict(fragment)  # 复制一份，避免修改调用方传进来的字典
        part["date"] = cursor.date().isoformat()
        part["start_time"] = cursor.isoformat()
        part["end_time"] = part_end.isoformat()
        part["duration"] = (part_end - cursor).total_seconds()
        parts.append(part)
        cursor = part_end  # 从本段结束处继续切
    return parts


def record_fragments_batch(fragments):
    """批量写入片段。fragments 是 list of dict。

    批量写入的意义：监控线程每 10 秒才调用一次，一次把攒下的多条一起写，
    每条开一次事务会非常慢。这里整体共用同一个连接/事务。

    同时会更新 app_usage 汇总表：
        total_seconds 累加，session_count 加 1（session = 一次连续使用）。
    """
    if not fragments:
        return
    # 标题比较敏感，用户没开启保存时一律写 None
    save_title = get_setting("save_window_title", "0") == "1"
    with _lock, _get_conn() as conn:
        for original in fragments:
            # 一条原始片段可能被拆成跨天的多条
            for f in _split_fragment_by_day(original):
                if f["duration"] < 0.5:
                    continue  # 太短的忽略，减少噪音
                title = f.get("window_title") if save_title else None
                fragment_date = f["date"]
                conn.execute(
                    """INSERT INTO usage_fragments
                       (date, app_name, window_title, exe_path,
                        start_time, end_time, duration_seconds, is_idle)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fragment_date, f["app_name"], title,
                     f.get("exe_path", ""), f["start_time"], f["end_time"],
                     round(f["duration"], 2),  # 保留两位小数，避免浮点噪声
                     1 if f.get("is_idle") else 0),  # SQLite 没有布尔类型，用 0/1
                )
                if not f.get("is_idle"):
                    # 只把「真实使用」计入排行，空闲不计入。
                    # ON CONFLICT ... DO UPDATE 是 SQLite 的「upsert」写法：
                    # 该 (date, app_name) 已存在就累加，不存在就插入新行。
                    conn.execute(
                        """INSERT INTO app_usage
                           (date, app_name, total_seconds, session_count)
                           VALUES (?, ?, ?, 1)
                           ON CONFLICT(date, app_name)
                           DO UPDATE SET total_seconds = total_seconds + ?,
                                         session_count = session_count + 1""",
                        (fragment_date, f["app_name"], f["duration"],
                         f["duration"]),
                    )


# ── 查询 ──────────────────────────────────────────────────────

def get_display_name(app_name):
    """把进程名翻译成给用户看的中文名。

    查找顺序（先找到先用）：
        1. 数据库里用户自定义规则的显示名
        2. 该应用被合并到的父应用的自定义显示名
        3. 内置的 _APP_NAMES 中文名表
        4. 快捷方式（桌面/开始菜单）的文件名
        5. exe 文件属性里的「文件说明」
        6. 把 .exe 后缀去掉，直接用进程名
    """
    # 优先查 DB 自定义规则的 display_name
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT display_name FROM app_rules WHERE child_name = ? AND display_name IS NOT NULL",
            (app_name.lower(),),
        ).fetchone()
        if row and row["display_name"]:
            return row["display_name"]
        # 查是否被合并到某个 parent
        row2 = conn.execute(
            "SELECT parent_name FROM app_rules WHERE child_name = ?",
            (app_name.lower(),),
        ).fetchone()
        if row2 and row2["parent_name"] != app_name.lower():
            # 自己没名字，就借用父应用的名字（比如 wechatappex.exe 借用微信的名字）
            prow = conn.execute(
                "SELECT display_name FROM app_rules WHERE child_name = ? AND display_name IS NOT NULL",
                (row2["parent_name"],),
            ).fetchone()
            if prow and prow["display_name"]:
                return prow["display_name"]

    # 已知应用使用稳定名称，避免 explorer.exe 等通用启动器被快捷方式误认。
    # （比如桌面上可能有个叫「某某项目」的快捷方式指向 explorer.exe，
    #  不能因此把资源管理器显示成「某某项目」）
    name = _APP_NAMES_LOWER.get(app_name.lower())
    if name:
        return name

    # 快捷方式名称
    try:
        from system.icons import get_shortcut_info  # 局部导入：避免模块互相循环依赖
        sc = get_shortcut_info(app_name)
        if sc and sc["display_name"]:
            return sc["display_name"]
    except Exception:
        pass  # 拿不到快捷方式不影响主流程，继续往下兜底

    desc = _extract_display_name(app_name)
    if desc:
        return desc
    if app_name.lower().endswith(".exe"):
        return app_name[:-4]  # 去掉结尾 4 个字符 ".exe"
    return app_name


_name_cache = {}  # 缓存 exe 文件属性里读出来的说明文字，避免重复读文件


def _extract_display_name(app_name):
    """从运行中的进程 exe 文件属性里读「文件说明」（FileDescription）。

    会真的去遍历当前所有进程找到对应的 exe 路径，再读它的版本信息，
    所以开销不小——因此结果会被 _name_cache 缓存起来。
    失败时返回 None，由调用方继续兜底。
    """
    if app_name in _name_cache:
        return _name_cache[app_name]
    try:
        import psutil
        import win32api
        rules = get_app_rules()
        # 收集同一应用的其它进程名（比如 code.exe 和 "code - insiders.exe"）
        aliases = {app_name.lower()}
        for child, parent in rules.items():
            if parent == app_name.lower():
                aliases.add(child)
        for proc in psutil.process_iter(['name', 'exe']):
            try:
                if proc.info['name'] and proc.info['name'].lower() in aliases and proc.info['exe']:
                    # Windows 版本信息里，「语言+代码页」本身也要先查出来，
                    # 再用它去拼真正的字段路径
                    info = win32api.GetFileVersionInfo(proc.info['exe'], "\\VarFileInfo\\Translation")
                    if info:
                        lang, cp = info[0]
                        key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
                        desc = win32api.GetFileVersionInfo(proc.info['exe'], key)
                        desc = desc.strip() if desc else ""
                        # 这些是安装包/启动器的通用说明文字，当名字用毫无意义
                        bad = {"release", "main", "app", "application","loader", "launcher", "setup", "install"}
                        if desc and len(desc) > 1 and desc.lower() not in bad:
                            _name_cache[app_name] = desc
                            return desc
            except Exception:
                continue  # 单个进程读失败就跳过，继续看下一个
    except Exception:
        pass
    return None


def _get_recent_exe_paths():
    """返回每个应用记录过的可执行文件路径，最近路径排在前面。

    用途：取图标时，同一个应用可能被记录过好几个 exe 路径（主程序 + 辅助进程），
    这里按最后一次出现的时间倒序，让调用方能优先选「最近用的那个」。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT LOWER(app_name) AS app_name, exe_path, MAX(id) AS last_id
               FROM usage_fragments
               WHERE exe_path IS NOT NULL AND TRIM(exe_path) != ''
               GROUP BY LOWER(app_name), exe_path
               ORDER BY last_id DESC"""
        ).fetchall()
    # sqlite3.Row 转普通字典，后续代码用起来更简单
    return [dict(row) for row in rows]


def _preferred_exe_path(app_name, recent_paths, rules):
    """在同一个应用记录过的多个 exe 路径里，挑一个最适合用来取图标的。

    优先级：
        1. _PREFERRED_ICON_EXECUTABLES 里指定的特殊进程
        2. 文件名和规范名完全一致的（也就是真正的主程序）
        3. 都不满足就用最近用过的那个
    取不到就返回空字符串，调用方会退回到默认图标。
    """
    # 先筛出所有「归一化之后属于这个应用」的路径
    candidates = [
        row for row in recent_paths
        if canonical_app_name(
            row["app_name"], rules, row["exe_path"]
        ) == app_name
    ]
    preferred_name = _PREFERRED_ICON_EXECUTABLES.get(app_name)
    if preferred_name:
        for row in candidates:
            if os.path.basename(row["exe_path"]).lower() == preferred_name:
                return row["exe_path"]
    for row in candidates:
        if os.path.basename(row["exe_path"]).lower() == app_name:
            return row["exe_path"]
    return candidates[0]["exe_path"] if candidates else ""


def _merge_rows(rows, recent_paths=None):
    """把查询结果里同一应用的多个进程名合并成一行。

    数据库里存的是原始进程名（code.exe、code - insiders.exe...），
    这里按规则把它们归并，总时长相加，最后按总时长从多到少排序。

    返回列表，每项形如：
        {"app_name": 规范名, "total_seconds": 秒数,
         "display_name": 中文名, "exe_path": 取图标用的路径, ...}
    """
    rules = get_app_rules()
    # 允许调用方传入预先取好的数据，避免在循环里反复查库
    recent_paths = _get_recent_exe_paths() if recent_paths is None else recent_paths
    merged = {}
    for r in rows:
        d = dict(r)
        app = d["app_name"].lower()
        app_paths = [
            path for path in recent_paths
            if path["app_name"] == app
        ]
        latest_path = app_paths[0]["exe_path"] if app_paths else ""
        key = canonical_app_name(app, rules, latest_path)
        if key in merged:
            # 已经出现过：累加时长和会话数，days_used 取较大的那个
            merged[key]["total_seconds"] += d["total_seconds"]
            if "session_count" in d:
                merged[key]["session_count"] += d["session_count"]
            if "days_used" in d:
                merged[key]["days_used"] = max(merged[key]["days_used"], d["days_used"])
        else:
            merged[key] = {
                "app_name": key,
                "total_seconds": d["total_seconds"],
                "display_name": get_display_name(key),
                "exe_path": _preferred_exe_path(key, recent_paths, rules),
            }
            if "session_count" in d:
                merged[key]["session_count"] = d["session_count"]
            if "days_used" in d:
                merged[key]["days_used"] = d["days_used"]
    # key=lambda 指定排序依据：按总时长倒序（用得最多的排最前）
    return sorted(merged.values(), key=lambda x: x["total_seconds"], reverse=True)


def get_stats(target_date=None):
    """某一天各应用的使用排行。不传日期就是今天。"""
    if target_date is None:
        target_date = beijing_today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT LOWER(app_name) as app_name,
                      SUM(total_seconds) as total_seconds,
                      SUM(session_count) as session_count
               FROM app_usage
               WHERE date = ?
               GROUP BY LOWER(app_name)
               ORDER BY total_seconds DESC""",
            (target_date,),
        ).fetchall()
    return _merge_rows(rows)


def get_daily_total(target_date=None):
    """某一天的总使用秒数（所有应用加一起）。"""
    if target_date is None:
        target_date = beijing_today().isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            # COALESCE(..., 0)：当天没有任何记录时 SUM 返回 NULL，
            # 用 COALESCE 把它变成 0，避免上层拿到 None 崩掉
            "SELECT COALESCE(SUM(total_seconds), 0) as t FROM app_usage WHERE date = ?",
            (target_date,),
        ).fetchone()
    return row["t"]


def get_available_dates():
    """所有有数据的日期，从新到旧。用于界面上的日期导航。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM app_usage ORDER BY date DESC"
        ).fetchall()
    return [r["date"] for r in rows]


def get_week_stats(start_date=None):
    """返回截止到 start_date（含）的最近 7 天，每天的汇总时长。

    注意是「往前数 7 天」，start_date 是最后一天而不是第一天。
    返回值固定 7 项（没有数据的那天补 0），这样图表永远画 7 根柱子。
    """
    if start_date is None:
        start_date = beijing_today().isoformat()
    d = date.fromisoformat(start_date)
    week_start = d - timedelta(days=6)  # 往前推 6 天 = 共 7 天
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT date, SUM(total_seconds) as total_seconds
               FROM app_usage
               WHERE date >= ? AND date <= ?
               GROUP BY date
               ORDER BY date""",
            (week_start.isoformat(), start_date),
        ).fetchall()
    # 先做成字典，再按固定的 7 天顺序铺开，缺失的日期就是 0
    result = {r["date"]: r["total_seconds"] for r in rows}
    days = []
    for i in range(7):
        dd = week_start + timedelta(days=i)
        ds = dd.isoformat()
        days.append({"date": ds, "total_seconds": result.get(ds, 0)})
    return days


def get_week_app_stats(start_date=None):
    """最近 7 天各应用的使用排行。

    比 get_stats 多一个 days_used 字段：这个应用在这 7 天里用了几「天」，
    用于区分「天天用 10 分钟」和「某天集中用了 70 分钟」。
    """
    if start_date is None:
        start_date = beijing_today().isoformat()
    d = date.fromisoformat(start_date)
    week_start = d - timedelta(days=6)
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT LOWER(app_name) as app_name,
                      SUM(total_seconds) as total_seconds,
                      COUNT(DISTINCT date) as days_used
                   FROM app_usage
                   WHERE date >= ? AND date <= ?
                   GROUP BY LOWER(app_name)
                   ORDER BY total_seconds DESC""",
            (week_start.isoformat(), start_date),
        ).fetchall()
    return _merge_rows(rows)


def get_summary_range(start_date, end_date):
    """返回 [start_date, end_date] 区间内每日各应用汇总，用于 CSV 导出。

    复用 get_stats() 的合并逻辑（多进程归一、加上中文显示名），再按
    日期从早到晚、每天内部按使用时长从多到少排序。日期范围之外、以及
    没有数据的天不会出现在结果里。

    返回列表，每项形如：
        {"date": "2026-09-22", "app_name": "weixin.exe",
         "display_name": "微信", "total_seconds": 1234.5,
         "session_count": 7}
    """
    rows = []
    # get_available_dates() 是从新到旧排的，这里只挑落在区间内的日期
    for d in get_available_dates():
        if start_date <= d <= end_date:
            for s in get_stats(d):  # 单日汇总已经按时长降序排好
                rows.append({
                    "date": d,
                    "app_name": s["app_name"],
                    "display_name": s["display_name"],
                    "total_seconds": s["total_seconds"],
                    "session_count": s.get("session_count", 0),
                })
    # 上面是按日期从新到旧追加的，这里再按日期正序排一遍，
    # 让导出的 CSV 时间从早到晚、更自然。每天内部仍按时长降序。
    rows.sort(key=lambda x: (x["date"], -x["total_seconds"]))
    return rows


# ── 时间线 ────────────────────────────────────────────────────

def get_fragments_for_date(target_date=None, include_idle=False):
    """获取某天的所有片段，按时间排序，用于时间线视图。

    include_idle=False（默认）时只返回真实使用片段，不返回空闲段。
    返回的 app_name 已经过归一化处理，可以直接拿去显示或取图标。
    """
    if target_date is None:
        target_date = beijing_today().isoformat()
    query = """SELECT app_name, window_title, exe_path,
                      start_time, end_time, duration_seconds, is_idle
               FROM usage_fragments
               WHERE date = ?"""
    # 参数化查询不能直接拼进 SQL，所以分两步：先判断要不要加条件，再用参数列表传值
    params = [target_date]
    if not include_idle:
        query += " AND is_idle = 0"
    query += " ORDER BY start_time"
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    # 记录时存的是原始进程名，展示前要统一成规范名
    rules = get_app_rules()
    result = []
    for row in rows:
        fragment = dict(row)
        fragment["app_name"] = canonical_app_name(
            fragment["app_name"], rules, fragment.get("exe_path", "")
        )
        result.append(fragment)
    return result


# ── 忽略列表 ──────────────────────────────────────────────────
# 用户可以把某些应用加进「忽略名单」，它们就不再计入统计。

def get_ignored_apps():
    """返回被忽略的应用名集合（全部小写，方便用 in 判断）。"""
    with _get_conn() as conn:
        rows = conn.execute("SELECT app_name FROM ignored_apps").fetchall()
    return {r["app_name"].lower() for r in rows}


def add_ignored_app(app_name):
    """把应用加入忽略名单。OR IGNORE 保证重复添加不会报错。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ignored_apps (app_name) VALUES (?)",
            (app_name.lower(),),
        )


def remove_ignored_app(app_name):
    """把应用移出忽略名单（本来就不在名单里也不会报错）。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            "DELETE FROM ignored_apps WHERE app_name = ?",
            (app_name.lower(),),
        )


# ── 清空数据 ──────────────────────────────────────────────────

def clear_all_data():
    """清空所有使用记录（设置项和忽略名单不受影响）。"""
    with _lock, _get_conn() as conn:
        conn.execute("DELETE FROM usage_fragments")
        conn.execute("DELETE FROM app_usage")


# ── 格式化 ────────────────────────────────────────────────────

def format_duration(seconds):
    """把秒数格式化成中文可读文本，如 "1小时5分钟" / "3分钟" / "42秒"。"""
    seconds = int(seconds)  # 丢弃小数部分
    if seconds < 60:
        return f"{seconds}秒"
    minutes, secs = divmod(seconds, 60)  # divmod 同时得到商和余数
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        # 超过 1 小时时省略秒，因为精确到秒反而看不清重点
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def format_duration_short(seconds):
    """与 format_duration 逻辑一致，供需要「短格式」的界面处调用。"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def get_hourly_stats(target_date=None):
    """获取某天 24 小时的使用统计，返回 [秒数] * 24，每小时上限 3600 秒。

    做法：把每条片段按整点切开，累加到对应的小时格子里。
    比如 13:50~14:10 这段，会给 13 点加 600 秒、给 14 点加 600 秒。

    「每小时上限 3600 秒」是为了防止数据异常（时钟被改、重复记录）时
    出现「某一小时用了 3 小时」这种不可能的值。
    """
    if target_date is None:
        target_date = beijing_today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT start_time, end_time, duration_seconds, is_idle
                   FROM usage_fragments
                   WHERE date = ? AND is_idle = 0
                   ORDER BY start_time""",
            (target_date,),
        ).fetchall()

    hourly = [0.0] * 24  # 下标 0~23 分别代表 0 点~23 点
    for r in rows:
        try:
            s = datetime.fromisoformat(r["start_time"])
            e = datetime.fromisoformat(r["end_time"])
        except Exception:
            continue  # 时间格式坏掉的记录直接跳过
        # 限制在合理范围内
        if (e - s).total_seconds() > 86400:  # 超过一天肯定不正常
            continue
        cur = s
        while cur < e:
            h = cur.hour
            # 本小时的终点 = 下一个整点
            next_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seg_end = min(e, next_hour)
            seg_dur = (seg_end - cur).total_seconds()
            if 0 <= h < 24:  # 防御性判断，理论上 h 一定在范围内
                hourly[h] = min(3600, hourly[h] + seg_dur)
            cur = seg_end  # 继续处理下一小时

    return hourly


def get_week_hourly_stats(end_date=None):
    """获取一周 24 小时的累计使用统计，返回 [秒数] * 24。

    做法很简单：逐天调用 get_hourly_stats 再按小时相加。
    数据量很小（一周最多 7 * 24 个数字），没必要在 SQL 层面优化。
    """
    if end_date is None:
        end_date = beijing_today().isoformat()
    end = date.fromisoformat(end_date)
    start = end - timedelta(days=6)
    result = [0.0] * 24
    cur = start
    while cur <= end:
        hourly = get_hourly_stats(cur.isoformat())
        for h in range(24):
            result[h] += hourly[h]
        cur += timedelta(days=1)  # 日期对象 + timedelta 就是「往后一天」
    return result


def format_time_short(iso_str):
    """ISO 时间字符串 → HH:MM"""
    try:
        from datetime import datetime  # 局部导入（本模块顶部已经导入过 datetime）
        dt = datetime.fromisoformat(iso_str)
        # strftime 的 %H 是 24 小时制小时，%M 是分钟
        return dt.strftime("%H:%M")
    except Exception:
        # 解析失败就原样返回，保证界面不崩
        return iso_str
