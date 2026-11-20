"""路由的公共依赖。现在只有一条：管理接口的口令校验。

单独一个文件，是因为 api/users.py 和 api/admin_mail.py 都要用它 ——
放在哪一边都会让另一边 import 一个「别人的」模块。
"""
import hmac

from fastapi import Header, HTTPException

import config


def require_admin(x_admin_token: str = Header(default="")):
    """管理接口的口令校验。

    没配口令时直接 403 —— 这是刻意的默认行为：宁可管理接口不能用，
    也不要它默认裸奔在公网上。

    比对用 hmac.compare_digest 而不是 == ：字符串的 == 是短路比较，
    会随「前几个字符对不对」而快慢不同，理论上能被时序攻击一个字符
    一个字符地试出口令。compare_digest 的耗时与内容无关。
    """
    if not config.ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="管理接口未启用：服务端没有设置 TIMETRACE_ADMIN_TOKEN",
        )
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="X-Admin-Token 不正确")
