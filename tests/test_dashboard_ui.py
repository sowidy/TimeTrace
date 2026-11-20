"""UI regressions for inline website dates, opt-in controls and dark email contrast."""
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QPalette
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from core import storage, site_storage


class DashboardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from ui import dashboard
        cls.ui = dashboard

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(storage, "DB_PATH", str(Path(self.temp.name) / "usage.db"))
        patch.start()
        self.addCleanup(patch.stop)
        storage.init_db()
        self.ui.apply_theme(False)
        self.addCleanup(self.ui.apply_theme, False)
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            if hasattr(window, "browser_settings"):
                window.browser_settings.timer.stop()
            if hasattr(window, "_clock_timer"):
                window._clock_timer.stop()
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def dashboard(self):
        window = self.ui.Dashboard()
        self.windows.append(window)
        return window

    def record(self, day, host, hour=9, seconds=600):
        start = datetime.combine(day, time(hour=hour)).timestamp()
        site_storage.record_segment({"event_id": str(uuid4()), "session_id": str(uuid4()),
            "browser": "chrome", "host": host}, [(start, start + seconds)])

    def test_website_timeline_is_embedded_and_follows_main_date(self):
        today = date.today()
        self.assertTrue(self.ui.APP_TITLE.startswith("TimeTrace"))
        self.record(today, "today.example")
        self.record(today - timedelta(days=1), "yesterday.example", hour=14)
        window = self.dashboard()
        self.assertFalse(window.website_panel.isWindow())
        self.assertEqual([r["display_name"] for r in window.website_panel.timeline._lanes], ["today.example"])
        window._prev_period()
        lanes = window.website_panel.timeline._lanes
        self.assertEqual([r["display_name"] for r in lanes], ["yesterday.example"])
        self.assertEqual(lanes[0]["total"], 600)
        self.assertEqual(list(lanes[0]["intervals"][0]), [14 * 3600, 14 * 3600 + 600])

    def test_website_categories_use_real_local_site_totals(self):
        today = date.today()
        self.record(today, "github.com", seconds=600)
        self.record(today, "youtube.com", hour=10, seconds=300)
        window = self.dashboard()
        pie = window.website_panel.category_chart
        values = {name: seconds for name, seconds, _ in pie._values}
        self.assertEqual(values, {"视频娱乐": 300, "工作学习": 600})
        window.show()
        self.app.processEvents()
        self.assertEqual(len(pie._wedges), 2)

    def test_weekly_websites_are_stacked_by_date_with_segment_hover(self):
        today = date.today()
        self.record(today, "today.example")
        self.record(today, "second.example", hour=10, seconds=300)
        self.record(today - timedelta(days=1), "yesterday.example", seconds=1200)
        window = self.dashboard()
        window._switch_tab("weekly")
        panel = window.website_panel
        self.assertEqual(len(panel.week_chart._days), 7)
        self.assertTrue(panel.scroll.isHidden())
        self.assertEqual(panel.title.text(), "每日网站时长")
        self.assertIn("7 天合计", panel.summary.text())
        self.assertEqual(panel.week_chart._days[-1][1],
                         [("today.example", 600.0), ("second.example", 300.0)])
        self.assertEqual(panel.week_chart._days[-2][1], [("yesterday.example", 1200.0)])
        hit = (today, "second.example", 300.0)
        self.assertIn("second.example", panel.week_chart._tooltip(hit))
        self.assertIn(f"{today:%Y年%m月%d日}", panel.week_chart._tooltip(hit))
        self.assertIn("网站：second.example", panel.week_chart._tooltip(hit))
        self.assertIn("用时：5分钟", panel.week_chart._tooltip(hit))
        self.assertIn("5分钟", panel.week_chart._tooltip(hit))
        column_detail = panel.week_chart._tooltip((today, None, panel.week_chart._days[-1][1]))
        self.assertIn("today.example", column_detail)
        self.assertIn("second.example", column_detail)
        window.show()
        self.app.processEvents()
        from ui import browser_panel
        segment = next(rect for rect, day, host, _ in panel.week_chart._segments
                       if day == today and host == "second.example")
        with mock.patch.object(browser_panel, "QToolTip") as tooltip:
            QTest.mouseMove(panel.week_chart, QPoint(round(segment.center().x()),
                                                     round(segment.center().y())))
            self.assertIn("second.example", tooltip.showText.call_args.args[1])
            self.assertIn(f"{today:%Y年%m月%d日}", tooltip.showText.call_args.args[1])
        window._refresh_weekly()
        self.assertEqual(len(panel.week_chart._days), 7)

    def test_app_charts_show_date_and_duration_on_hover(self):
        today = date.today().isoformat()
        for chart, setup, expected in (
                (self.ui._BarChart(), lambda c: c.set_data(
                    [{"date": today, "total_seconds": 600}], today), today),
                (self.ui._HourlyChart(), lambda c: c.set_data(
                    [600] + [0] * 23), "00:00")):
            with self.subTest(chart=type(chart).__name__):
                chart.resize(500, 160)
                setup(chart)
                chart.show()
                self.app.processEvents()
                rect = chart._bar_rects[0]
                with mock.patch.object(self.ui, "QToolTip") as tooltip:
                    QTest.mouseMove(chart, QPoint(rect[0] + rect[2] // 2,
                                                  rect[1] + max(1, rect[3] // 2)))
                    self.assertIn(expected, tooltip.showText.call_args.args[1])
                    self.assertIn("10分钟", tooltip.showText.call_args.args[1])
                    if isinstance(chart, self.ui._HourlyChart):
                        self.assertNotIn(today, tooltip.showText.call_args.args[1])
                chart.close()

    def test_daily_timeline_hover_has_name_interval_and_duration_without_date(self):
        today = date.today()
        self.record(today, "docs.example", hour=9, seconds=600)
        window = self.dashboard()
        timeline = window.website_panel.timeline
        timeline.resize(400, 130)
        x = timeline._MARGIN_LEFT + (9 * 3600 + 300) / 86400 * (
            timeline.width() - timeline._MARGIN_LEFT - timeline._MARGIN_RIGHT)
        detail = timeline._hover_detail(x, timeline._HEADER_H + 8)
        self.assertIn("网站：docs.example", detail)
        self.assertNotIn(today.isoformat(), detail)
        self.assertIn("09:00:00", detail)
        self.assertIn("用时：10分钟", detail)
        window.show()
        self.app.processEvents()
        x = timeline._MARGIN_LEFT + (9 * 3600 + 300) / 86400 * (
            timeline.width() - timeline._MARGIN_LEFT - timeline._MARGIN_RIGHT)
        with mock.patch.object(self.ui, "QToolTip") as tooltip:
            QTest.mouseMove(timeline, QPoint(round(x), timeline._HEADER_H + 8))
            self.assertIn("docs.example", tooltip.showText.call_args.args[1])

    def test_short_app_timeline_segment_is_hoverable_with_name_and_duration(self):
        timeline = self.ui._TimelineView()
        timeline.resize(400, 130)
        timeline.set_data([{"app_name": "example.exe",
                            "start_time": "2026-10-26T09:00:00",
                            "end_time": "2026-10-26T09:00:01"}])
        timeline.show()
        self.app.processEvents()
        rect = timeline._interval_rect(9 * 3600, 9 * 3600 + 1, 0)
        self.assertEqual(rect.width(), 2)
        pos = QPoint(rect.left() + 1, rect.center().y())
        detail = timeline._hover_detail(pos.x(), pos.y())
        self.assertIn("应用：", detail)
        self.assertIn("example", detail)
        self.assertIn("用时：1秒", detail)
        self.assertNotIn("2026-10-26", detail)
        with mock.patch.object(self.ui, "QToolTip") as tooltip:
            QTest.mouseMove(timeline, pos)
            self.assertIn("用时：1秒", tooltip.showText.call_args.args[1])
        timeline.close()

    def test_settings_grouping_retains_local_opt_in_without_enabling_upload(self):
        window = self.dashboard()
        window._show_settings_section("web")
        for key, cards in window._settings_groups.items():
            self.assertTrue(all(card.isHidden() == (key != "web") for card in cards))
        panel = window.browser_settings
        self.assertFalse(any(button.text() == "查看网站记录" for button in panel.findChildren(QPushButton)))
        panel.checkboxes["web_tracking_enabled"].setChecked(True)
        self.assertEqual(storage.get_setting("web_tracking_enabled"), "1")
        self.assertEqual(storage.get_setting("web_upload_enabled", "0"), "0")
        self.assertEqual(storage.get_setting("timeline_upload_enabled", "0"), "0")

    def test_manual_upload_includes_opted_in_website_data(self):
        from net import uploader
        window = self.dashboard()
        with mock.patch.object(window, "_start_bg", side_effect=lambda name, fn: fn()), \
                mock.patch.object(uploader, "upload_all") as upload:
            window._upload_now()
        upload.assert_called_once_with(force=True)

    def test_sync_summary_does_not_hide_website_failure_behind_application_success(self):
        storage.set_setting("upload_last_success", "2026-11-02 18:52")
        storage.set_setting("web_upload_enabled", "1")
        storage.set_setting("web_upload_status", "HTTP 503")
        window = self.dashboard()
        summary = window.lbl_upload_summary.text()
        self.assertIn("应用最近上传：2026-11-02 18:52", summary)
        self.assertIn("网站上传：服务端暂时无法写入数据", summary)

    def test_email_field_has_readable_background_in_both_themes_and_disabled_state(self):
        def luminance(color):
            channels = [color.redF(), color.greenF(), color.blueF()]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))
        for dark in (False, True):
            with self.subTest(dark=dark):
                self.ui.apply_theme(dark)
                window = self.dashboard()
                window._switch_tab("settings")
                window._show_settings_section("sync")
                edit = window.edit_email
                edit.setText("reader@example.com")
                edit.setEnabled(True)
                edit.ensurePolished()
                palette = edit.palette()
                background, foreground = palette.color(QPalette.Base), palette.color(QPalette.Text)
                self.assertEqual(background.name(), self.ui.BG.lower())
                contrast = sorted((luminance(background), luminance(foreground)))
                self.assertGreaterEqual((contrast[1] + .05) / (contrast[0] + .05), 4.5)
                edit.setEnabled(False)
                disabled = edit.palette()
                contrast = sorted((luminance(disabled.color(QPalette.Disabled, QPalette.Base)),
                                   luminance(disabled.color(QPalette.Disabled, QPalette.Text))))
                self.assertGreaterEqual((contrast[1] + .05) / (contrast[0] + .05), 3)
