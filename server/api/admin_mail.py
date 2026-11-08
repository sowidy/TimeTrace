"""报告邮件的两个管理接口：手工触发、查发送记录。

模块名叫 admin_mail 而不是 mail，是为了不和顶层那个 mail/ 包（真正发信的
那一套）看混 —— 这里只有两个 HTTP 入口，不含任何发信逻辑。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import require_admin
from db import mail_log as db_mail_log
from mail import sender as mail_sender

router = APIRouter()


@router.post("/api/v1/admin/daily-mail", dependencies=[Depends(require_admin)])
def trigger_daily_mail(
    day: date | None = Query(None, description="发哪一天，默认昨天"),
    catchup: int = Query(0, ge=0, le=7,
                         description="额外补发前几天（漏发时用）"),
    dry_run: bool = Query(False, description="只渲染不发送，用来检查内容"),
    user_key: str | None = Query(None, description="只给这一个设备发，忽略订阅开关"),
    weekly: bool | None = Query(None, description="报告种类：true 发周报，false 发日报，不传按星期几默认（周日发周报）"),
):
    """手工触发报告邮件。

    日报和周报是两种模板，任何一天都可以套用任一种：weekly=true 发周报
    （覆盖「以该天结尾的 7 天」），weekly=false 发当天日报，不传则按星期几
    默认（周日发周报）。mail_log 按 (user_key, day, weekly) 去重，同一天
    日报周报可以各发一封。

    两个用途：
        1. 外部 cron 定时（那就把 PCTIME_MAIL_SCHEDULER 关掉，免得
           进程内定时器和 cron 重复发）。cron 不需要知道周报这回事：
           每天 00:03 调用即可；默认发送昨天，昨天是周报日时自然生成周报。
        2. 排查问题：dry_run=true 先看看内容长什么样，再决定要不要真发。
           results 里每条的 `weekly` 字段会告诉你这一封是哪种。

    这是**同步**接口，一轮发送可能要几十秒（每个收件人一次 SMTP 往返）。
    没关系：FastAPI 会把同步接口丢到线程池里跑，不会卡住别的请求。
    """
    result = mail_sender.run(
        day=day, catchup_days=catchup, dry_run=dry_run, user_key=user_key,
        weekly=weekly,
    )
    if result.get("reason") == "disabled":
        raise HTTPException(
            status_code=503,
            detail="服务端没有配置 SMTP（需要 PCTIME_SMTP_HOST 和 MAIL_FROM）",
        )
    if result.get("reason") == "no_such_user":
        raise HTTPException(status_code=404, detail="没有这个 user_key")
    return result


@router.get("/api/v1/admin/mail-log", dependencies=[Depends(require_admin)])
def read_mail_log(
    day: date | None = Query(None, description="只看某一天"),
    limit: int = Query(200, ge=1, le=1000),
):
    """查每日邮件的发送记录。用户说「没收到」时先看这里。"""
    return {"records": db_mail_log.list_mail_log(day=day, limit=limit)}
