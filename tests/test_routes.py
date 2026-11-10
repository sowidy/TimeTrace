"""server/ 的路由表护栏。

为什么专门有这么一个文件：整套测试里，只有这个文件碰 app.py。
拆 app.py 那次重构（10 个接口从一个大文件散到 api/ 的 5 个模块）最大的风险
就是「路径悄悄变了 / 少挂了一个 router / 某个管理接口忘了带口令校验」——
这三件事任何一个都不会让别的测试变红，但线上会直接出事。

两个测试分别在守：
    * 路径、方法、顺序 —— 逐条对齐。顺序也管，因为它决定 /docs 的排列和
      OpenAPI 里 operationId 的生成顺序；
    * 管理口令 —— 有且只有 4 个接口必须校验。多一个少一个都是 bug：
      少了是安全漏洞，多了（尤其是 subscription）会让客户端再也改不了
      自己的订阅。

**不连数据库、不起服务**：只读路由表，不调任何接口实现。
"""
import unittest

import app
from api.health import SERVICE_VERSION
from fastapi.routing import _EffectiveRouteContext, _IncludedRouter


def business_routes():
    """本服务自己声明的路由，摊平后按注册顺序返回。

    要摊平是因为 include_router 存的是一个惰性节点（_IncludedRouter），
    直接遍历 app.routes 只能看到 5 个空壳，看不到里面的真实路径。
    顺便把 /docs、/openapi.json 这些框架自带的 Route 过滤掉 ——
    它们是 Starlette 的 Route 而不是 _EffectiveRouteContext，顺手就区分开了。
    """
    out = []

    def walk(routes):
        for r in routes:
            if isinstance(r, _IncludedRouter):
                walk(r.effective_candidates())
            elif isinstance(r, _EffectiveRouteContext):
                out.append(r)

    walk(app.app.routes)
    return out


# (方法, 路径, 函数名)。顺序 = 注册顺序 = /docs 里的排列顺序。
EXPECTED_ROUTES = [
    ("GET", "/", "index"),
    ("GET", "/api/v1/health", "health"),
    ("POST", "/api/v1/upload", "upload"),
    ("GET", "/api/v1/usage/{user_key}", "read_usage"),
    ("GET", "/api/v1/summary/{user_key}", "read_summary"),
    ("GET", "/api/v1/users", "read_users"),
    ("GET", "/api/v1/users/{user_key}", "read_user"),
    ("POST", "/api/v1/subscription/{user_key}", "update_subscription"),
    ("POST", "/api/v1/admin/daily-mail", "trigger_daily_mail"),
    ("GET", "/api/v1/admin/mail-log", "read_mail_log"),
    ("POST", "/api/v1/device/register", "register_device"),
    ("DELETE", "/api/v1/admin/device-token/{user_key}", "reset_device_token"),
    ("POST", "/api/v1/web-usage/upload", "upload_web"),
    ("POST", "/api/v1/app-timeline/upload", "upload_timeline"),
    ("POST", "/api/v1/app-summary/complete", "complete_apps"),
    ("GET", "/api/v1/web-usage/{user_key}", "read_web"),
    ("GET", "/api/v1/web-usage-summary/{user_key}", "web_summary"),
    ("GET", "/api/v1/web-hourly/{user_key}", "read_web_hourly"),
    ("GET", "/api/v1/web-intervals/{user_key}", "read_web_intervals"),
    ("GET", "/api/v1/app-timeline/{user_key}", "read_timeline"),
    ("GET", "/api/v1/app-intervals/{user_key}", "read_app_intervals"),
    ("GET", "/api/v1/merged-timeline/{user_key}", "read_merged_timeline"),
]

# 必须校验 X-Admin-Token 的接口。**故意不含** subscription：
# 客户端手里只有自己的 user_key，没有管理口令，那条接口加了口令就废了。
ADMIN_PATHS = {
    "/api/v1/users",
    "/api/v1/users/{user_key}",
    "/api/v1/admin/daily-mail",
    "/api/v1/admin/mail-log",
    "/api/v1/admin/device-token/{user_key}",
}


class TestRouteTable(unittest.TestCase):
    def test_routes_are_exactly_as_before_the_split(self):
        """路径、方法、函数名、顺序，逐条对齐。"""
        actual = [(sorted(r.methods), r.path, r.name)
                  for r in business_routes()]
        expected = [(sorted({m}), p, n) for m, p, n in EXPECTED_ROUTES]
        self.assertEqual(actual, expected)

    def test_route_count_is_not_silently_changed(self):
        # 上一条用的是列表比较，已经能发现多挂/漏挂；这条是给人看的一句话。
        self.assertEqual(len(business_routes()), len(EXPECTED_ROUTES))

    def test_only_the_four_admin_routes_carry_the_token_dependency(self):
        gated = {r.path for r in business_routes() if r.dependencies}
        self.assertEqual(gated, ADMIN_PATHS)

    def test_subscription_is_reachable_without_the_admin_token(self):
        """单独盯住这一条 —— 它是唯一一个「看起来像管理接口但不是」的。

        客户端改邮箱 / 退订走的就是它（client/net/uploader.py 的
        flush_subscription），一旦有人「顺手统一加个 Depends」，
        用户在设置页点保存就会开始收到 403。
        """
        sub = [r for r in business_routes()
               if r.path == "/api/v1/subscription/{user_key}"]
        self.assertEqual(len(sub), 1)
        self.assertFalse(sub[0].dependencies)

    def test_upload_is_open(self):
        # 上传接口靠 user_key 本身当凭据，不校验管理口令
        up = [r for r in business_routes() if r.path == "/api/v1/upload"]
        self.assertEqual(len(up), 1)
        self.assertFalse(up[0].dependencies)


class TestServiceVersion(unittest.TestCase):
    def test_one_source_of_truth(self):
        """版本号只有一份来源，index() 和 FastAPI 元数据都读它。

        这条守的是那个循环导入的修复：index() 原来返回 app.version，
        搬到 api/health.py 之后不能再反过来 import app（会成环），
        于是拆成了 SERVICE_VERSION 常量。两处一旦漂移，
        GET / 报的版本和 /docs 上标的就不是一个数了。
        """
        self.assertEqual(app.app.version, SERVICE_VERSION)

    def test_index_reports_it(self):
        from api.health import index

        body = index()
        self.assertEqual(body["service"], "pctime-server")
        self.assertEqual(body["version"], SERVICE_VERSION)

    def test_index_endpoint_list_matches_route_table(self):
        """目录里列的路由要真的存在 —— 免得改了路径忘了改这段文案。"""
        from api.health import index

        listed = [line.split()[1].split("?")[0]
                  for line in index()["endpoints"]]
        real = {r.path for r in business_routes()}
        real.discard("/")                   # 目录本身不列自己
        self.assertEqual(set(listed), real)


if __name__ == "__main__":
    unittest.main()
