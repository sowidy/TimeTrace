"""客户端上传接口（POST /api/v1/upload）—— 整个服务最核心的一条。

鉴权说明见 app.py 顶部那段：user_key 同时充当身份和凭据。
"""
import logging
from datetime import datetime

import pymysql
from fastapi import APIRouter, HTTPException

import config
from api.schemas import UploadRequest, UploadResponse
from db import conn as db_conn
from db import usage_records as db_usage_records
from db import users as db_users

log = logging.getLogger("timetrace")

router = APIRouter()


@router.post("/api/v1/upload", response_model=UploadResponse)
def upload(req: UploadRequest):
    """客户端上传某几天（通常是补传的那几天）的使用记录。

    幂等：同一天重复上传只会覆盖，不会累加。所以客户端可以放心重试，
    断网重传也不会让服务端的数据翻倍。
    """
    if not req.days:
        raise HTTPException(status_code=400, detail="days 不能为空")

    if len(req.days) > config.MAX_DAYS_PER_UPLOAD:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多上传 {config.MAX_DAYS_PER_UPLOAD} 天，收到 {len(req.days)} 天",
        )
    for d in req.days:
        if len(d.apps) > config.MAX_APPS_PER_DAY:
            raise HTTPException(
                status_code=413,
                detail=f"{d.day} 有 {len(d.apps)} 个应用，"
                       f"超过单日上限 {config.MAX_APPS_PER_DAY}",
            )

    now = datetime.now()
    days_written = 0
    apps_written = 0

    try:
        with db_conn.connect() as conn:
            with conn.cursor() as cur:
                # 先登记用户，再写明细 —— schema 里有外键，顺序反了会插入失败
                db_users.upsert_user(
                    cur, req.user_key, req.hostname, req.platform,
                    req.client_version, email=req.email, now=now,
                )
                for d in req.days:
                    # 某一天一个应用都没有：跳过，不要写。
                    # 如果照常执行 replace_day，会先把服务端已有的删除再插 0 行，
                    # 等于把数据清了。客户端正常不会传空的一天（没数据就不传），
                    # 所以这里当成异常情况处理，保守地什么都不做。
                    if not d.apps:
                        log.warning("上传里 %s 没有任何应用，跳过", d.day)
                        continue
                    n = db_usage_records.replace_day(
                        cur, req.user_key, d.day,
                        [a.model_dump() for a in d.apps], now=now,
                    )
                    # A new snapshot invalidates the prior day-end confirmation, even
                    # if its total happens to match. The authenticated completion
                    # endpoint confirms the newly uploaded snapshot separately.
                    cur.execute("UPDATE tracking_uploads SET complete=FALSE WHERE user_key=%s "
                                "AND day=%s AND kind='apps'", (req.user_key, d.day))
                    days_written += 1
                    apps_written += n
    except pymysql.MySQLError as e:
        # 只兜数据库异常。别的异常（比如代码写错了）要继续抛出去，
        # 否则会被伪装成 503，排查时找不到真正的原因。
        log.error("写入数据库失败 user_key=%s: %s", req.user_key, e)
        raise HTTPException(status_code=503, detail=f"数据库写入失败：{type(e).__name__}")

    log.info("上传成功 user_key=%s 天=%d 应用行=%d 带邮箱=%s",
             req.user_key, days_written, apps_written, bool(req.email))

    return UploadResponse(
        ok=True,
        user_key=req.user_key,
        days_written=days_written,
        apps_written=apps_written,
        email_saved=bool(req.email),
    )
