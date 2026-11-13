"""服务端入口：接收客户端上传的使用记录，并把数据读出来。

部署地址由运行环境配置，不在源码中固定公网地址。

启动方式（在 server/ 目录下）：
    uvicorn app:app --host 0.0.0.0 --port 8000

接口一览：
    GET  /                              接口目录（人看的）
    GET  /api/v1/health                 探活，含数据库连通性
    POST /api/v1/upload                 客户端上传（核心接口）
    GET  /api/v1/usage/{user_key}       读某用户某区间的明细
    GET  /api/v1/summary/{user_key}     读某用户某天的汇总（邮件正文的数据源）
    GET  /api/v1/users                  列出所有用户（需管理口令）
    GET  /api/v1/users/{user_key}       看单个用户（需管理口令）
    POST /api/v1/subscription/{user_key} 设置邮箱 / 订阅开关
    POST /api/v1/admin/daily-mail       手工触发每日邮件（需管理口令）
    GET  /api/v1/admin/mail-log         查发送记录（需管理口令）

本文件现在只做三件事：建 FastAPI 实例、挂 lifespan、把 5 个 router 挂上去。
每个接口的实现都在 api/ 包里按资源分文件（health / upload / usage / users /
admin_mail），路由装饰器上写的是**完整路径**（含 /api/v1 前缀），所以下面
include_router 不加 prefix —— health 那个 router 同时管着 `/` 和
`/api/v1/health`，两者没有共同前缀，加不了。也不加 tags：tags 会改变
OpenAPI 文档内容，而这次重构的验收标准就是「文档逐字节不变」。

⚠️ 关于鉴权 —— 现在的设计和它的边界：

    user_key 是客户端自己生成的 UUID，同时充当「身份」和「凭据」。
    这是个 capability 式的设计：知道这个 UUID 就能读、能写这个用户的数据，
    不知道就碰不到。UUID4 有 122 位随机性，猜不出来，所以只要客户端
    不把它泄露出去，这个方案是站得住的。

    但它**不是账号体系**：
        * 客户端丢了本地数据，就再也找不回这个 key 对应的数据了；
        * key 一旦泄露（比如用户把数据库文件发给别人），数据就裸了，
          而且没有「改密码」这种补救手段 —— 只能换一个新 key，
          旧数据就留在了旧 key 下；
        * 没法防止有人恶意刷别人的 key（虽然也得先知道 key）。

    要正经用的话，下一步应该是：客户端首次上传时由**服务端签发** key，
    或者加一层邮箱验证码换 token。现在这版是「基本可用」，够用但不该
    当成最终形态。

    管理接口（/users、/admin/*）走另一条路：要有 PCTIME_ADMIN_TOKEN
    环境变量才能用，没设就直接 403。校验在 api/deps.py 的 require_admin。
"""
import asyncio
import contextlib
import logging

from fastapi import FastAPI

import config
from api import admin_mail, app_update, health, upload, usage, users, tracking
# 版本号在 api/health.py 里定义（index() 要报它，放这里会和 api.health 成环）
from api.health import SERVICE_VERSION
from db import conn as db_conn
from mail import scheduler as mail_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pctime")


# ── 应用生命周期 ──────────────────────────────────────────────

@contextlib.asynccontextmanager
async def lifespan(_app):
    """启动时建库建表，并按需拉起每日邮件定时器。

    放在 lifespan 里而不是模块顶层，是为了让「导入 app 模块」这件事
    不产生副作用 —— 测试、脚本里 import 一下不该就去动数据库、
    更不该起一个会往外发邮件的线程。
    """
    # 先报配置从哪来：.env 写错了（键名少个前缀、BOM 没除干净）的表现是
    # 「配了却不生效」，只打日志看不出所以然，所以把文件名摊在启动日志里。
    log.info("配置来源：%s", config.ENV_FILE if config.ENV_FILE_LOADED
             else "未找到 %s，全部用内置默认值" % config.ENV_FILE)

    db_conn.init_schema()
    log.info("数据库就绪：%s@%s:%d/%s",
             config.DB_USER, config.DB_HOST, config.DB_PORT, config.DB_NAME)

    # 没配 SMTP 时返回 None，服务端行为和以前完全一样
    scheduler = mail_scheduler.start_scheduler()
    try:
        yield
    finally:
        if scheduler is not None:
            # stop() 里是 join(timeout)，会阻塞。扔到线程里等，
            # 免得关服务时把事件循环卡住几秒。
            await asyncio.to_thread(scheduler.stop)


app = FastAPI(
    title="TimeTrace 服务端",
    description="接收客户端上传的屏幕使用时间，并按用户区分存储。",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)


# ── 挂路由 ────────────────────────────────────────────────────
#
# 这个顺序 = 原来这些接口在 app.py 里的声明顺序。
# 顺序会影响 OpenAPI 里的 operationId 和 /docs 页面的排序，所以别随手调。

app.include_router(health.router)      # GET  /                            GET  /api/v1/health
app.include_router(upload.router)      # POST /api/v1/upload
app.include_router(usage.router)       # GET  .../usage/{user_key}         GET  .../summary/{user_key}
app.include_router(users.router)       # GET  .../users                    GET  .../users/{user_key}
                                       # POST .../subscription/{user_key}
app.include_router(admin_mail.router)  # POST .../admin/daily-mail         GET  .../admin/mail-log
app.include_router(tracking.router)
app.include_router(app_update.router)  # GET  /api/v1/app-version          GET  /api/v1/app-download
