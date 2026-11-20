"""测试 monitor.py 的空闲时间判定和片段结算逻辑。

这些是项目里最容易出错、也最值得测试的地方：
    * tick 计数回绕（Windows 的 GetTickCount 约 49.7 天归零一次）
    * 电脑休眠/长时间无操作后，不能把整段睡眠时间都算成「在用电脑」

测试完全不需要真的去操作键鼠：所有跟系统打交道的函数都用 mock 替换掉了。
"""
import itertools
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from system.monitor import (
    WindowMonitor,
    _active_time_cutoff,
    _idle_seconds_from_ticks,
    _is_audio_playing,
    _is_foreground_fullscreen,
    _is_media_active,
    _media_aware_active_end,
)


class IdleTimeTests(unittest.TestCase):
    def test_tick_counter_wrap_keeps_idle_time_positive(self):
        """tick 计数回绕后，空闲时长仍然应该是正的小数值。

        current_tick = 0x10（16）、last_input_tick = 0xFFFFFFF0（很大），
        如果直接相减会得到负数；按 32 位无符号回绕处理，
        实际经过的时间是 16 - 0xFFFFFFF0 绕一圈 = 32 毫秒 = 0.032 秒。
        """
        self.assertAlmostEqual(
            _idle_seconds_from_ticks(0x00000010, 0xFFFFFFF0),
            0.032,
        )

    def test_active_time_is_capped_after_a_long_sleep(self):
        """长时间空闲后，计时终点要被「往回拨」到合理的位置。

        现在 = 7200 秒，已空闲 3600 秒，阈值 300 秒：
        只应该算到 7200 - 3600 + 300 = 3900 秒为止，
        也就是「最后一次操作后 300 秒」，多出来的 3300 秒不该计入。
        """
        now = 7_200.0
        idle_seconds = 3_600.0
        threshold = 300

        self.assertEqual(
            _active_time_cutoff(now, idle_seconds, threshold),
            3_900.0,
        )

    def test_recent_activity_uses_current_time(self):
        """空闲时长没超过阈值时，终点就是「现在」，不做任何削减。"""
        self.assertEqual(_active_time_cutoff(1_000.0, 30.0, 300), 1_000.0)

    @patch("system.monitor.get_ignored_apps", return_value=set())
    def test_flush_current_accepts_an_idle_cutoff(self, _ignored_apps):
        """传入截断时间后，结算出的时长和结束时间都要用这个值。

        开始于 100.0 秒，传入的结束时间 160.0 秒 → 时长应为 60 秒。
        patch 的参数名 _ignored_apps 以下划线开头，表示这个假对象用不到，
        只是为了接住 patch 传进来的位置参数。
        """
        monitor = WindowMonitor()
        # 手工摆出「正在计时」的状态，这样不用真的启动监控线程
        monitor._last_app = "example.exe"
        monitor._last_title = "Example"
        monitor._last_exe = "C:/Example/example.exe"
        monitor._last_start = 100.0

        monitor._flush_current(160.0)

        self.assertEqual(len(monitor._buffer), 1)
        self.assertEqual(monitor._buffer[0]["duration"], 60.0)
        self.assertEqual(
            datetime.fromisoformat(monitor._buffer[0]["end_time"]),
            datetime.fromtimestamp(160.0),
        )

    @patch("system.monitor.record_fragments_batch")
    @patch("system.monitor.get_ignored_apps", return_value=set())
    @patch("system.monitor.get_setting", return_value="300")
    @patch("system.monitor._get_idle_seconds", return_value=3_600.0)
    @patch("system.monitor.time.time", return_value=7_200.0)
    def test_stop_after_sleep_does_not_count_the_whole_sleep(
        self,
        _time,
        _idle_seconds,
        _setting,
        _ignored_apps,
        record_batch,
    ):
        """核心回归测试：睡了一觉之后停止监控，睡眠时间不能被算进使用时长。

        场景：3500 秒时开始用某应用，之后电脑空闲了 3600 秒，
        在 7200 秒时程序退出。
        正确结果：只记到 3500 + 400 = 3900 秒为止，时长 400 秒。
        （3900 = 7200 - 3600 + 300，即「最后操作时刻 + 阈值」）

        patch 装饰器是自上而下写、自下而上生效的，所以函数参数要按
        由下往上的顺序接收这些假对象（除 self 外）。
        """
        monitor = WindowMonitor()
        monitor._last_app = "example.exe"
        monitor._last_title = "Example"
        monitor._last_exe = "C:/Example/example.exe"
        monitor._last_start = 3_500.0

        monitor.stop()

        # call_args.args[0] 取出被 mock 的 record_fragments_batch 收到的第一个参数，
        # 也就是准备写库的那批片段
        fragments = record_batch.call_args.args[0]
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]["duration"], 400.0)


class MediaActivityTests(unittest.TestCase):
    """全屏 / 音频检测，以及「看东西时不判空闲」的逻辑。

    背景：看直播、看视频时人可能几分钟不动键鼠，但「在用电脑」是真的。
    所以空闲判定除了看键鼠，还要看前台是不是真全屏、系统有没有在出声音。
    """

    def _fullscreen_mocks(self, rect, style, monitor_rect=(0, 0, 1920, 1080)):
        """返回一组 mock，让前台窗口看起来是给定的矩形和样式。"""
        gui = MagicMock()
        api = MagicMock()
        con = MagicMock()
        con.GWL_STYLE = -16
        con.WS_CAPTION = 0x00C00000
        con.MONITOR_DEFAULTTONEAREST = 2
        gui.GetForegroundWindow.return_value = 1234
        gui.GetWindowRect.return_value = rect
        api.GetMonitorInfo.return_value = {"Monitor": monitor_rect}
        gui.GetWindowLong.return_value = style
        return gui, api, con

    def test_fullscreen_window_counts_as_active(self):
        """前台窗口真全屏（盖满屏幕、无标题栏）-> 在用。"""
        gui, api, con = self._fullscreen_mocks((0, 0, 1920, 1080), 0)
        with patch("system.monitor.win32gui", gui), \
             patch("system.monitor.win32api", api), \
             patch("system.monitor.win32con", con):
            self.assertTrue(_is_foreground_fullscreen())

    def test_maximized_window_with_title_bar_is_not_fullscreen(self):
        """最大化窗口还留着标题栏 -> 不算全屏，人走开也该算空闲。"""
        gui, api, con = self._fullscreen_mocks((0, 0, 1920, 1080), 0x00C00000)
        with patch("system.monitor.win32gui", gui), \
             patch("system.monitor.win32api", api), \
             patch("system.monitor.win32con", con):
            self.assertFalse(_is_foreground_fullscreen())

    def test_windowed_window_is_not_fullscreen(self):
        """窗口化（没盖满屏幕）-> 不算全屏。"""
        gui, api, con = self._fullscreen_mocks((100, 100, 1500, 900), 0)
        with patch("system.monitor.win32gui", gui), \
             patch("system.monitor.win32api", api), \
             patch("system.monitor.win32con", con):
            self.assertFalse(_is_foreground_fullscreen())

    def test_no_foreground_window_is_not_fullscreen(self):
        """没有前台窗口 -> 不算全屏。"""
        gui, api, con = self._fullscreen_mocks((0, 0, 1920, 1080), 0)
        gui.GetForegroundWindow.return_value = 0
        with patch("system.monitor.win32gui", gui), \
             patch("system.monitor.win32api", api), \
             patch("system.monitor.win32con", con):
            self.assertFalse(_is_foreground_fullscreen())

    def _audio_mocks(self, peak):
        """返回一组 mock，让输出设备的峰值看起来是给定的值。"""
        meter = MagicMock()
        meter.GetPeakValue.return_value = peak
        iface = MagicMock()
        iface.QueryInterface.return_value = meter
        speakers = MagicMock()
        speakers.Activate.return_value = iface
        utils = MagicMock()
        utils.GetSpeakers.return_value = speakers
        return utils

    def test_audio_playing_when_peak_above_zero(self):
        """输出设备峰值 > 0 -> 有声音在放。"""
        utils = self._audio_mocks(0.5)
        with patch("system.monitor._PYCAW_AVAILABLE", True), \
             patch("system.monitor.AudioUtilities", utils), \
             patch("system.monitor.IAudioMeterInformation", MagicMock()), \
             patch("system.monitor.CLSCTX_ALL", 1), \
             patch("system.monitor._audio_meter", None):
            self.assertTrue(_is_audio_playing())

    def test_audio_silent_when_peak_is_zero(self):
        """峰值 0 -> 没声音。"""
        utils = self._audio_mocks(0.0)
        with patch("system.monitor._PYCAW_AVAILABLE", True), \
             patch("system.monitor.AudioUtilities", utils), \
             patch("system.monitor.IAudioMeterInformation", MagicMock()), \
             patch("system.monitor.CLSCTX_ALL", 1), \
             patch("system.monitor._audio_meter", None):
            self.assertFalse(_is_audio_playing())

    def test_audio_detection_off_when_pycaw_missing(self):
        """pycaw 没装 -> 音频检测关闭，返回 False，不报错。"""
        with patch("system.monitor._PYCAW_AVAILABLE", False):
            self.assertFalse(_is_audio_playing())

    def test_media_active_is_either_signal(self):
        """全屏或音频任一成立就算在看东西。"""
        with patch("system.monitor._is_foreground_fullscreen", return_value=True), \
             patch("system.monitor._is_audio_playing", return_value=False):
            self.assertTrue(_is_media_active())
        with patch("system.monitor._is_foreground_fullscreen", return_value=False), \
             patch("system.monitor._is_audio_playing", return_value=True):
            self.assertTrue(_is_media_active())
        with patch("system.monitor._is_foreground_fullscreen", return_value=False), \
             patch("system.monitor._is_audio_playing", return_value=False):
            self.assertFalse(_is_media_active())

    def test_media_aware_end_uses_last_media_time(self):
        """媒体活动比 cutoff 晚时，收尾点取媒体时间。

        now=10000，已空闲 3600 秒，阈值 300：普通 cutoff 是 6700，
        但媒体最后活动在 9900 -> 应取 9900。
        """
        self.assertEqual(
            _media_aware_active_end(10000, 3600, 300, 9900), 9900
        )

    def test_media_aware_end_falls_back_to_cutoff(self):
        """没有媒体活动（或媒体活动比 cutoff 早）时，就是普通 cutoff。"""
        self.assertEqual(
            _media_aware_active_end(10000, 3600, 300, None), 6700
        )
        self.assertEqual(
            _media_aware_active_end(10000, 3600, 300, 6000), 6700
        )

    @patch("system.monitor.record_fragments_batch")
    @patch("system.monitor.get_ignored_apps", return_value=set())
    @patch("system.monitor.get_setting", return_value="300")
    @patch("system.monitor._get_idle_seconds", return_value=3600.0)
    def test_watching_stream_does_not_trigger_idle(
        self,
        _idle_seconds,
        _setting,
        _ignored_apps,
        _record_batch,
    ):
        """核心场景：看直播时不动键鼠不进空闲；停播后空闲从最后媒体时间起算。

        键鼠一直超时（3600 秒），前两拍媒体开着（在看直播），
        第三拍起媒体关掉。正确结果：前两拍不进空闲；第三拍进空闲，
        且空闲起点是「最后一次媒体活动」的时刻（约第 2 拍），
        而不是普通 cutoff（最后操作 + 300 秒，几小时前）。
        """
        monitor = WindowMonitor()
        monitor._running = True
        monitor._last_app = "chrome.exe"
        monitor._last_title = "直播"
        monitor._last_exe = "C:/chrome.exe"
        monitor._last_start = 1000.0
        monitor._last_hwnd = 111
        monitor._last_flush = 1000.0
        monitor._was_idle = False

        # 前两拍媒体开着，之后一直关着（repeat 保证不会耗尽而抛错）
        media_seq = itertools.chain([True, True], itertools.repeat(False))
        sleep_calls = {"n": 0}

        def fake_sleep(_s):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 20:
                raise RuntimeError("主循环没有按预期停下")
            if sleep_calls["n"] >= 5:
                monitor._running = False

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 1.0
            return clock["t"]

        with patch(
            "system.monitor._is_media_active",
            side_effect=lambda: next(media_seq),
        ), patch.object(
            WindowMonitor,
            "_get_active_window_info",
            return_value=(111, "chrome.exe", "直播", "C:/chrome.exe"),
        ), patch(
            "system.monitor.time"
        ) as mock_time:
            mock_time.time.side_effect = fake_time
            mock_time.sleep.side_effect = fake_sleep
            monitor._loop()

        self.assertTrue(monitor._was_idle)
        # 空闲起点是第 2 拍（最后一次媒体活动），约 1002，而不是几小时前
        self.assertGreaterEqual(monitor._idle_start, 1000.0)
        self.assertLessEqual(monitor._idle_start, 1010.0)


if __name__ == "__main__":
    # 直接运行本文件时执行测试（也可以从项目根目录用 unittest discover 跑）
    unittest.main()
