"""服务目录（GET /）和探活（GET /api/v1/health）。"""
import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config
from api.schemas import HealthResponse
from db import conn as db_conn

# 服务版本号。**唯一的一份来源**：app.py 里的 FastAPI(version=...) 也读它。
#
# 放在这里而不是 app.py，是为了解决一个循环导入：index() 要报出当前版本，
# 但它住在 api/health.py 里，如果它去 import app 取 app.version，就变成了
# api.health → app，而 app 又必须 import api.health 才能把路由挂上去。
# 拆成一个常量，两边都往下依赖它，环就断了。
SERVICE_VERSION = "1.1.14"

log = logging.getLogger("pctime")

router = APIRouter()


@router.get("/")
def index():
    """纯给人看的接口目录，方便用浏览器打开确认服务活着。"""
    return {
        "service": "pctime-server",
        "version": SERVICE_VERSION,
        "endpoints": [
            "GET  /api/v1/health",
            "POST /api/v1/upload",
            "GET  /api/v1/usage/{user_key}?start=YYYY-MM-DD&end=YYYY-MM-DD",
            "GET  /api/v1/summary/{user_key}?day=YYYY-MM-DD",
            "GET  /api/v1/users                     (需要 X-Admin-Token)",
            "GET  /api/v1/users/{user_key}          (需要 X-Admin-Token)",
            "POST /api/v1/subscription/{user_key}",
            "POST /api/v1/admin/daily-mail          (需要 X-Admin-Token)",
            "GET  /api/v1/admin/mail-log            (需要 X-Admin-Token)",
            "POST /api/v1/device/register",
            "DELETE /api/v1/admin/device-token/{user_key}",
            "POST /api/v1/web-usage/upload",
            "POST /api/v1/app-timeline/upload",
            "POST /api/v1/app-summary/complete",
            "GET /api/v1/web-usage/{user_key}",
            "GET /api/v1/web-usage-summary/{user_key}",
            "GET /api/v1/web-hourly/{user_key}",
            "GET /api/v1/web-intervals/{user_key}",
            "GET /api/v1/app-timeline/{user_key}",
            "GET /api/v1/app-intervals/{user_key}",
            "GET /api/v1/merged-timeline/{user_key}",
        ],
        "docs": "/docs",
    }


@router.get("/api/v1/health", response_model=HealthResponse)
def health():
    """探活。数据库连不上就返回 503，让负载均衡/监控能看出来。

    返回 200 但 database=false 那种写法更「友好」，但会让健康检查失去
    意义 —— 监控看到 200 就以为没事了。所以这里如实返回 503。
    """
    try:
        db_ok = db_conn.ping()
    except Exception as e:                       # noqa: BLE001
        log.warning("健康检查：数据库连不上：%s", e)
        db_ok = False

    body = HealthResponse(
        ok=db_ok,
        database=db_ok,
        db_name=config.DB_NAME,
        server_time=datetime.now().isoformat(sep=" ", timespec="seconds"),
    )
    if not db_ok:
        return JSONResponse(status_code=503, content=body.model_dump())
    return body
