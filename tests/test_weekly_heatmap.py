"""GET /api/v1/weekly-heatmap: the weekly mail's two heatmaps as data.

Pure builder tests (no DB) plus the auth gate on the endpoint itself.
Fixtures mirror the merged mail's input shapes: per-device range
summaries with daily_apps, unionable app intervals, web daily totals
and web_hourly rows.
"""
import unittest
from datetime import date
from uuid import uuid4

from fastapi import HTTPException

from api.tracking import build_weekly_heatmap, read_weekly_heatmap

START = date(2026, 11, 9)    # Monday
END = date(2026, 11, 15)     # Sunday


def app_summary(apps, daily_apps):
    return {"start": START.isoformat(), "end": END.isoformat(),
            "total_seconds": sum(a["seconds"] for a in apps),
            "app_count": len(apps), "apps": apps, "daily_apps": daily_apps}


def app_row(name, seconds, display=""):
    return {"app_name": name, "display_name": display or name,
            "seconds": seconds, "launches": 1}


def interval(day, app, start_second, end_second):
    return {"day": day.isoformat(), "app_name": app,
            "start_second": start_second, "end_second": end_second}


class TestBuildWeeklyHeatmap(unittest.TestCase):
    def setUp(self):
        # 11-09: wechat 10:00-11:00 and 14:00-14:30 (5400s).
        # 11-11: dwrg 20:00-21:00 (3600s), no uploaded display name.
        self.summaries = [("Pixel", app_summary(
            [app_row("wechat", 5400, "微信"), app_row("dwrg", 3600, "")],
            [{"day": "2026-11-09", "app_name": "wechat", "seconds": 5400},
             {"day": "2026-11-11", "app_name": "dwrg", "seconds": 3600}]))]
        self.intervals = [
            interval(date(2026, 11, 9), "wechat", 36000, 39600),
            interval(date(2026, 11, 9), "wechat", 50400, 52200),
            interval(date(2026, 11, 11), "dwrg", 72000, 75600),
        ]
        self.site_rows = [
            {"day": "2026-11-09", "host": "example.com", "seconds": 1800},
            {"day": "2026-11-09", "host": "bilibili.com", "seconds": 600},
        ]
        self.site_hours = [
            {"day": "2026-11-09", "hour": 10, "host": "example.com", "seconds": 1800},
            {"day": "2026-11-09", "hour": 10, "host": "bilibili.com", "seconds": 600},
        ]
        self.readiness = [
            {"day": "2026-11-09", "kind": "apps", "complete": True},
            {"day": "2026-11-09", "kind": "timeline", "complete": True},
            {"day": "2026-11-09", "kind": "web_hourly", "complete": True},
            {"day": "2026-11-11", "kind": "apps", "complete": True},
            {"day": "2026-11-11", "kind": "timeline", "complete": True},
        ]

    def build(self):
        return build_weekly_heatmap(START, END, self.summaries, self.intervals,
                                    self.site_rows, self.site_hours, self.readiness)

    def test_window_and_days(self):
        out = self.build()
        self.assertEqual(out["start"], "2026-11-09")
        self.assertEqual(out["end"], "2026-11-15")
        self.assertEqual(out["days"],
                         ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12",
                          "2026-11-13", "2026-11-14", "2026-11-15"])

    def test_app_buckets_for_known_intervals(self):
        out = self.build()
        app = out["app"]
        self.assertEqual(app["max_seconds"], 3600)
        day0 = app["days"][0]
        self.assertEqual(len(day0), 24)
        self.assertEqual(day0[10], {"seconds": 3600, "top": "微信"})
        self.assertEqual(day0[14], {"seconds": 1800, "top": "微信"})
        self.assertEqual(day0[0], {"seconds": 0, "top": ""})
        # dwrg resolves through the same display-name logic as the mail.
        self.assertEqual(app["days"][2][20], {"seconds": 3600, "top": "第五人格"})

    def test_missing_day_returns_null(self):
        out = self.build()
        self.assertIsNone(out["app"]["days"][1])   # 11-10: no data at all
        self.assertIsNone(out["app"]["days"][3])
        self.assertIsNone(out["app"]["days"][4])
        self.assertIsNone(out["app"]["days"][5])
        self.assertIsNone(out["app"]["days"][6])
        self.assertIsNone(out["web"]["days"][1])

    def test_web_buckets_present_when_web_data_exists(self):
        out = self.build()
        web = out["web"]
        self.assertEqual(web["max_seconds"], 2400)
        self.assertEqual(web["days"][0][10], {"seconds": 2400, "top": "example.com"})
        self.assertEqual(web["days"][0][0], {"seconds": 0, "top": ""})

    def test_no_app_data_returns_all_null_app_days(self):
        out = build_weekly_heatmap(START, END, [], [], self.site_rows,
                                   self.site_hours, self.readiness)
        self.assertEqual(out["app"]["days"], [None] * 7)
        self.assertEqual(out["app"]["max_seconds"], 0)
        # Web data is independent of the app summary.
        self.assertEqual(out["web"]["days"][0][10],
                         {"seconds": 2400, "top": "example.com"})

    def test_no_web_data_returns_all_null_web_days(self):
        # No web rows and no web_hourly readiness at all: every day is
        # missing, not a zero. (A day whose web_hourly upload completed
        # with zero totals is a real empty day and comes back as 24 zero
        # cells instead — the model's "complete empty is zero" rule.)
        readiness = [r for r in self.readiness if r["kind"] != "web_hourly"]
        out = build_weekly_heatmap(START, END, self.summaries, self.intervals,
                                   [], [], readiness)
        self.assertEqual(out["web"]["days"], [None] * 7)
        self.assertEqual(out["web"]["max_seconds"], 0)
        self.assertEqual(out["app"]["days"][0][10], {"seconds": 3600, "top": "微信"})

    def test_complete_empty_web_day_is_zero_cells_not_null(self):
        # web_hourly complete but no rows: the model treats it as an
        # observed empty day (same rule as the mail heatmap).
        out = build_weekly_heatmap(START, END, self.summaries, self.intervals,
                                   [], [], self.readiness)
        self.assertEqual(len(out["web"]["days"][0]), 24)
        self.assertEqual(out["web"]["days"][0][10], {"seconds": 0, "top": ""})
        self.assertIsNone(out["web"]["days"][1])

    def test_overlapping_devices_do_not_double_count(self):
        # Same app on both devices at the same time: the heatmap shows
        # real wall-clock usage (3600s), not the summed 7200s.
        phone = app_summary([app_row("wechat", 3600, "微信")],
                            [{"day": "2026-11-09", "app_name": "wechat",
                              "seconds": 3600}])
        pc = app_summary([app_row("wechat", 3600, "微信")],
                         [{"day": "2026-11-09", "app_name": "wechat",
                           "seconds": 3600}])
        intervals = [
            interval(date(2026, 11, 9), "wechat", 36000, 39600),
            interval(date(2026, 11, 9), "wechat", 36000, 39600),
        ]
        out = build_weekly_heatmap(START, END, [("Pixel", phone), ("PC", pc)],
                                   intervals, [], [], self.readiness)
        self.assertEqual(out["app"]["days"][0][10], {"seconds": 3600, "top": "微信"})
        self.assertEqual(out["app"]["max_seconds"], 3600)


class TestWeeklyHeatmapAuth(unittest.TestCase):
    def test_no_bearer_is_rejected_before_any_db_access(self):
        with self.assertRaises(HTTPException) as ctx:
            read_weekly_heatmap(uuid4(), end=None, authorization="")
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
