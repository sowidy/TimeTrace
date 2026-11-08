"""mail_log 表：某天的报告发过没有、发的结果是什么。

「今天到底发过没有」这件事必须落在数据库里，不能只放在内存：
服务器半夜重启过、或者手工触发和定时器撞在一起时，内存里的标记就没了，
用户会收到两封一模一样的报告。

日报和周报是两种模板，同一天可以各发一封，所以去重键是
(user_key, day, weekly)：weekly=0 是日报，weekly=1 是周报。
报告种类不再是「这天是星期几」的纯函数 —— 调用方可以显式指定
weekly 覆盖默认（默认仍按星期几：周日发周报），见 mail/sender.py。
"""
from datetime import datetime

from db import conn as db_conn

MAIL_SENT = "sent"
MAIL_FAILED = "failed"


def was_mail_sent(user_key, day, weekly=False):
    """这一天这种报告是不是已经成功发出去了。

    只认 status='sent'：失败记录不算「发过」，所以下次运行还会重试。
    weekly 区分日报/周报：同一天可以各发一封。
    """
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM mail_log WHERE user_key = %s AND day = %s AND weekly = %s",
                (user_key, day, 1 if weekly else 0),
            )
            row = cur.fetchone()
            return bool(row) and row["status"] == MAIL_SENT


def record_mail(user_key, day, email, status, detail="", now=None, weekly=False):
    """记一笔发送结果。同一天同一种报告重复记录会覆盖上一条。

    detail 是给人和给后台看的，不是给邮件用的，所以这里截断到列宽 ——
    SMTP 报错信息有时候很长（带着完整的服务器应答），不截断会直接
    因为「Data too long」把这条记录也写失败，那就连「发失败了」都查不到。
    """
    now = now or datetime.now()
    cur_detail = (detail or "")[:512]
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mail_log
                    (user_key, day, weekly, email, status, detail, sent_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    email   = VALUES(email),
                    status  = VALUES(status),
                    detail  = VALUES(detail),
                    sent_at = VALUES(sent_at)
                """,
                (user_key, day, 1 if weekly else 0, email or "", status, cur_detail, now),
            )


def list_mail_log(day=None, limit=200):
    """查发送记录，最近的排前面。给管理接口用（排查「为什么没收到」）。"""
    sql = """
        SELECT user_key, day, email, status, detail, sent_at
        FROM mail_log
    """
    params = []
    if day is not None:
        sql += " WHERE day = %s"
        params.append(day)
    sql += " ORDER BY sent_at DESC LIMIT %s"
    params.append(int(limit))

    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [db_conn.row_to_json(r) for r in cur.fetchall()]
