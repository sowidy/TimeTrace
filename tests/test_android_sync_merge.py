"""Android sync merge: unlocks storage + per-mailbox merged mail.

Server-side only, mock DB, never a real database or real mail. Follows the
same patching discipline as tests/test_mailer.py and
tests/test_tracking_routes.py: patch the name where the code under test
actually looks it up.
"""
import unittest
from datetime import date, timedelta
from unittest import mock

from api import tracking as tracking_api
from api.tracking_schemas import TimelineDay, TimelineUpload
from db import mail_log, tracking, usage_records, users
from mail import merge, report, sender

U_PC = "11111111-1111-4111-8111-111111111111"
U_PHONE = "22222222-2222-4222-8222-222222222222"
U_PHONE2 = "33333333-3333-4333-8333-333333333333"
MAIL = "User@Example.com "
DAY = date(2026, 11, 6)  # Friday: daily mail, not the weekly day
SUNDAY = date(2026, 10, 18)


def device(key, email=MAIL, hostname="PC", created="2026-01-01 00:00:00",
           platform="Windows"):
    return {"user_key": key, "email": email, "hostname": hostname,
            "platform": platform, "created_at": created}


def summary(total, apps):
    return {"day": DAY.isoformat(), "total_seconds": total,
            "app_count": len(apps), "apps": apps}


def app_row(name, seconds, launches=1, display=""):
    return {"app_name": name, "display_name": display or name,
            "seconds": seconds, "launches": launches}


class TestNormalizeAndGroup(unittest.TestCase):
    def test_email_key_is_lowercase_and_stripped(self):
        self.assertEqual(merge.normalize_email("  User@Example.COM "), "user@example.com")
        self.assertEqual(merge.normalize_email(None), "")
        self.assertEqual(merge.normalize_email(""), "")

    def test_same_mailbox_different_case_and_space_is_one_group(self):
        rows = [device(U_PC, "User@Example.com", "PC", "2026-01-02 00:00:00"),
                device(U_PHONE, " user@example.com ", "Pixel", "2026-01-01 00:00:00")]
        groups = merge.group_devices_by_email(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["email"], "user@example.com")
        # Anchor is the earliest-created device, not the first row.
        self.assertEqual(groups[0]["devices"][0]["user_key"], U_PHONE)
        self.assertEqual(groups[0]["devices"][1]["user_key"], U_PC)

    def test_distinct_mailboxes_stay_separate_and_no_email_rows_drop(self):
        rows = [device(U_PC, "a@example.com", "PC"),
                device(U_PHONE, "b@example.com", "Pixel"),
                device("33333333-3333-4333-8333-333333333333", "", "NoMail"),
                device("44444444-4444-4334-8444-444444444444", None, "NoMail2")]
        groups = merge.group_devices_by_email(rows)
        self.assertEqual([g["email"] for g in groups], ["a@example.com", "b@example.com"])

    def test_single_device_group_keeps_send_day_path(self):
        groups = merge.group_devices_by_email([device(U_PC)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["devices"]), 1)


class TestMergeSummaries(unittest.TestCase):
    def test_totals_ranking_and_launches_add_up(self):
        pc = summary(3600, [app_row("wechat", 2000, 5), app_row("chrome.exe", 1600, 3)])
        phone = summary(1800, [app_row("wechat", 1200, 9), app_row("douyin", 600, 2)])
        merged = merge.merge_app_summaries([("PC", pc), ("Pixel", phone)])
        self.assertEqual(merged["total_seconds"], 5400)
        self.assertEqual(merged["app_count"], 3)
        self.assertEqual(merged["apps"][0]["app_name"], "wechat")
        self.assertEqual(merged["apps"][0]["seconds"], 3200)
        self.assertEqual(merged["apps"][0]["launches"], 14)
        self.assertEqual([d["total_seconds"] for d in merged["_devices"]], [3600, 1800])

    def test_all_none_is_none_and_partial_none_keeps_zero_device(self):
        self.assertIsNone(merge.merge_app_summaries([("PC", None), ("Pixel", None)]))
        merged = merge.merge_app_summaries([("PC", None), ("Pixel", summary(60, [app_row("a", 60)]))])
        self.assertEqual(merged["total_seconds"], 60)
        self.assertEqual(merged["_devices"][0]["total_seconds"], 0)

    def test_display_name_keeps_first_non_empty(self):
        pc = summary(10, [{"app_name": "wechat", "display_name": "", "seconds": 10, "launches": 1}])
        phone = summary(10, [{"app_name": "wechat", "display_name": "微信", "seconds": 10, "launches": 1}])
        merged = merge.merge_app_summaries([("PC", pc), ("Pixel", phone)])
        self.assertEqual(merged["apps"][0]["display_name"], "微信")


class TestOverlap(unittest.TestCase):
    def test_disjoint_is_zero_and_touching_is_zero(self):
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(100, 200)]]), 0)
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(200, 300)]]), 0)

    def test_partial_and_nested_overlap(self):
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(50, 150)]]), 50)
        self.assertEqual(merge.compute_overlap_seconds([[(0, 1000)], [(100, 200)]]), 100)

    def test_three_devices_count_once_per_second(self):
        # 0-50 covered by 2, 50-100 by 3, 100-150 by 2, 150-200 by 1:
        # overlap is 150, not the sum of pairwise overlaps.
        intervals = [[(0, 100)], [(0, 200)], [(50, 150)]]
        self.assertEqual(merge.compute_overlap_seconds(intervals), 150)

    def test_invalid_spans_ignored(self):
        self.assertEqual(merge.compute_overlap_seconds([[(100, 100)], [(0, 10)]]), 0)


class TestUnionIntervalsAndBins(unittest.TestCase):
    def test_same_app_overlapping_spans_collapse(self):
        rows = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 600},
                {"day": str(DAY), "app_name": "wechat", "start_second": 300, "end_second": 900},
                {"day": str(DAY), "app_name": "douyin", "start_second": 0, "end_second": 300}]
        union = merge.union_intervals_by_app(rows)
        self.assertEqual(len(union), 2)
        wechat = [r for r in union if r["app_name"] == "wechat"][0]
        self.assertEqual((wechat["start_second"], wechat["end_second"]), (0, 900))

    def test_union_bins_split_at_15_minute_boundaries(self):
        union = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 1800}]
        bins = merge.union_bins_from_intervals(union)
        self.assertEqual([(b["bucket"], b["seconds"]) for b in bins], [(0, 900), (1, 900)])
        self.assertEqual(sum(b["seconds"] for b in bins), 1800)

    def test_union_total_equals_phone_plus_pc_minus_same_app_overlap(self):
        rows = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 600},
                {"day": str(DAY), "app_name": "wechat", "start_second": 300, "end_second": 900}]
        bins = merge.union_bins_from_intervals(merge.union_intervals_by_app(rows))
        self.assertEqual(sum(b["seconds"] for b in bins), 900)  # 600+600-300

    def test_daily_apps_from_bins(self):
        bins = [{"day": str(DAY), "bucket": 0, "app_name": "wechat", "seconds": 900},
                {"day": str(DAY), "bucket": 1, "app_name": "wechat", "seconds": 900}]
        daily = merge.daily_apps_from_bins(bins)
        self.assertEqual(daily, [{"day": str(DAY), "app_name": "wechat", "seconds": 1800}])

    def test_per_app_overlap_seconds(self):
        rows = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 600},
                {"day": str(DAY), "app_name": "wechat", "start_second": 300, "end_second": 900},
                {"day": str(DAY), "app_name": "douyin", "start_second": 0, "end_second": 300},
                {"day": str(DAY), "app_name": "douyin", "start_second": 600, "end_second": 900}]
        # wechat: 600+600-900 = 300s overlap; douyin spans are disjoint.
        self.assertEqual(merge.per_app_overlap_seconds(rows), {"wechat": 300.0})

    def test_device_kind_and_sources_by_key(self):
        self.assertEqual(merge.device_kind("Android 14"), "P")
        self.assertEqual(merge.device_kind("android"), "P")
        self.assertEqual(merge.device_kind("Windows 11"), "C")
        self.assertEqual(merge.device_kind(""), "C")
        self.assertEqual(merge.sources_by_key([
            ("C", ["chrome.exe", "weixin.exe"]),
            ("P", ["weixin.exe", "douyin.exe"]),
        ]), {"chrome.exe": "C", "weixin.exe": "M", "douyin.exe": "P"})


class TestTimelineUnlocksSchema(unittest.TestCase):
    def test_old_payload_without_unlocks_still_parses_as_none(self):
        day = TimelineDay.model_validate(
            {"day": "2026-11-06",
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})
        self.assertIsNone(day.unlocks)

    def test_unlocks_accepted(self):
        day = TimelineDay.model_validate(
            {"day": "2026-11-06", "unlocks": 42,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})
        self.assertEqual(day.unlocks, 42)

    def test_unlocks_rejects_negative_and_non_integer(self):
        for bad in (-1, 1.5, "42", True):
            with self.assertRaises(Exception, msg=repr(bad)):
                TimelineDay.model_validate(
                    {"day": "2026-11-06", "unlocks": bad,
                     "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})

    def test_extra_fields_still_forbidden(self):
        with self.assertRaises(Exception):
            TimelineUpload.model_validate(
                {"user_key": U_PC, "days": [
                    {"day": "2026-11-06", "unlock": 42,
                     "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]})


class TestUnlocksStorage(unittest.TestCase):
    def test_save_unlocks_upserts_instead_of_accumulating(self):
        cur = mock.MagicMock()
        tracking.save_unlocks(cur, U_PHONE, DAY, 42)
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("android_daily_stats", sql)
        self.assertEqual(params, (U_PHONE, DAY, 42, mock.ANY))

    def test_read_unlocks_filters_by_user_and_range(self):
        fake_conn = mock.MagicMock()
        fake_cur = fake_conn.cursor.return_value.__enter__.return_value
        fake_cur.fetchall.return_value = [
            {"day": DAY, "unlocks": 42, "uploaded_at": "2026-11-06 00:00:00"}]
        with mock.patch.object(tracking.db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value = fake_conn
            rows = tracking.read_unlocks(U_PHONE, DAY, DAY)
        self.assertEqual(rows[0]["unlocks"], 42)
        self.assertEqual(rows[0]["day"], "2026-11-06")
        sql = fake_cur.execute.call_args[0][0]
        self.assertIn("android_daily_stats", sql)


class TestTimelineUploadWritesUnlocks(unittest.TestCase):
    def _upload(self, unlocks):
        req = TimelineUpload.model_validate(
            {"user_key": U_PHONE, "hostname": "Pixel", "platform": "Android 14",
             "client_version": "1.8.0", "bucket_minutes": 15,
             "days": [{"day": "2026-11-06",
                       **({"unlocks": unlocks} if unlocks is not None else {}),
                       "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]})
        cur = mock.MagicMock()
        store = mock.MagicMock()
        with mock.patch.object(tracking_api, "authenticate"), \
                mock.patch.object(tracking_api.users, "upsert_user"), \
                mock.patch.object(tracking_api, "store", store), \
                mock.patch.object(tracking_api.db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
            result = tracking_api.upload_dataset("timeline", req, "Bearer " + "t" * 40)
        return result, store, cur

    def test_upload_with_unlocks_calls_save_unlocks(self):
        result, store, _ = self._upload(42)
        self.assertTrue(result["ok"])
        store.save_unlocks.assert_called_once()
        args = store.save_unlocks.call_args[0]
        self.assertEqual((args[1], args[2], args[3]), (U_PHONE, date(2026, 11, 6), 42))

    def test_upload_without_unlocks_does_not_touch_the_table(self):
        result, store, _ = self._upload(None)
        self.assertTrue(result["ok"])
        store.save_unlocks.assert_not_called()

    def test_reupload_overwrites_unlocks_instead_of_adding(self):
        # Same day uploaded twice: the second value replaces the first.
        _, store1, _ = self._upload(42)
        _, store2, _ = self._upload(7)
        self.assertEqual(store1.save_unlocks.call_args[0][3], 42)
        self.assertEqual(store2.save_unlocks.call_args[0][3], 7)


class TestValidationIs4xx(unittest.TestCase):
    """Validation failures are 4xx, so a client that retries only on
    network/429/5xx (see client/net/tracking_upload.py) never retries them."""

    def setUp(self):
        import app as app_module
        from tests.test_tracking_routes import ASGIClient
        self.client = ASGIClient()
        self.app = app_module
        self.meta = {"user_key": U_PHONE, "hostname": "Pixel",
                     "platform": "Android 14", "client_version": "1.8.0"}

    def test_unknown_field_is_422_without_touching_db(self):
        from db import conn as db_conn
        payload = {**self.meta, "days": [
            {"day": "2026-11-06", "unlock": 1,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]}
        with mock.patch.object(db_conn, "connect") as connect:
            response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 422)
        connect.assert_not_called()

    def test_bad_bucket_is_422(self):
        payload = {**self.meta, "days": [
            {"day": "2026-11-06",
             "bins": [{"bucket": 96, "app_name": "wechat", "seconds": 60}]}]}
        response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_valid_unlocks_payload_is_accepted(self):
        cur = mock.MagicMock()
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cur
        payload = {**self.meta, "days": [
            {"day": "2026-11-06", "unlocks": 42,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]}
        from db import conn as db_conn
        with mock.patch.object(tracking_api, "authenticate"), \
                mock.patch.object(db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value = connection
            response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


class MergedMailTestCase(unittest.TestCase):
    def setUp(self):
        self._patchers = []
        self.send_mail = self._patch(sender, "send_mail")
        self.was_sent = self._patch(mail_log, "was_mail_sent", return_value=False)
        self.record_mail = self._patch(mail_log, "record_mail")
        self.get_user = self._patch(
            users, "get_user",
            return_value={"user_key": U_PC, "email": "user@example.com",
                          "hostname": "PC", "subscribed": 1})
        self.get_summary = self._patch(usage_records, "get_daily_summary")
        self.get_range = self._patch(usage_records, "get_range_summary")
        self.read_range = self._patch(tracking, "read_range")
        self.readiness = self._patch(tracking, "readiness", return_value=[])
        self._patch_config = mock.patch.object(sender.config, "MAIL_TRACKING_ENABLED", True)
        self._patch_config.start()
        self.addCleanup(self._patch_config.stop)

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _summaries(self, pc_total=3600, phone_total=1800):
        def daily(key, day):
            if key == U_PC:
                return summary(pc_total, [app_row("wechat", 2000, 5),
                                          app_row("chrome.exe", 1600, 3)])
            return summary(phone_total, [app_row("wechat", 1200, 9),
                                         app_row("douyin", 600, 2)])
        self.get_summary.side_effect = daily

    def _intervals(self, pc_spans, phone_spans):
        """app_intervals per device; timeline bins derived from the same spans."""
        def read_range(kind, key, start, end):
            if kind == "app_intervals":
                spans = pc_spans if key == U_PC else phone_spans
                return [{"day": str(DAY), "app_name": "wechat",
                         "start_second": s, "end_second": e} for s, e in spans]
            if kind == "timeline":
                spans = pc_spans if key == U_PC else phone_spans
                return [{"day": str(DAY), "bucket": int(s // 900), "app_name": "wechat",
                         "seconds": e - s} for s, e in spans]
            return []
        self.read_range.side_effect = read_range


class TestSendMergedDay(MergedMailTestCase):
    def test_one_mail_for_two_devices_with_overlap_line(self):
        self._summaries()
        # PC 0-3600, phone 1800-3600: 1800s overlap; merged = 3600+1800-1800.
        self._intervals([(0, 3600)], [(1800, 3600)])
        devices = [device(U_PHONE, hostname="Pixel 8", platform="Android 14", created="2026-01-01 00:00:00"),
                   device(U_PC, created="2026-01-02 00:00:00")]
        outcome = sender.send_merged_day(devices, DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(outcome["merged_devices"], 2)
        self.send_mail.assert_called_once()
        to, subject, text, html_body = self.send_mail.call_args[0]
        self.assertEqual(to, "user@example.com")
        self.assertIn("1 小时", subject)  # merged 3600s, not 5400s
        self.assertIn("分设备", text)
        self.assertIn("双设备重叠：30 分", text)
        self.assertIn("双设备重叠", html_body)
        # mail_log keyed by the earliest-created device, detail names the merge.
        self.record_mail.assert_called_once()
        args = self.record_mail.call_args[0]
        self.assertEqual(args[0], U_PHONE)
        self.assertIn("合并 2 台设备", args[4])

    def test_no_overlap_means_plain_sum(self):
        self._summaries()
        self._intervals([(0, 1800)], [(3600, 5400)])
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE, hostname="Pixel 8", platform="Android 14")], DAY, MAIL)
        _, _, text, _ = self.send_mail.call_args[0]
        self.assertEqual(outcome["status"], "sent")
        self.assertNotIn("双设备重叠", text)

    def test_dry_run_renders_but_sends_nothing(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE, hostname="Pixel 8", platform="Android 14")], DAY, MAIL, dry_run=True)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "dry_run")
        self.send_mail.assert_not_called()
        self.record_mail.assert_not_called()

    def test_already_sent_anchor_skips_the_whole_mailbox(self):
        self._summaries()
        self.was_sent.return_value = True
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE, hostname="Pixel 8", platform="Android 14")], DAY, MAIL)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "already_sent")
        self.send_mail.assert_not_called()

    def test_no_data_on_any_device_skips(self):
        self.get_summary.return_value = None
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE, hostname="Pixel 8", platform="Android 14")], DAY, MAIL)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "no_data")
        self.send_mail.assert_not_called()

    def test_merged_daily_mail_keeps_gantt_chart(self):
        # Regression: the daily Gantt used to refuse to draw for merged mail
        # because union spans are shorter than the summed per-app totals
        # whenever one app ran on both devices at once. The chart must draw
        # union spans and say so, on top of the merged breakdown.
        self.get_summary.side_effect = lambda key, day: summary(
            3600 if key == U_PC else 1800,
            [app_row("wechat", 2000 if key == U_PC else 1200, 5),
             app_row("douyin", 1600 if key == U_PC else 600, 2)])

        def read_range(kind, key, start, end):
            spans = {"wechat": (0, 3600) if key == U_PC else (1800, 3600),
                     "douyin": (3600, 5200) if key == U_PC else (5400, 6000)}
            if kind == "app_intervals":
                return [{"day": str(DAY), "app_name": app,
                         "start_second": s, "end_second": e}
                        for app, (s, e) in spans.items()]
            if kind == "timeline":
                return [{"day": str(DAY), "bucket": s // 900, "app_name": app,
                         "seconds": e - s} for app, (s, e) in spans.items()]
            return []
        self.read_range.side_effect = read_range
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE, hostname="Pixel 8", platform="Android 14")], DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        _, _, text, html_body = self.send_mail.call_args[0]
        self.assertIn("双设备重叠", text)
        self.assertIn("软件使用时段和时长", html_body)
        self.assertIn("data:image/png", html_body)
        self.assertNotIn("尚未收到完整的软件起止时段", html_body)
        self.assertIn("合并显示一次", html_body)

    def test_merged_gantt_labels_show_chinese_and_markers(self):
        # Gantt rows and legend use Chinese display names with a device
        # marker: P phone, C computer, M both. WeChat ran on both devices,
        # Chrome only on PC, Douyin only on the phone.
        self.get_summary.side_effect = lambda key, day: summary(
            3600 if key == U_PC else 1800,
            [app_row("weixin.exe", 2000 if key == U_PC else 1200, 5, "微信"),
             app_row("chrome.exe" if key == U_PC else "douyin.exe",
                     1600 if key == U_PC else 600, 2,
                     "Chrome 浏览器" if key == U_PC else "抖音")])

        def read_range(kind, key, start, end):
            spans = {"weixin.exe": (0, 2000) if key == U_PC else (1800, 3000),
                     "chrome.exe": (2000, 3600),
                     "douyin.exe": (3600, 4200)}
            if kind == "app_intervals":
                apps = ["weixin.exe", "chrome.exe"] if key == U_PC else ["weixin.exe", "douyin.exe"]
                return [{"day": str(DAY), "app_name": app,
                         "start_second": spans[app][0], "end_second": spans[app][1]}
                        for app in apps]
            if kind == "timeline":
                return []
            return []
        self.read_range.side_effect = read_range
        outcome = sender.send_merged_day(
            [device(U_PC),
             device(U_PHONE, hostname="Pixel 8", platform="Android 14")],
            DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        _, _, _, html_body = self.send_mail.call_args[0]
        self.assertIn("data:image/png", html_body)
        self.assertIn("微信 (M)", html_body)
        self.assertIn("Chrome 浏览器 (C)", html_body)
        self.assertIn("抖音 (P)", html_body)
        self.assertIn("P 手机，C 电脑，M 两台都有", html_body)

    def test_merged_mail_opens_with_winner_timeline(self):
        self.get_summary.side_effect = lambda key, day: summary(
            8400 if key == U_PC else 9000,
            [app_row("chrome.exe", 4800, 5, "Chrome 浏览器"),
             app_row("weixin.exe", 3600 if key == U_PC else 1800, 5, "微信")]
            if key == U_PC else
            [app_row("weixin.exe", 1800, 5, "微信"),
             app_row("douyin.exe", 7200, 5, "抖音")])

        def read_range(kind, key, start, end):
            pc_apps = [("chrome.exe", 32400, 34200), ("chrome.exe", 36000, 39000),
                       ("weixin.exe", 32400, 36000)]
            phone_apps = [("weixin.exe", 36000, 37800), ("douyin.exe", 75600, 82800)]
            if kind == "app_intervals":
                apps = pc_apps if key == U_PC else phone_apps
                return [{"day": str(DAY), "app_name": app,
                         "start_second": s, "end_second": e} for app, s, e in apps]
            if kind == "web_intervals" and key == U_PC:
                return [{"day": str(DAY), "host": "douyin.com",
                         "start_second": 36000, "end_second": 38400},
                        {"day": str(DAY), "host": "chatgpt.com",
                         "start_second": 38400, "end_second": 39000}]
            if kind == "web" and key == U_PC:
                return [{"day": str(DAY), "host": "douyin.com", "seconds": 2400},
                        {"day": str(DAY), "host": "chatgpt.com", "seconds": 600}]
            if kind == "timeline":
                return []
            return []
        self.read_range.side_effect = read_range
        outcome = sender.send_merged_day(
            [device(U_PC),
             device(U_PHONE, hostname="Pixel 8", platform="Android 14")],
            DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        _, _, _, html_body = self.send_mail.call_args[0]
        self.assertIn("今日时间线", html_body)
        self.assertLess(html_body.index("今日时间线"),
                        html_body.index("软件使用时段和时长"))
        self.assertIn("抖音 (P)", html_body)
        self.assertIn("抖音 (C)", html_body)

    def test_gantt_still_refuses_incomplete_intervals(self):
        # No overlap allowance: a missing app's spans still refuse the chart.
        from mail.charts import application_gantt_chart
        s = summary(3600, [app_row("wechat", 3600), app_row("douyin", 600)])
        intervals = [{"day": str(DAY), "app_name": "wechat",
                      "start_second": 0, "end_second": 1800}]
        html_body = application_gantt_chart(s, intervals, DAY)
        self.assertNotIn("data:image/png", html_body)
        self.assertIn("尚未收到完整的软件起止时段", html_body)

    def test_same_device_registered_twice_shows_once(self):
        # Regression: reinstalling the app minted a fresh random user_key,
        # so the same phone appeared twice with identical totals. The mail
        # must list it once, and the headline total must not move: the
        # duplicate span is collapsed inside the label, not subtracted
        # again as cross-device overlap.
        def daily(key, day):
            if key == U_PC:
                return summary(3600, [app_row("wechat", 2000, 5),
                                      app_row("chrome.exe", 1600, 3)])
            return summary(1800, [app_row("wechat", 1200, 9),
                                  app_row("douyin", 600, 2)])
        self.get_summary.side_effect = daily

        def read_range(kind, key, start, end):
            # Spans are disjoint per device (one foreground app at a time),
            # matching the summaries above.
            spans = {"wechat": (0, 2000) if key == U_PC else (1800, 3000),
                     "douyin": (3600, 4200),
                     "chrome.exe": (2000, 3600)}
            if kind == "app_intervals":
                apps = ["wechat", "chrome.exe"] if key == U_PC else ["wechat", "douyin"]
                return [{"day": str(DAY), "app_name": app,
                         "start_second": spans[app][0], "end_second": spans[app][1]}
                        for app in apps]
            if kind == "timeline":
                return []
            return []
        self.read_range.side_effect = read_range
        devices = [device(U_PC, created="2026-01-01 00:00:00"),
                   device(U_PHONE, hostname="Pixel 8", platform="Android 14",
                          created="2026-01-02 00:00:00"),
                   device(U_PHONE2, hostname="Pixel 8", platform="Android 14",
                          created="2026-01-03 00:00:00")]
        outcome = sender.send_merged_day(devices, DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        _, subject, text, _ = self.send_mail.call_args[0]
        # PC 3600 + phone 1800 - PC/phone wechat overlap 1200 = 4200s.
        self.assertIn("1 小时 10 分", subject)
        self.assertEqual(text.count("Pixel 8"), 1)
        self.assertIn("双设备重叠：20 分", text)

    def test_device_with_no_data_drops_from_breakdown(self):
        self.get_summary.side_effect = lambda key, day: (
            summary(3600, [app_row("wechat", 3600)]) if key == U_PC else None)
        self._intervals([(0, 3600)], [])
        outcome = sender.send_merged_day(
            [device(U_PC),
             device(U_PHONE, hostname="Pixel 8", platform="Android 14")],
            DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        _, _, text, _ = self.send_mail.call_args[0]
        self.assertNotIn("Pixel 8", text)
        self.assertNotIn("分设备", text)


class TestRunGroupsByMailbox(MergedMailTestCase):
    def setUp(self):
        super().setUp()
        self.get_subscribers = self._patch(users, "get_subscribers")
        self._patch_config_mail = mock.patch.object(sender.config, "MAIL_ENABLED", True)
        self._patch_config_mail.start()
        self.addCleanup(self._patch_config_mail.stop)

    def test_same_mailbox_sends_once(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        self.get_subscribers.return_value = [
            device(U_PC, created="2026-01-02 00:00:00"),
            device(U_PHONE, hostname="Pixel 8", platform="Android 14", created="2026-01-01 00:00:00")]
        result = sender.run(day=DAY)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["merged_devices"], 2)
        self.send_mail.assert_called_once()

    def test_distinct_mailboxes_still_send_separately(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        self.get_subscribers.return_value = [
            device(U_PC, "a@example.com"),
            device(U_PHONE, "b@example.com")]
        result = sender.run(day=DAY)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertNotIn("merged_devices", result["results"][0])

    def test_weekly_merge_does_not_count_cross_day_overlap(self):
        # PC used 0-3600 on Monday, phone 0-3600 on Tuesday: same seconds,
        # different days, so overlap must be 0 and merged total the plain sum.
        monday = SUNDAY - timedelta(days=6)
        tuesday = monday + timedelta(days=1)

        def weekly(key, start, end):
            apps = [app_row("wechat", 3600, 1)]
            out = {"start": str(start), "end": str(end), "days_with_data": 1,
                   "total_seconds": 3600, "app_count": 1, "apps": apps,
                   "daily_apps": [{"day": str(monday if key == U_PC else tuesday),
                                   "app_name": "wechat", "seconds": 3600}]}
            return out
        self.get_range.side_effect = weekly

        def read_range(kind, key, start, end):
            if kind == "app_intervals":
                day = monday if key == U_PC else tuesday
                return [{"day": str(day), "app_name": "wechat",
                         "start_second": 0, "end_second": 3600}]
            if kind == "timeline":
                day = monday if key == U_PC else tuesday
                return [{"day": str(day), "bucket": 0, "app_name": "wechat",
                         "seconds": 3600}]
            return []
        self.read_range.side_effect = read_range
        self.get_subscribers.return_value = [
            device(U_PC, created="2026-01-02 00:00:00"),
            device(U_PHONE, hostname="Pixel 8", platform="Android 14", created="2026-01-01 00:00:00")]
        result = sender.run(day=SUNDAY)
        self.assertEqual(result["sent"], 1)
        _, subject, text, _ = self.send_mail.call_args[0]
        self.assertIn("2 小时", subject)  # 3600 + 3600, no overlap deducted
        self.assertNotIn("双设备重叠", text)


class TestWinnerTimeline(unittest.TestCase):
    def test_segments_follow_real_spans_and_split_browser(self):
        from mail.charts import daily_winner_timeline, timeline_segments
        summary = {"apps": [
            {"app_name": "chrome.exe", "display_name": "Chrome 浏览器", "seconds": 4800},
            {"app_name": "weixin.exe", "display_name": "微信", "seconds": 7200},
            {"app_name": "douyin.exe", "display_name": "抖音", "seconds": 7200}]}
        app_intervals = [
            {"day": str(DAY), "app_name": "chrome.exe", "start_second": 32400, "end_second": 34200},
            {"day": str(DAY), "app_name": "chrome.exe", "start_second": 36000, "end_second": 39000},
            {"day": str(DAY), "app_name": "weixin.exe", "start_second": 32400, "end_second": 36000},
            {"day": str(DAY), "app_name": "weixin.exe", "start_second": 34200, "end_second": 37800},
            {"day": str(DAY), "app_name": "douyin.exe", "start_second": 75600, "end_second": 82800},
        ]
        site_intervals = [
            {"day": str(DAY), "host": "douyin.com", "start_second": 36000, "end_second": 38400},
            {"day": str(DAY), "host": "chatgpt.com", "start_second": 38400, "end_second": 39000},
        ]
        app_segments, first, last = timeline_segments(
            summary, app_intervals, site_intervals, DAY)
        # Weixin ran 09:00-10:00 on PC and 09:30-10:30 on the phone: one
        # continuous 90-minute run across the device switch, so it owns the
        # contested 10:00-10:30 stretch over chrome's 50-minute run, and
        # chrome only takes 10:30-10:50. A per-device sum would have split
        # the weixin run at the switch and told the wrong story.
        # Labels are plain display names: no P/C/M marker on the timeline.
        self.assertEqual((first, last), (32400, 82800))
        self.assertEqual(app_segments, [
            (32400, 37800, "weixin.exe", "微信", []),
            (37800, 39000, "chrome.exe", "Chrome 浏览器",
             [(37800, 38400, "douyin.com", "抖音"),
              (38400, 39000, "chatgpt.com", "ChatGPT")]),
            (75600, 82800, "douyin.exe", "抖音", []),
        ])
        html_body = daily_winner_timeline(
            summary, app_intervals, site_intervals, DAY)
        self.assertIn("今日时间线", html_body)
        self.assertIn("data:image/png", html_body)

    def test_timeline_survives_sub_two_pixel_fragments(self):
        # A day of 45-second fragments: each maps to about 1px, and the old
        # +1/-1 insets inverted the rectangle (x1 < x0) and crashed the
        # whole mail into the plain old report. Every fragment must draw.
        from mail.charts import daily_winner_timeline
        summary = {"apps": [
            {"app_name": "weixin.exe", "display_name": "微信", "seconds": 5000},
            {"app_name": "douyin.exe", "display_name": "抖音", "seconds": 3000},
            {"app_name": "chrome.exe", "display_name": "Chrome 浏览器", "seconds": 2000}]}
        intervals = []
        start = 25200
        for i in range(60):
            app = ["weixin.exe", "douyin.exe", "chrome.exe"][i % 3]
            intervals.append({"day": str(DAY), "app_name": app,
                              "start_second": start, "end_second": start + 45})
            start += 900
        sites = [{"day": str(DAY), "host": "douyin.com",
                  "start_second": 30000, "end_second": 30040}]
        html_body = daily_winner_timeline(summary, intervals, sites, DAY)
        self.assertIn("data:image/png", html_body)

    def test_label_rows_stagger_dense_names(self):
        # Eight close labels must all find a row across the four staggered
        # levels instead of sharing one line and dropping most of them.
        from mail.charts import _assign_label_rows, _fit_label, cjk_font
        from PIL import ImageFont
        # Measure with the real CJK font: load_default has no CJK glyphs
        # and would under-measure these labels by half.
        font = cjk_font(14) or ImageFont.load_default(size=14)
        # Eight ~112px labels 40px apart, all preferring above: neighbours
        # cannot share a row, so placement must spill into a third level.
        items = [(100 + i * 40, "应用名称测试一二", True) for i in range(8)]
        placed = _assign_label_rows(items, font)
        self.assertEqual(len([p for p in placed if p is not None]), 8)
        rows_used = {p[1] for p in placed if p is not None}
        self.assertGreaterEqual(len(rows_used), 3)
        self.assertEqual(_fit_label("Chrome 浏览器", font), "Chrome 浏览器")

    def test_leader_lines_connect_labels_to_blocks(self):
        # Each row label gets a thin vertical leader down/up to its block,
        # so a name is never left floating detached from its segment.
        import base64
        import io
        import re
        from PIL import Image
        from mail.charts import daily_winner_timeline
        summary = {"apps": [
            {"app_name": "chrome.exe", "display_name": "Chrome 浏览器", "seconds": 4800},
            {"app_name": "weixin.exe", "display_name": "微信", "seconds": 7200},
            {"app_name": "douyin.exe", "display_name": "抖音", "seconds": 7200}]}
        app_intervals = [
            {"day": str(DAY), "app_name": "chrome.exe", "start_second": 32400, "end_second": 34200},
            {"day": str(DAY), "app_name": "chrome.exe", "start_second": 36000, "end_second": 39600},
            {"day": str(DAY), "app_name": "weixin.exe", "start_second": 32400, "end_second": 36000},
            {"day": str(DAY), "app_name": "weixin.exe", "start_second": 34200, "end_second": 37800},
            {"day": str(DAY), "app_name": "douyin.exe", "start_second": 75600, "end_second": 82800},
        ]
        site_intervals = [
            {"day": str(DAY), "host": "douyin.com", "start_second": 37800, "end_second": 39000},
            {"day": str(DAY), "host": "chatgpt.com", "start_second": 39000, "end_second": 39600},
        ]
        html_body = daily_winner_timeline(summary, app_intervals, site_intervals, DAY)
        match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html_body)
        self.assertIsNotNone(match)
        image = Image.open(io.BytesIO(base64.b64decode(match.group(1)))).convert("RGB")
        leader = (126, 143, 166)  # #7E8FA6
        above = any(image.getpixel((x, y)) == leader
                    for x in range(image.width) for y in range(40, 64))
        below = any(image.getpixel((x, y)) == leader
                    for x in range(image.width) for y in range(104, 118))
        self.assertTrue(above)
        self.assertTrue(below)

    def test_short_segments_get_no_name(self):
        # A stretch of 5 minutes or less shows only its block: no name,
        # and therefore no leader line. A 6-minute stretch gets both.
        import base64
        import io
        import re
        from PIL import Image
        from mail.charts import daily_winner_timeline
        leader = (126, 143, 166)  # #7E8FA6

        def has_leader(seconds):
            summary = {"apps": [{"app_name": "weixin.exe",
                                  "display_name": "微信", "seconds": seconds}]}
            intervals = [{"day": str(DAY), "app_name": "weixin.exe",
                          "start_second": 36000, "end_second": 36000 + seconds}]
            html_body = daily_winner_timeline(summary, intervals, [], DAY)
            match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html_body)
            self.assertIsNotNone(match)
            image = Image.open(io.BytesIO(base64.b64decode(match.group(1)))).convert("RGB")
            return any(image.getpixel((x, y)) == leader
                       for x in range(image.width) for y in range(image.height))

        self.assertFalse(has_leader(300))
        self.assertTrue(has_leader(360))

    def test_timeline_empty_day(self):
        from mail.charts import daily_winner_timeline, timeline_segments
        self.assertEqual(timeline_segments({"apps": []}, [], [], DAY), ([], 0, 0))
        self.assertEqual(daily_winner_timeline({"apps": []}, [], [], DAY), "")

    def test_merged_spans_bridges_small_gaps(self):
        from mail.charts import _merged_spans
        # Overlapping and touching still merge.
        self.assertEqual(_merged_spans([(0, 60), (30, 90)]), [(0, 90)])
        self.assertEqual(_merged_spans([(0, 60), (60, 120)]), [(0, 120)])
        # A gap of 60s or less also merges: the foreground flickering to
        # another app for a few seconds does not break the stretch.
        self.assertEqual(_merged_spans([(0, 60), (120, 180)]), [(0, 180)])
        # A gap over 60s is a real break between two sessions.
        self.assertEqual(_merged_spans([(0, 60), (121, 180)]),
                         [(0, 60), (121, 180)])
        # Chained fragments with small gaps become one span.
        self.assertEqual(
            _merged_spans([(0, 10), (20, 30), (50, 70), (200, 210)]),
            [(0, 70), (200, 210)])

    def test_fragmented_app_wins_after_gap_bridging(self):
        # Regression test for the 2026-11-07 dwrg case: dwrg ran 2h25m but
        # only as 1-10s bursts with the foreground flickering to other
        # apps, so its longest single span was under a minute and it never
        # won the fused timeline against douyin's 24-minute run. With 60s
        # gap bridging the bursts merge into one long run and dwrg wins.
        from mail.charts import timeline_segments
        summary = {"apps": [
            {"app_name": "dwrg.exe", "display_name": "dwrg", "seconds": 8700},
            {"app_name": "douyin.exe", "display_name": "抖音", "seconds": 1440}]}
        # dwrg: 5s bursts from 12:30, 30s apart, until 13:06.
        app_intervals = []
        t = 12 * 3600 + 30 * 60
        end = 13 * 3600 + 6 * 60
        while t < end:
            app_intervals.append({"day": str(DAY), "app_name": "dwrg.exe",
                                  "start_second": t, "end_second": t + 5})
            t += 35
        # douyin: one 24-minute run overlapping dwrg.
        app_intervals.append({"day": str(DAY), "app_name": "douyin.exe",
                              "start_second": 12 * 3600 + 41 * 60,
                              "end_second": 13 * 3600 + 5 * 60})
        app_segments, first, last = timeline_segments(
            summary, app_intervals, [], DAY)
        # dwrg's bridged run (12:30-13:06, ~36 min) beats douyin's
        # 24-minute run, so dwrg owns the whole stretch.
        self.assertEqual((first, last), (12 * 3600 + 30 * 60, 13 * 3600 + 6 * 60 - 20))
        self.assertEqual(app_segments, [
            (12 * 3600 + 30 * 60, 13 * 3600 + 6 * 60 - 20, "dwrg.exe", "dwrg", []),
        ])


class TestGanttLabels(unittest.TestCase):
    def test_website_gantt_labels_show_chinese_and_markers(self):
        from mail.charts import website_gantt_chart
        rows = [{"day": str(DAY), "host": "douyin.com", "seconds": 2721},
                {"day": str(DAY), "host": "hpc5000.shu.edu.cn", "seconds": 2474}]
        intervals = [{"day": str(DAY), "host": "douyin.com",
                      "start_second": 0, "end_second": 2721},
                     {"day": str(DAY), "host": "hpc5000.shu.edu.cn",
                      "start_second": 3600, "end_second": 6074}]
        html_body = website_gantt_chart(
            rows, intervals, DAY,
            sources={"douyin.com": "C", "hpc5000.shu.edu.cn": "C"})
        self.assertIn("data:image/png", html_body)
        self.assertIn("抖音 (C)", html_body)
        self.assertIn("hpc5000.shu.edu.cn (C)", html_body)
        self.assertIn("P 手机，C 电脑，M 两台都有", html_body)

    def test_cjk_font_discovery(self):
        from mail import charts
        charts._cjk_font_cache.clear()
        try:
            # This machine ships Noto CJK; a server without any CJK font
            # must degrade to None instead of raising.
            self.assertIsNotNone(charts.cjk_font(19))
            charts._cjk_font_cache.clear()
            with mock.patch.object(charts.config, "MAIL_CHART_CJK_FONT",
                                  "/nonexistent/font.ttf"):
                with mock.patch.object(charts, "_CJK_FONT_CANDIDATES",
                                      ("/nonexistent/a.ttf",)):
                    self.assertIsNone(charts.cjk_font(19))
        finally:
            charts._cjk_font_cache.clear()


class TestMergedReportRendering(unittest.TestCase):
    def test_merged_breakdown_lines_only_appear_for_merged_summaries(self):
        plain = summary(3600, [app_row("wechat", 3600)])
        self.assertNotIn("分设备", report.build_text(DAY, plain))
        merged = dict(plain)
        merged["_devices"] = [{"label": "PC", "total_seconds": 3600, "app_count": 1},
                              {"label": "Pixel", "total_seconds": 1800, "app_count": 1}]
        merged["_overlap_seconds"] = 1800
        text = report.build_text(DAY, merged)
        self.assertIn("分设备", text)
        self.assertIn("双设备重叠：30 分", text)


if __name__ == "__main__":
    unittest.main()
