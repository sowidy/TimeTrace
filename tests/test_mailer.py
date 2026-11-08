"""server/mail/ 的单元测试。

这个文件测的是**服务端**的模块。它们和客户端那几个模块一样是扁平导入
（`import config`），靠 tests/__init__.py 里的 sys.path 把 server/ 加进来，
见那个文件的注释。

两条纪律：
    * 不碰数据库 —— db 包里每个函数都被换成了假实现；
    * 不发真邮件 —— smtplib 和 sender.send_mail 都是假的。
      跑完这套测试不该产生任何网络请求。

模块划分见 server/mail/__init__.py：渲染在 report、发信在 sender、
定时器在 scheduler。哪里被 patch 是照着「被测代码实际去查哪个模块」定的。
"""
import unittest
from datetime import date, datetime, time, timedelta
from io import BytesIO
from unittest import mock

from email.generator import BytesGenerator

import config
# 数据层是按表拆开的包、邮件层是按责任拆开的包。
# patch 的目标必须和被测代码查找的位置**完全一致**：sender.py 里查的是
# `mail_log.was_mail_sent`，所以要 patch `db.mail_log` 上的那个名字，
# patch 包 `db` 是没用的（包上根本没有这个名字，会直接 AttributeError）。
from db import mail_log, usage_records, users
from mail import report, scheduler, sender, tracking_report

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
MAIL1 = "u1@example.com"
MAIL2 = "u2@example.com"


def make_summary(total_seconds=None):
    """一份形状和 usage_records.get_daily_summary() 返回值一致的假数据。

    总时长 13500 秒 = 3 小时 45 分。
    """
    apps = [
        {"app_name": "pycharm64.exe", "display_name": "PyCharm",
         "seconds": 9000.0, "launches": 12},
        {"app_name": "chrome.exe", "display_name": "Chrome",
         "seconds": 3900.0, "launches": 34},
        # display_name 是空的：渲染时要退回用 app_name，不能显示成空
        {"app_name": "wechat.exe", "display_name": "",
         "seconds": 600.0, "launches": 5},
    ]
    total = sum(a["seconds"] for a in apps) if total_seconds is None \
        else total_seconds
    return {"day": TODAY.isoformat(), "total_seconds": total,
            "app_count": len(apps), "apps": apps}


# 「按星期分档」用的固定日期。刻意**不用** date.today()：那个值随哪天跑测试而变，
# 「周日」还是「周三」就不确定了，测试会时红时绿。
MONDAY = date(2026, 10, 12)
SUNDAY = date(2026, 10, 18)      # 默认的周报日，这一周的最后一天


def make_week_summary(total_seconds=None):
    """一份形状和 usage_records.get_range_summary() 返回值一致的假数据。

    比 make_summary 多 start / end / days_with_data 三个键；apps 里那些秒数是
    **跨天聚合**后的（同一个应用整周的总和），不是某一天的。
    总时长 27000 秒 = 7 小时 30 分，PyCharm 占 18000 / 27000 = 66.7%。
    """
    apps = [
        {"app_name": "pycharm64.exe", "display_name": "PyCharm",
         "seconds": 18000.0, "launches": 40},
        {"app_name": "chrome.exe", "display_name": "Chrome",
         "seconds": 7800.0, "launches": 90},
        # display_name 是空的：渲染时要退回用 app_name，不能显示成空
        {"app_name": "wechat.exe", "display_name": "",
         "seconds": 1200.0, "launches": 15},
    ]
    total = sum(a["seconds"] for a in apps) if total_seconds is None \
        else total_seconds
    return {"start": MONDAY.isoformat(), "end": SUNDAY.isoformat(),
            "days_with_data": 5, "total_seconds": total,
            "app_count": len(apps), "apps": apps}


class TestFormatDuration(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(report.format_duration(0), "0 秒")
        self.assertEqual(report.format_duration(45), "45 秒")
        self.assertEqual(report.format_duration(59.4), "59 秒")

    def test_minutes(self):
        self.assertEqual(report.format_duration(60), "1 分")
        self.assertEqual(report.format_duration(90), "1 分 30 秒")
        self.assertEqual(report.format_duration(3599), "59 分 59 秒")

    def test_hours(self):
        self.assertEqual(report.format_duration(3600), "1 小时")
        # 整小时不带「0 分」的零头
        self.assertEqual(report.format_duration(7200), "2 小时")
        self.assertEqual(report.format_duration(13500), "3 小时 45 分")

    def test_none_and_float_rounding(self):
        # 秒数是 DOUBLE，传进来可能是 89.6，不该出现「1 分 29.6 秒」
        self.assertEqual(report.format_duration(None), "0 秒")
        self.assertEqual(report.format_duration(89.6), "1 分 30 秒")


class TestParseSendTime(unittest.TestCase):
    def test_default_is_three_minutes_after_midnight(self):
        self.assertEqual(scheduler.DEFAULT_SEND_TIME, time(0, 3))

    def test_full_and_short_forms(self):
        self.assertEqual(scheduler.parse_send_time("23:59:59"), time(23, 59, 59))
        self.assertEqual(scheduler.parse_send_time(" 09:30 "), time(9, 30, 0))

    def test_bad_value_falls_back_instead_of_raising(self):
        # 配置写错不该让服务起不来 —— 退回默认值，日志里有告警
        with self.assertLogs("pctime.mail.scheduler", level="WARNING") as caught:
            self.assertEqual(scheduler.parse_send_time("不存在的时刻"),
                             scheduler.DEFAULT_SEND_TIME)
        self.assertIn("PCTIME_MAIL_TIME", "\n".join(caught.output))

    def test_empty_value_falls_back(self):
        self.assertEqual(scheduler.parse_send_time(""), scheduler.DEFAULT_SEND_TIME)
        self.assertEqual(scheduler.parse_send_time(None), scheduler.DEFAULT_SEND_TIME)


class TestBuildMessage(unittest.TestCase):
    def test_subject_has_date_and_total(self):
        subject = report.build_subject(TODAY, make_summary())
        self.assertIn(TODAY.isoformat(), subject)
        self.assertIn("3 小时 45 分", subject)

    def test_text_lists_apps_with_share_and_footer(self):
        text = report.build_text(TODAY, make_summary())
        self.assertIn(TODAY.isoformat(), text)
        self.assertIn("3 小时 45 分", text)
        self.assertIn("PyCharm", text)
        self.assertIn("2 小时 30 分", text)
        # 9000 / 13500 = 66.7%
        self.assertIn("66.7%", text)
        self.assertIn("启动 12 次", text)
        # display_name 为空时要退回进程名
        self.assertIn("wechat.exe", text)
        self.assertIn(report.FOOTER_TEXT, text)

    def test_device_name_shown_only_when_known(self):
        with_host = report.build_text(TODAY, make_summary(),
                                      {"hostname": "DESKTOP-ABC"})
        self.assertIn("DESKTOP-ABC", with_host)
        self.assertNotIn("设备：", report.build_text(TODAY, make_summary()))

    def test_long_list_is_folded(self):
        summary = make_summary()
        starts = (0, 9000, 12900)
        intervals = [{"day": str(TODAY), "app_name": app["app_name"],
                      "start_second": start, "end_second": start + app["seconds"]}
                     for app, start in zip(summary["apps"], starts)]
        with mock.patch.object(config, "MAIL_MAX_APPS", 2):
            text = report.build_text(TODAY, summary)
            html_body = tracking_report.append_sections(report.build_message(TODAY, summary),
                summary, [], [], TODAY, TODAY, app_intervals=intervals)[2]
        self.assertIn("PyCharm", text)
        self.assertNotIn("wechat.exe", text)
        self.assertIn("另有 1 个应用", text)
        self.assertIn("PyCharm", html_body)
        self.assertNotIn("wechat.exe", html_body)
        self.assertIn("另有 1 个软件", html_body)
        self.assertIn("10 分", html_body)

    def test_html_escapes_app_names(self):
        # 应用名来自客户端上报，是外部输入。不转义的话，一个叫
        # `<script>` 的进程就能往收件人邮箱里注入 HTML。
        evil = {
            "day": TODAY.isoformat(), "total_seconds": 60.0, "app_count": 1,
            "apps": [{"app_name": "x.exe",
                      "display_name": '<script>alert("x")</script>',
                      "seconds": 60.0, "launches": 1}],
        }
        html_body = tracking_report.append_sections(report.build_message(TODAY, evil),
            evil, [], [], TODAY, TODAY,
            app_intervals=[{"day": str(TODAY), "app_name": "x.exe",
                            "start_second": 0, "end_second": 60}])[2]
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_html_escapes_hostname(self):
        html_body = report.build_html(TODAY, make_summary(),
                                      {"hostname": "<b>x</b>"})
        self.assertNotIn("<b>x</b>", html_body)
        self.assertIn("&lt;b&gt;", html_body)

    def test_build_message_returns_three_parts(self):
        subject, text, html_body = report.build_message(TODAY, make_summary())
        self.assertTrue(subject)
        self.assertTrue(text)
        self.assertIn("<!DOCTYPE html>", html_body)


class TestSendMail(unittest.TestCase):
    """测 SMTP 那一层：不真的连网，把 smtplib 换成假的。"""

    def _patch_config(self, **overrides):
        base = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": 465,
            "SMTP_USER": "sender@example.com",
            "SMTP_PASSWORD": "授权码",
            "MAIL_FROM": "sender@example.com",
            "MAIL_FROM_NAME": "TimeTrace 使用报告",
        }
        base.update(overrides)
        return base

    def test_ssl_path_logs_in_and_sends_alternative(self):
        server = mock.MagicMock()
        patches = [mock.patch.object(config, k, v)
                   for k, v in self._patch_config(SMTP_USE_SSL=True).items()]
        with mock.patch.object(sender.smtplib, "SMTP_SSL",
                               return_value=server) as ctor:
            for p in patches:
                p.start()
            try:
                sender.send_mail(MAIL1, "主题", "纯文本", "<p>HTML</p>")
            finally:
                for p in patches:
                    p.stop()

        ctor.assert_called_once_with("smtp.example.com", 465,
                                     timeout=config.SMTP_TIMEOUT)
        server.login.assert_called_once_with("sender@example.com", "授权码")
        server.send_message.assert_called_once()

        msg = server.send_message.call_args[0][0]
        self.assertEqual(msg["To"], MAIL1)
        # 中文主题必须编码，否则会出现乱码或被服务器拒收。
        # msg["Subject"] 是 Header 对象：str() 给的是解码后的原文，
        # encode() 才是会被写进邮件头的那份编码形式。
        subject = msg["Subject"]
        self.assertEqual(str(subject), "主题")
        self.assertIn("=?utf-8?", subject.encode().lower())
        # 纯文本和 HTML 两个部分都在。纯文本放前面：客户端从后往前挑自己
        # 认识的那种，所以「更高级」的 HTML 要排在后面。
        self.assertEqual([p.get_content_type() for p in msg.get_payload()],
                         ["text/plain", "text/html"])

    def test_serialized_subject_is_rfc2047_encoded(self):
        """真的生成一遍字节流，确认主题落到邮件头里还是合法的。

        上面那个测试只看到 Header 对象。这里走一次 BytesGenerator ——
        那才是 send_message 实际发出去的形态 —— 确保中文主题没有被
        序列化成裸 UTF-8（那会被收件方判成乱码或直接拒信）。
        """
        server = mock.MagicMock()
        patches = [mock.patch.object(config, k, v)
                   for k, v in self._patch_config(SMTP_USE_SSL=True).items()]
        with mock.patch.object(sender.smtplib, "SMTP_SSL",
                               return_value=server):
            for p in patches:
                p.start()
            try:
                sender.send_mail(MAIL1, "【报告】3 小时 45 分", "t", "<p>h</p>")
            finally:
                for p in patches:
                    p.stop()

        msg = server.send_message.call_args[0][0]
        raw = BytesIO()
        BytesGenerator(raw).flatten(msg)
        head = raw.getvalue().split(b"\n\n", 1)[0]

        subject_line = [ln for ln in head.split(b"\n")
                        if ln.startswith(b"Subject: ")]
        self.assertTrue(subject_line, head)
        self.assertIn(b"=?utf-8?", subject_line[0])
        # 编码之后，原始的中文不该以裸字节出现在邮件头里
        self.assertNotIn("报告".encode("utf-8"), head)

    def test_starttls_path_ehlos_twice(self):
        server = mock.MagicMock()
        patches = [mock.patch.object(config, k, v)
                   for k, v in self._patch_config(
                       SMTP_USE_SSL=False, SMTP_PORT=587).items()]
        with mock.patch.object(sender.smtplib, "SMTP",
                               return_value=server) as ctor:
            for p in patches:
                p.start()
            try:
                sender.send_mail(MAIL1, "主题", "纯文本", "<p>HTML</p>")
            finally:
                for p in patches:
                    p.stop()

        ctor.assert_called_once_with("smtp.example.com", 587,
                                     timeout=config.SMTP_TIMEOUT)
        server.starttls.assert_called_once()
        # STARTTLS 前后各 ehlo 一次：升级加密层之后服务器宣告的能力可能变了
        self.assertEqual(server.ehlo.call_count, 2)

    def test_no_login_when_user_not_configured(self):
        server = mock.MagicMock()
        patches = [mock.patch.object(config, k, v)
                   for k, v in self._patch_config(SMTP_USER="",
                                                  MAIL_FROM="noreply@x.com",
                                                  SMTP_USE_SSL=True).items()]
        with mock.patch.object(sender.smtplib, "SMTP_SSL",
                               return_value=server):
            for p in patches:
                p.start()
            try:
                sender.send_mail(MAIL1, "s", "t", "<p>h</p>")
            finally:
                for p in patches:
                    p.stop()
        server.login.assert_not_called()

    def test_smtp_error_propagates(self):
        # 发送失败必须往外抛，由 send_day 记进 mail_log；
        # 在这里悄悄吞掉的话，用户收不到邮件而且查不出原因。
        server = mock.MagicMock()
        server.send_message.side_effect = OSError("连接被拒绝")
        patches = [mock.patch.object(config, k, v)
                   for k, v in self._patch_config(SMTP_USE_SSL=True).items()]
        with mock.patch.object(sender.smtplib, "SMTP_SSL",
                               return_value=server):
            for p in patches:
                p.start()
            try:
                with self.assertRaises(OSError):
                    sender.send_mail(MAIL1, "s", "t", "<p>h</p>")
            finally:
                for p in patches:
                    p.stop()


class MailerTestCase(unittest.TestCase):
    """公共脚手架：把 db 全部换成假实现，并假设邮件功能是开着的。"""

    def setUp(self):
        patcher = mock.patch.object(config, "MAIL_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

        from db import tracking
        self._patch(tracking, "read_range", return_value=[])
        self._patch(tracking, "readiness", return_value=[])

        self.was_sent = self._patch(mail_log, "was_mail_sent", return_value=False)
        self.record_mail = self._patch(mail_log, "record_mail")
        # 形状和 users.get_user() 真实返回值一致（那是个 DictCursor 行）
        self.get_user = self._patch(
            users, "get_user",
            return_value={"user_key": U1, "email": MAIL1,
                          "hostname": "DESKTOP-ABC", "subscribed": 1})
        self.get_summary = self._patch(usage_records, "get_daily_summary",
                                       return_value=make_summary())
        # 周报那条路走的是区间查询。两个都换上假的，跑测试时不该碰数据库
        # —— 也就不该依赖「今天恰好有数据」这种运气。
        self.get_range = self._patch(usage_records, "get_range_summary",
                                     return_value=make_week_summary())
        self.get_subscribers = self._patch(users, "get_subscribers",
                                           return_value=[])
        self.send_mail = self._patch(sender, "send_mail")

    def _patch(self, target, name, **kwargs):
        p = mock.patch.object(target, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()


class TestSendDay(MailerTestCase):
    def test_sent_marks_the_day_in_mail_log(self):
        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["status"], "sent")
        self.assertEqual(out["day"], TODAY.isoformat())
        self.send_mail.assert_called_once()
        args = self.send_mail.call_args[0]
        self.assertEqual(args[0], MAIL1)
        self.assertEqual(args[1], out["subject"])
        self.record_mail.assert_called_once()
        self.assertEqual(self.record_mail.call_args[0][3], mail_log.MAIL_SENT)

    def test_already_sent_is_skipped(self):
        self.was_sent.return_value = True

        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "already_sent")
        self.send_mail.assert_not_called()
        # 跳过的时候连数据都不该去查，省一次数据库往返
        self.get_summary.assert_not_called()

    def test_no_email_is_skipped(self):
        for empty in (None, ""):
            with self.subTest(email=empty):
                out = sender.send_day(U1, TODAY, email=empty)
                self.assertEqual(out["reason"], "no_email")
        self.send_mail.assert_not_called()

    def test_no_data_is_skipped(self):
        # 机器那天没开机，或者没上传 —— 不该发一封「总计 0 秒」的空报告
        self.get_summary.return_value = None
        self.get_range.return_value = None  # 同样覆盖周日走周报查询的情况。

        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["reason"], "no_data")
        self.send_mail.assert_not_called()
        self.record_mail.assert_not_called()

    def test_day_accepts_iso_string(self):
        out = sender.send_day(U1, TODAY.isoformat(), email=MAIL1)
        self.assertEqual(out["day"], TODAY.isoformat())

    def test_dry_run_renders_but_does_not_send(self):
        out = sender.send_day(U1, TODAY, email=MAIL1, dry_run=True)

        self.assertEqual(out["reason"], "dry_run")
        self.assertTrue(out["subject"])
        self.send_mail.assert_not_called()
        # 试运行不能把「发过了」的标记写进去，否则真到点就不发了
        self.record_mail.assert_not_called()

    def test_failure_is_recorded_and_does_not_raise(self):
        # 一个地址发挂了不能把整轮发送带崩
        self.send_mail.side_effect = OSError("连接被拒绝")

        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["status"], "failed")
        self.assertIn("OSError", out["reason"])
        self.record_mail.assert_called_once()
        self.assertEqual(self.record_mail.call_args[0][3], mail_log.MAIL_FAILED)

    def test_failed_send_is_not_treated_as_sent(self):
        # 关键闭环：失败之后 was_mail_sent 仍然是 False，下次会重试。
        # 这里用一个真的（而不是 mock 的）判断逻辑来确认状态常量没写反。
        self.assertNotEqual(mail_log.MAIL_SENT, mail_log.MAIL_FAILED)
        self.send_mail.side_effect = OSError("boom")
        sender.send_day(U1, TODAY, email=MAIL1)
        recorded = self.record_mail.call_args[0]
        self.assertEqual(recorded[3], mail_log.MAIL_FAILED)

    def test_record_failure_does_not_lose_the_send(self):
        # 信已经发出去了，写记录失败 —— 别因此报错，最坏结果是重发一封
        self.record_mail.side_effect = RuntimeError("数据库挂了")

        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["status"], "sent")

    def test_record_failure_on_error_path_still_reports_failure(self):
        self.send_mail.side_effect = OSError("连接被拒绝")
        self.record_mail.side_effect = RuntimeError("数据库挂了")

        out = sender.send_day(U1, TODAY, email=MAIL1)

        self.assertEqual(out["status"], "failed")


class TestRun(MailerTestCase):
    def setUp(self):
        super().setUp()
        self.get_subscribers.return_value = [
            {"user_key": U1, "email": MAIL1},
            {"user_key": U2, "email": MAIL2},
        ]

    def test_disabled_short_circuits(self):
        with mock.patch.object(config, "MAIL_ENABLED", False):
            result = sender.run(day=TODAY)

        self.assertEqual(result["reason"], "disabled")
        self.assertFalse(result["ok"])
        # 没配 SMTP 时不该查任何订阅者
        self.get_subscribers.assert_not_called()
        self.send_mail.assert_not_called()

    def test_default_target_is_previous_complete_day(self):
        now = datetime.combine(TODAY, time(0, 3))

        result = sender.run(now=now, dry_run=True)

        self.assertEqual(result["day"], YESTERDAY.isoformat())
        self.assertEqual(result["days"], [YESTERDAY.isoformat()])

    def test_no_subscribers_is_not_an_error(self):
        self.get_subscribers.return_value = []

        result = sender.run(day=TODAY)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "no_subscribers")
        self.assertEqual(result["sent"], 0)

    def test_explicit_user_key_bypasses_subscription_state(self):
        # 手工重发时用户已经退订了，也该发得出去 —— 那是用户自己要求的
        self.get_subscribers.return_value = []

        result = sender.run(day=TODAY, user_key=U1)

        self.assertEqual(result["sent"], 1)
        self.get_subscribers.assert_not_called()

    def test_unknown_user_key(self):
        self.get_user.return_value = None

        result = sender.run(day=TODAY, user_key="nope")

        self.assertEqual(result["reason"], "no_such_user")
        self.assertFalse(result["ok"])

    def test_tallies_each_outcome(self):
        # 覆盖四种结局：成功、已发过、无数据、发送失败
        self.was_sent.side_effect = lambda uk, d, weekly=False: (uk, d) == (U1, YESTERDAY)
        self.get_summary.side_effect = (
            lambda uk, d: make_summary() if d == TODAY else None)
        self.get_range.side_effect = (
            lambda uk, start, end: make_week_summary() if end == TODAY else None)

        def flaky_send(to, subject, text, html_body):
            if to == MAIL2:
                raise OSError("连接被拒绝")
        self.send_mail.side_effect = flaky_send

        result = sender.run(day=TODAY, catchup_days=1)

        self.assertEqual(result["days"], [TODAY.isoformat(),
                                          YESTERDAY.isoformat()])
        self.assertEqual(result["sent"], 1)        # u1 今天
        self.assertEqual(result["skipped"], 2)     # u1 昨天已发 + u2 昨天无数据
        self.assertEqual(result["failed"], 1)      # u2 今天
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["results"]), 4)

    def test_catchup_covers_previous_days(self):
        result = sender.run(day=TODAY, catchup_days=3)

        self.assertEqual(result["days"], [
            TODAY.isoformat(),
            (TODAY - timedelta(days=1)).isoformat(),
            (TODAY - timedelta(days=2)).isoformat(),
            (TODAY - timedelta(days=3)).isoformat(),
        ])

    def test_never_sends_a_day_that_has_not_finished(self):
        # 就算调用方手滑传了未来日期，也不能把「还没过完的一天」当报告发出去
        result = sender.run(day=TODAY + timedelta(days=5), catchup_days=2)

        self.assertEqual(result["days"], [])
        self.send_mail.assert_not_called()
        self.assertEqual(result["sent"], 0)

    def test_dry_run_sends_nothing(self):
        result = sender.run(day=TODAY, dry_run=True)

        self.send_mail.assert_not_called()
        self.record_mail.assert_not_called()
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(result["ok"])


class TestScheduler(MailerTestCase):
    def test_next_fire_is_today_when_still_ahead(self):
        sched = scheduler.DailyMailScheduler()

        fire = sched.next_fire(datetime.combine(TODAY, time(0, 2, 59)))

        self.assertEqual(fire, datetime.combine(TODAY, time(0, 3)))

    def test_next_fire_rolls_over_after_the_moment_passed(self):
        sched = scheduler.DailyMailScheduler()

        fire = sched.next_fire(datetime.combine(TODAY, time(0, 3)))

        # 恰好到点时算「已经过了」—— 否则会 wait(0) 空转
        self.assertEqual(fire, datetime.combine(TODAY + timedelta(days=1),
                                                time(0, 3)))

    def test_latest_due_day_respects_upload_grace_period(self):
        sched = scheduler.DailyMailScheduler()

        before = sched.latest_due_day(datetime.combine(TODAY, time(0, 2, 59)))
        at_time = sched.latest_due_day(datetime.combine(TODAY, time(0, 3)))

        self.assertEqual(before, TODAY - timedelta(days=2))
        self.assertEqual(at_time, YESTERDAY)

    def test_report_day_comes_from_fire_time_not_wall_clock(self):
        """睡过头时，必须发送计划触发日的前一天。

        这是这个定时器最容易写错的地方：00:03 是新的一天，但邮件内容
        必须属于已经结束的昨天。
        """
        seen = []
        sched = scheduler.DailyMailScheduler(catchup_days=0)
        fired_at = datetime.combine(TODAY, time(0, 3))
        sched.next_fire = lambda now=None: fired_at
        sched._stop.wait = lambda timeout: False

        def fake_runner(day=None, catchup_days=0):
            seen.append(day)
            sched._stop.set()     # 跑一轮就停，否则 next_fire 恒为过去会空转
            return {"ok": True, "day": day.isoformat(), "sent": 0, "failed": 0}

        sched._runner = fake_runner
        sched._loop()

        self.assertEqual(seen, [YESTERDAY])

    def test_startup_catchup_only_covers_finished_days(self):
        """启动补发从昨天开始往前数，绝不碰今天。

        昨天的报告归今天 00:03 那一枪。启动补发只能从已经到期的报告日开始，
        不能绕过数据上传的缓冲期。
        """
        calls = []
        sched = scheduler.DailyMailScheduler(catchup_days=2)
        sched.latest_due_day = lambda now=None: YESTERDAY

        def fake_runner(day=None, catchup_days=0):
            calls.append((day, catchup_days))
            sched._stop.set()
            return {"ok": True, "day": day.isoformat(), "sent": 0, "failed": 0}

        sched._runner = fake_runner
        sched._loop()

        # 最近 2 个「已经过完的」日子 = 昨天 和 前天
        self.assertEqual(calls, [(YESTERDAY, 1)])

    def test_no_startup_catchup_when_disabled(self):
        calls = []
        sched = scheduler.DailyMailScheduler(catchup_days=0)
        sched._runner = lambda **kw: calls.append(kw) or {}
        sched._stop.set()          # 直接进 while 就退出，只验证没做补发

        sched._loop()

        self.assertEqual(calls, [])

    def test_runner_exception_does_not_kill_the_thread(self):
        # 定时器线程里逃出来的异常会静默杀掉这个线程，之后每天都不发邮件
        # 而没有任何迹象 —— 这是最难查的一类故障，所以必须被吃掉。
        sched = scheduler.DailyMailScheduler()

        def boom(day=None, catchup_days=0):
            raise RuntimeError("SMTP 服务器挂了")

        sched._runner = boom

        self.assertIsNone(sched._guarded_run(day=TODAY))

    def test_last_result_is_recorded(self):
        sched = scheduler.DailyMailScheduler()
        sched._runner = lambda day=None, catchup_days=0: {
            "ok": True, "day": TODAY.isoformat(), "sent": 3, "failed": 0}

        sched._guarded_run(day=TODAY)

        self.assertEqual(sched.last_result["sent"], 3)

    def test_start_and_stop_are_idempotent(self):
        sched = scheduler.DailyMailScheduler(catchup_days=0)
        sched._runner = lambda day=None, catchup_days=0: {
            "ok": True, "day": TODAY.isoformat(), "sent": 0, "failed": 0}

        sched.start()
        same_thread = sched._thread
        sched.start()                       # 重复 start 不该起第二个线程
        self.assertIs(sched._thread, same_thread)

        sched.stop()
        sched.stop()                        # 重复 stop 也不该炸
        self.assertIsNone(sched._thread)

    def test_start_scheduler_returns_none_without_smtp(self):
        with mock.patch.object(config, "MAIL_ENABLED", False):
            self.assertIsNone(scheduler.start_scheduler())

    def test_start_scheduler_returns_none_when_using_external_cron(self):
        with mock.patch.object(config, "MAIL_ENABLED", True), \
             mock.patch.object(config, "MAIL_SCHEDULER_ENABLED", False):
            self.assertIsNone(scheduler.start_scheduler())

    def test_start_scheduler_only_ever_builds_one(self):
        # lifespan 每次启动都会调它，重复调用不能起出第二个定时器
        with mock.patch.object(config, "MAIL_ENABLED", True), \
             mock.patch.object(config, "MAIL_SCHEDULER_ENABLED", True), \
             mock.patch.object(scheduler, "DailyMailScheduler") as ctor:
            fake = ctor.return_value
            old = scheduler._scheduler
            scheduler._scheduler = None
            try:
                first = scheduler.start_scheduler()
                second = scheduler.start_scheduler()
            finally:
                scheduler._scheduler = old

        self.assertIs(first, fake)
        self.assertIs(second, fake)
        self.assertEqual(ctor.call_count, 1)
        self.assertEqual(fake.start.call_count, 1)


class TestWeekRange(unittest.TestCase):
    """week_range 只做算术，不看 day 是星期几 —— 它是纯函数，不必起脚手架。"""

    def test_sunday_gives_monday_through_sunday(self):
        self.assertEqual(report.week_range(SUNDAY), (MONDAY, SUNDAY))

    def test_the_window_ends_on_the_given_day(self):
        # 给定周三 → 上周四到本周三。函数本身不要求输入是周日
        self.assertEqual(report.week_range(date(2026, 10, 14)),
                         (date(2026, 10, 8), date(2026, 10, 14)))

    def test_window_is_always_seven_days(self):
        day = date(2026, 1, 1)
        for _ in range(400):
            start, end = report.week_range(day)
            self.assertEqual(end, day)
            self.assertEqual((end - start).days, 6)
            day += timedelta(days=1)

    def test_crosses_a_month_boundary(self):
        # 2026-11-02 是周一，它那一周从 10 月跨到 11 月
        self.assertEqual(report.week_range(date(2026, 11, 2)),
                         (date(2026, 10, 27), date(2026, 11, 2)))

    def test_crosses_a_year_boundary(self):
        # 2027-01-03 是周日，它那一周从 2026 年跨到 2027 年
        self.assertEqual(report.week_range(date(2027, 1, 3)),
                         (date(2026, 12, 28), date(2027, 1, 3)))


class TestBuildWeeklyMessage(unittest.TestCase):
    def test_subject_carries_the_range_and_the_total(self):
        subject = report.build_weekly_subject(SUNDAY, make_week_summary())

        self.assertTrue(subject.startswith("【TimeTrace】"))
        self.assertIn("2026-10-12 ~ 2026-10-18", subject)
        self.assertIn("7 小时 30 分", subject)
        self.assertIn("周报", subject)
        # 收件箱里要能一眼和日报区分开
        self.assertNotIn("使用报告", subject)

    def test_text_has_period_device_and_app_ranking(self):
        text = report.build_weekly_text(SUNDAY, make_week_summary(),
                                        {"hostname": "DESKTOP-ABC"})

        self.assertIn("TimeTrace 每周使用报告", text)
        self.assertIn("周期：2026-10-12 ~ 2026-10-18（7 天，有数据 5 天）", text)
        self.assertIn("设备：DESKTOP-ABC", text)
        self.assertIn("总计：7 小时 30 分，共 3 个应用", text)
        self.assertIn("PyCharm", text)
        self.assertIn("5 小时", text)              # 18000 秒
        self.assertIn("66.7%", text)               # 18000 / 27000
        self.assertIn("启动 40 次", text)
        self.assertIn("wechat.exe", text)          # 显示名为空时退回进程名
        self.assertIn(report.FOOTER_TEXT, text)

    def test_text_without_hostname_omits_the_device_line(self):
        for user in (None, {}, {"hostname": ""}):
            with self.subTest(user=user):
                text = report.build_weekly_text(SUNDAY, make_week_summary(), user)
                self.assertNotIn("设备：", text)

    def test_period_line_drops_the_day_count_when_it_is_unknown(self):
        # 手工拼的 summary 可能没有 days_with_data：退成只报天数，不能印出 None
        summary = make_week_summary()
        summary.pop("days_with_data")

        text = report.build_weekly_text(SUNDAY, summary)

        self.assertIn("周期：2026-10-12 ~ 2026-10-18（7 天）", text)
        self.assertNotIn("有数据", text)
        self.assertNotIn("None", text)

    def test_html_has_the_weekly_heading_and_the_period(self):
        body = report.build_weekly_html(SUNDAY, make_week_summary(),
                                        {"hostname": "DESKTOP-ABC"})

        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("2026-10-12 ~ 2026-10-18 周报", body)
        self.assertIn("有数据 5/7 天", body)
        self.assertIn("DESKTOP-ABC", body)
        self.assertIn("PyCharm", body)
        # 页脚在 HTML 里是转义过的（原文含 <设备ID>）。
        self.assertIn("TimeTrace 自动报告", body)

    def test_long_weekly_ranking_is_folded(self):
        with mock.patch.object(config, "MAIL_MAX_APPS", 2):
            text = report.build_weekly_text(SUNDAY, make_week_summary())
            body = report.build_weekly_html(SUNDAY, make_week_summary())

        for rendered in (text, body):
            self.assertIn("PyCharm", rendered)
            self.assertNotIn("wechat.exe", rendered)
            self.assertIn("20 分", rendered)
            self.assertIn("3 个应用", rendered)
        self.assertIn("另有 1 个应用", text)
        self.assertIn("其他（1 应用）", body)

    def test_html_escapes_app_names_and_hostname(self):
        summary = make_week_summary()
        summary["apps"] = [{"app_name": "x.exe",
                            "display_name": '<script>alert("x")</script>',
                            "seconds": 60.0, "launches": 1}]

        body = report.build_weekly_html(SUNDAY, summary, {"hostname": "<b>x</b>"})

        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<b>x</b>", body)

    def test_zero_total_does_not_divide_by_zero(self):
        # 整周只是启动过、没累计出时长：占比算成 0.0，不能炸
        summary = make_week_summary(total_seconds=0)
        for app in summary["apps"]:
            app["seconds"] = 0.0

        text = report.build_weekly_text(SUNDAY, summary)
        body = report.build_weekly_html(SUNDAY, summary)

        self.assertIn("0.0%", text)
        # HTML is now a stacked chart, not a percentage/ranking table.
        self.assertIn("总计 <strong>0 秒</strong>", body)
        self.assertIn("data:image/png;base64,", body)
        self.assertIn("0 秒", text)

    def test_message_returns_subject_text_and_html(self):
        subject, text, body = report.build_weekly_message(SUNDAY,
                                                          make_week_summary())

        self.assertIn("周报", subject)
        self.assertIn("每周使用报告", text)
        self.assertIn("<!DOCTYPE html>", body)


class TestIsWeeklyDay(unittest.TestCase):
    """「哪一天发周报」的真值表。日期是写死的，不跟 date.today() 走。"""

    def _week(self):
        # MONDAY 起连着 7 天，正好一周
        return [MONDAY + timedelta(days=i) for i in range(7)]

    def test_default_is_sunday(self):
        with mock.patch.object(config, "MAIL_WEEKLY_DAY", 6):
            flags = [sender.is_weekly_day(d) for d in self._week()]

        self.assertEqual(flags, [False] * 6 + [True])

    def test_can_be_moved_to_monday(self):
        with mock.patch.object(config, "MAIL_WEEKLY_DAY", 0):
            flags = [sender.is_weekly_day(d) for d in self._week()]

        self.assertEqual(flags, [True] + [False] * 6)

    def test_exactly_one_day_of_each_week_is_weekly(self):
        """一周里恰好一天是周报日 —— 这正是 mail_log 仍共用同一个键的依据。

        报告种类既然是「这一天是星期几」的纯函数，同一天就只可能长出一封
        报告，(user_key, day) 上永远不会有两个种类要记。
        """
        for weekday in range(7):
            with self.subTest(weekday=weekday):
                with mock.patch.object(config, "MAIL_WEEKLY_DAY", weekday):
                    hits = [d for d in self._week() if sender.is_weekly_day(d)]
                self.assertEqual(len(hits), 1)


class TestReportKind(MailerTestCase):
    """send_day 按星期分岔：周报日走区间查询 + 周报渲染，其余走日报。

    日期用写死的 MONDAY / SUNDAY，并把 config.MAIL_WEEKLY_DAY 也固定住，
    免得 server/.env 改了默认值就让这套测试时红时绿。
    """

    def _weekly_on(self, weekday):
        p = mock.patch.object(config, "MAIL_WEEKLY_DAY", weekday)
        self.addCleanup(p.stop)
        p.start()

    def test_weekly_day_queries_the_range_and_renders_the_weekly(self):
        self._weekly_on(6)

        out = sender.send_day(U1, SUNDAY, email=MAIL1)

        self.get_range.assert_called_once_with(U1, MONDAY, SUNDAY)
        self.get_summary.assert_not_called()
        self.assertTrue(out["weekly"])
        self.assertEqual(out["status"], "sent")
        self.assertIn("周报", out["subject"])
        self.assertIn("2026-10-12 ~ 2026-10-18", out["subject"])

    def test_other_days_still_use_the_daily(self):
        self._weekly_on(6)

        out = sender.send_day(U1, MONDAY, email=MAIL1)

        self.get_summary.assert_called_once_with(U1, MONDAY)
        self.get_range.assert_not_called()
        self.assertFalse(out["weekly"])
        self.assertIn("使用报告", out["subject"])

    def test_moving_the_weekly_day_moves_the_behaviour(self):
        # 把周报挪到周一：周一那封变成周报，周日退回日报
        self._weekly_on(0)

        monday = sender.send_day(U1, MONDAY, email=MAIL1)
        sunday = sender.send_day(U1, SUNDAY, email=MAIL1)

        self.assertTrue(monday["weekly"])
        self.assertFalse(sunday["weekly"])

    def test_weekly_day_with_no_data_all_week_is_skipped(self):
        # 整周一行记录都没有，不能发一封「总计 0 秒」的空周报
        self._weekly_on(6)
        self.get_range.return_value = None

        out = sender.send_day(U1, SUNDAY, email=MAIL1)

        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "no_data")
        self.send_mail.assert_not_called()
        # 没发就不该留痕，否则补发会被 was_mail_sent 挡掉、永远补不上
        self.record_mail.assert_not_called()

    def test_already_sent_weekly_does_not_query(self):
        self._weekly_on(6)
        self.was_sent.return_value = True

        out = sender.send_day(U1, SUNDAY, email=MAIL1)

        self.assertEqual(out["reason"], "already_sent")
        self.get_range.assert_not_called()

    def test_weekly_dry_run_renders_but_does_not_send(self):
        self._weekly_on(6)

        out = sender.send_day(U1, SUNDAY, email=MAIL1, dry_run=True)

        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "dry_run")
        self.assertIn("周报", out["subject"])
        self.send_mail.assert_not_called()
        self.record_mail.assert_not_called()

    def test_weekly_failure_is_recorded_without_marking_it_sent(self):
        self._weekly_on(6)
        self.send_mail.side_effect = OSError("连接被拒绝")

        out = sender.send_day(U1, SUNDAY, email=MAIL1)

        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.record_mail.call_args[0][3], mail_log.MAIL_FAILED)
        # 失败留痕，但 was_mail_sent 只认 sent —— 下一轮还会重试
        self.assertEqual(self.was_sent.call_count, 1)

    def test_daily_and_weekly_use_different_mail_log_keys(self):
        """日报和周报在 mail_log 上用不同的键 (user_key, day, weekly)。

        所以同一天可以各发一封日报和周报：周报记的是周日那天（而不是区间
        起点周一），weekly=True；日报 weekly=False。见 server/mail/__init__.py。
        """
        self._weekly_on(6)

        sender.send_day(U1, SUNDAY, email=MAIL1)

        self.was_sent.assert_called_once_with(U1, SUNDAY, weekly=True)
        self.assertEqual(self.record_mail.call_args[0][1], SUNDAY)
        self.assertEqual(self.record_mail.call_args[0][3], mail_log.MAIL_SENT)
        self.assertTrue(self.record_mail.call_args[1]["weekly"])

    def test_weekly_override_sends_daily_on_sunday(self):
        """显式传 weekly=False 时，周日也能发日报（模板与星期几解耦）。"""
        self._weekly_on(6)

        out = sender.send_day(U1, SUNDAY, email=MAIL1, weekly=False)

        self.assertFalse(out["weekly"])
        self.was_sent.assert_called_once_with(U1, SUNDAY, weekly=False)
        self.assertFalse(self.record_mail.call_args[1]["weekly"])

    def test_run_does_not_distinguish_kinds(self):
        """run() 只是按天循环，周日那天自然变周报 —— 外部 cron 因此不用改。"""
        self._weekly_on(6)
        # SUNDAY 比今天晚，run 里 d <= now.date() 的过滤会把它剔掉，
        # 所以把 now 挪到它之后
        now = datetime(2026, 10, 19, 9, 0)

        result = sender.run(day=SUNDAY, user_key=U1, now=now)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["results"][0]["weekly"])
        self.get_range.assert_called_once_with(U1, MONDAY, SUNDAY)


if __name__ == "__main__":
    unittest.main()
