"""连接、建库建表，以及给别处用的行转 JSON。

连接策略 —— 每个请求开一条新连接，用完就关：
    为什么不做连接池：这个服务的量级是「几台机器、每天各传一次」，
    大部分时间一个请求都没有，连接开销根本不是瓶颈。而 PyMySQL 的连接
    **不是线程安全的**，共享连接反而要额外加锁、容易出难查的怪问题。
    每个请求独立连接是最简单也最不容易错的。
    哪天量真的上来了，把 connect() 换成池化实现即可，调用方不用改。
"""
import contextlib
import os
import re
from datetime import datetime

import pymysql
from pymysql.cursors import DictCursor

import config

# schema.sql 和本文件在同一个目录（server/db/），跟着服务端一起部署
_SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
REQUIRED_TABLES = frozenset(("users", "usage_records", "mail_log", "device_tokens",
                             "website_usage_records", "website_hourly_usage", "website_intervals",
                             "app_timeline_bins", "app_timeline_intervals", "tracking_uploads",
                             "android_daily_stats"))


# ── 连接 ──────────────────────────────────────────────────────

@contextlib.contextmanager
def connect(with_db=True):
    """开一条数据库连接，退出时自动提交或回滚，然后关闭。

    用法：
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(...)

    正常退出提交，抛异常回滚后把异常继续往外抛（不要悄悄吞掉，
    否则接口会返回成功但数据没写进去）。

    参数：
        with_db：False 表示不选库连接。建库（CREATE DATABASE）时必须用
                 False —— 那时候目标库还不存在，选了会直接连不上。
    """
    conn = pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME if with_db else None,
        charset=config.DB_CHARSET,
        cursorclass=DictCursor,
        # 手动提交：一次上传要写 users + 若干行 usage_records，
        # 必须要么全成功要么全失败，不能写一半。
        autocommit=False,
        connect_timeout=8,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping():
    """探活：能连上并查出一条结果就算健康。给 /health 接口用。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()["ok"] == 1


# ── 建库建表 ──────────────────────────────────────────────────

def _split_statements(sql_text):
    """把 schema.sql 按分号切成一条条语句。

    会先丢掉以 `--` 开头的注释行，再把剩下的按分号切开。

    这是个「够用就好」的实现，不是 SQL 解析器：它不认字符串字面量里的
    分号。所以 schema.sql 里别在 COMMENT 或别的字符串里写分号 ——
    文件开头也写了这条提醒。
    """
    lines = []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def _check_db_name(name):
    """校验库名，挡住拼进 SQL 的注入。

    库名来自环境变量、没法用参数化占位符（DDL 里不支持），只能拼字符串，
    所以这里先白名单校验一遍。库名合法字符就是字母、数字、下划线。
    """
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(
            "非法的数据库名 %r：只能是字母、数字和下划线" % name)
    return name


def init_schema():
    """建库 + 建表。服务端启动时调用一次，重复调用是安全的。

    表结构定义在 schema.sql 里（唯一的一份），这里只是把它执行一遍。
    新表的 user_key 字符集会匹配已有 users 表，兼容旧部署的排序规则。
    运行账号没有 DDL 权限时，先用建表账号初始化，再设置 DB_INIT_SCHEMA=0。
    """
    db_name = _check_db_name(config.DB_NAME)

    if not config.DB_INIT_SCHEMA:
        check_schema()
        return

    # ① 建库。这一步必须不选库连接，因为库还不存在。
    with connect(with_db=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                        (db_name,))
            if cur.fetchone() is None:
                cur.execute(
                    "CREATE DATABASE IF NOT EXISTS `%s` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" % db_name
                )

    # ② 建表。schema.sql 里全是 CREATE TABLE IF NOT EXISTS，重复跑没事。
    with open(_SCHEMA_FILE, encoding="utf-8") as fh:
        statements = _split_statements(fh.read())

    with connect() as conn:
        with conn.cursor() as cur:
            parent_charset = None
            for stmt in statements:
                # Existing users.user_key may use general_ci or another charset.
                # MySQL requires FK columns to match the parent *column*, even
                # when the database default or table default is different.
                if parent_charset is not None:
                    charset, collation = parent_charset
                    stmt = re.sub(r"(`user_key`\s+CHAR\(36\))",
                                  rf"\1 CHARACTER SET {charset} COLLATE {collation}",
                                  stmt, flags=re.IGNORECASE)
                cur.execute(stmt)
                if parent_charset is None:
                    cur.execute("SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users' AND COLUMN_NAME='user_key'",
                                (db_name,))
                    row = cur.fetchone()
                    if not row or not all(re.fullmatch(r"[a-zA-Z0-9_]+", row[k] or "")
                                          for k in ("CHARACTER_SET_NAME", "COLLATION_NAME")):
                        raise RuntimeError("无法读取 users.user_key 字符集，请检查已有表结构")
                    parent_charset = row["CHARACTER_SET_NAME"], row["COLLATION_NAME"]
    check_schema()


def check_schema():
    """Read-only readiness check: report incomplete deployments before serving requests."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                        (config.DB_NAME,))
            found = {row["TABLE_NAME"] for row in cur.fetchall()}
    missing = REQUIRED_TABLES - found
    if missing:
        raise RuntimeError("数据库缺少表：%s；请用建表账号设置 TIMETRACE_DB_INIT_SCHEMA=1 初始化"
                           % ", ".join(sorted(missing)))


def row_to_json(row):
    """把一行查询结果里的 date / datetime 转成 ISO 字符串。

    FastAPI 其实能自己序列化日期，但那样接口返回什么形状就取决于
    FastAPI 的默认行为；这里显式转掉，返回的 JSON 结构更稳定，
    以后换框架也不会变。

    名字不带下划线：users / usage_records / mail_log 三个子模块都要用它，
    跨模块引用别人的私有名不好看，所以它是这个模块的公开接口。
    """
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat(sep=" ", timespec="seconds")
        elif hasattr(v, "isoformat"):      # datetime.date
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
