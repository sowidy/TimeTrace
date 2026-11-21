"""后台监控线程：每秒看一眼你在用哪个软件，把使用时长记下来。

核心思路（轮询法，最简单可靠）：
    每 1 秒问一次 Windows：「现在最前面那个窗口是谁的？」
    如果和上一秒是同一个窗口，就继续累加时间；一旦换了窗口，就把上一段
    「从开始到此刻」的使用时长作为一条记录存起来。

为什么要判断「空闲」：
    你离开电脑去吃饭时，窗口并没有变，如果不管就会一直算成「在用电脑」。
    所以同时还要问 Windows「用户上次动键盘/鼠标是什么时候」，
    超过设定时间（默认 5 分钟）没动作，就认为人不在，停止计时。

线程模型：
    本模块跑在一个后台守护线程里（daemon thread），主线程跑 Qt 界面。
    两边通过数据库和内存缓冲区交互，写缓冲区时用锁保护。
"""
import time
import threading
import ctypes
import ctypes.wintypes as wintypes
from core.timeutil import beijing_from_timestamp

import win32gui
import win32process
import win32api
import win32con
import psutil

# ── 媒体活动检测（全屏 / 音频）─────────────────────────────────
# 看直播、看视频时人可能几分钟不动键鼠，但「在用电脑」是真的。
# 所以空闲判定除了看键鼠，还要看是不是在放东西：
#   * 前台窗口是真全屏（视频 / 直播 / 游戏）
#   * 系统正在出声音（放视频、听歌、看直播）
# 任一成立就当「在用」，不进空闲。
# pycaw 是可选依赖：没装时只用全屏检测，音频那条自动关闭，不报错。
try:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    from comtypes import CLSCTX_ALL
    _PYCAW_AVAILABLE = True
except Exception:
    AudioUtilities = None
    IAudioMeterInformation = None
    CLSCTX_ALL = None
    _PYCAW_AVAILABLE = False

from core.storage import (
    record_fragments_batch, get_ignored_apps, get_app_rules, get_setting,
    canonical_app_name,
)


# ── Idle 检测 ─────────────────────────────────────────────────
# 「Idle（空闲）」= 用户一段时间没碰键鼠。下面这段通过 Windows API 拿到
# 「距离上次输入过了多少秒」。

class LASTINPUTINFO(ctypes.Structure):
    """对应 Windows 的 LASTINPUTINFO 结构体。

    ctypes.Structure 的子类用于描述一段 C 语言结构体的内存布局，
    这样才能把它传给 Windows API 去填充数据。
    """
    _fields_ = [
        ("cbSize", ctypes.c_uint),   # 本结构体的大小，调用前必须填好
        ("dwTime", ctypes.c_uint),   # 最后一次输入时的系统 tick 计数
    ]


def _get_idle_seconds():
    """返回「距离用户上次操作键鼠已经过了多少秒」。

    GetLastInputInfo 给的是「上次输入那一刻的 tick 值」，GetTickCount 给的是
    「当前 tick 值」，两者相减就是空闲时长。tick 的单位是毫秒。
    """
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)  # API 要求先告诉它结构体多大
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        # byref 相当于传指针，让 API 把结果写进我们的 lii 对象里。
        # 调用失败（返回 0）时返回 0 秒，即「当作没有空闲」，宁可多记也不漏记。
        return 0.0
    ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD
    tick = ctypes.windll.kernel32.GetTickCount()
    return _idle_seconds_from_ticks(tick, lii.dwTime)


def _idle_seconds_from_ticks(current_tick, last_input_tick):
    """计算空闲秒数，并正确处理 DWORD 回绕。

    tick 是 32 位无符号整数，大约每 49.7 天会从最大值绕回 0。如果直接相减，
    回绕那一刻会得到一个巨大的负数。& 0xFFFFFFFF 把它重新解释成 32 位无符号数，
    相当于自动取模，绕回时也能算出正确的小间隔。
    """
    elapsed_ms = (int(current_tick) - int(last_input_tick)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def _active_time_cutoff(now, idle_seconds, idle_threshold):
    """返回当前应用最多可以计时到的墙钟时间。

    场景：你 13:00 坐下用电脑，13:30 离开，13:35 程序才发现你空闲了。
    如果直接把结束时间写成 13:35，就会白算 5 分钟。所以结束时间要「往回拨」：
        结束时间 = 现在 - 已空闲时长 + 阈值
    也就是只算到「你最后一次操作 + 阈值」那一刻为止，多出来的尾巴砍掉。

    返回的是时间戳（从 1970 年起的秒数）。如果本来就没超时，直接返回 now。
    """
    if idle_seconds <= idle_threshold:
        return now
    return now - idle_seconds + idle_threshold


def _is_foreground_fullscreen():
    """前台窗口是不是「真全屏」。

    真全屏 = 盖满它所在的那块屏幕，并且没有标题栏（WS_CAPTION）。
    最大化窗口不算：它还留着标题栏，看网页、读文档时窗口经常是
    最大化的，那种情况人走开也该算空闲。只有浏览器按 F11、视频播放器
    全屏、全屏游戏这种去掉标题栏的才算在用。
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        mleft, mtop, mright, mbottom = info["Monitor"]
        if left > mleft or top > mtop or right < mright or bottom < mbottom:
            return False
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        if style & win32con.WS_CAPTION:
            return False
        return True
    except Exception:
        return False


_audio_meter = None  # 缓存的输出设备峰值表，避免每秒都重新激活 COM 对象


def _is_audio_playing():
    """系统现在有没有声音在放。

    读默认输出设备的峰值（IAudioMeterInformation），峰值 > 0.001
    就认为在放。pycaw 没装、或取不到设备时返回 False，
    只是音频这条信号不启用，不影响全屏检测和原来的空闲逻辑。
    """
    if not _PYCAW_AVAILABLE:
        return False
    global _audio_meter
    try:
        if _audio_meter is None:
            speakers = AudioUtilities.GetSpeakers()
            iface = speakers.Activate(
                IAudioMeterInformation._iid_, CLSCTX_ALL, None
            )
            _audio_meter = iface.QueryInterface(IAudioMeterInformation)
        return _audio_meter.GetPeakValue() > 0.001
    except Exception:
        _audio_meter = None
        return False


def _is_media_active():
    """用户是不是在「看东西」：前台真全屏，或正在放声音。"""
    return _is_foreground_fullscreen() or _is_audio_playing()


def _media_aware_active_end(now, idle_seconds, idle_threshold, last_media_active):
    """算出「真实使用」这一段该截止到哪里。

    平时就是 _active_time_cutoff：最后一次操作 + 阈值。
    但如果中间一直在放视频 / 全屏，真实使用的结束时间应该是
    「最后一次检测到媒体活动」的时刻，不能往回砍到最后一次操作。
    否则看完 2 小时直播、视频一停，空闲段会从 2 小时前开始算，
    和已经记下的直播时间重叠。
    """
    end = _active_time_cutoff(now, idle_seconds, idle_threshold)
    if last_media_active is not None:
        end = max(end, last_media_active)
    return end


# ── 挂起（睡眠 / 休眠）检测 ───────────────────────────────────
# 合上笔记本盖子后系统会睡眠：这段时间监控线程被冻住，墙钟却在走。
# 偏偏空闲判定用的是 tick 计数（GetLastInputInfo），睡眠时 tick 不走，
# 所以醒来后 idle_secs 看起来很小，程序以为人一直在用电脑，
# 于是把整段睡眠时间都算到合盖前那个应用（常常是文件资源管理器）头上。
# 办法是看墙钟：如果两轮循环之间隔了远超 1 秒，就说明中间挂起过。
# 正常循环每 1 秒一轮，系统再卡也不会差 10 秒；真正的睡眠都是分钟起步。
_SUSPEND_GAP_SECONDS = 10.0


# ── 桌面窗口 ──────────────────────────────────────────────────
# Windows 桌面本身就是 explorer.exe 的一个窗口（类名 Progman / WorkerW）。
# 只开着桌面、什么窗口都没打开时，前台窗口就是它，之前会被误记成
# 「文件资源管理器」。真正的资源管理器窗口类名是 CabinetWClass /
# ExploreWClass，所以按类名区分：桌面不算任何应用。
_DESKTOP_CLASSES = {"progman", "workerw"}


def _is_desktop_window(hwnd):
    """这个前台窗口是不是桌面（而不是真正的资源管理器窗口）。"""
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        return False
    return bool(cls) and cls.lower() in _DESKTOP_CLASSES


# ── 忽略的系统进程 ────────────────────────────────────────────
# 这些是 Windows 的界面外壳进程，它们本身没有使用价值，但经常会短暂地
# 变成前台窗口（比如按开始菜单、切换输入法），会把真实使用的应用数据切碎。

_IGNORED = {
    "searchui.exe", "shellexperiencehost.exe", "openwith.exe",
    "textinputhost.exe", "lockapp.exe", "applicationframehost.exe",
    "systemsettings.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "runtimebroker.exe", "sihost.exe", "fontdrvhost.exe",
    "dwm.exe", "csrss.exe",
    "timetrace.exe", "pctime.exe", "usagetracker.exe", "usagetime.exe",  # 含旧版进程名，避免统计自己
}


# ── 监控主类 ──────────────────────────────────────────────────

class WindowMonitor:
    """前台窗口监控器。start() 后会在后台线程里持续轮询。"""

    def __init__(self, poll_interval=1.0):
        self.poll_interval = poll_interval  # 轮询间隔（秒），越小越精确也越费 CPU
        self._running = False  # 线程主循环的开关，置 False 后循环退出
        self._paused = False   # 用户手动暂停时为 True，暂停期间完全不采样
        self._thread = None

        # 下面这几个记录「当前正在计时的那一段」的信息
        self._last_hwnd = None   # 窗口句柄，用来判断是否切换了窗口
        self._last_app = None    # 应用名（已按合并规则归一化，如 weixin.exe）
        self._last_title = None  # 窗口标题
        self._last_exe = None    # 可执行文件完整路径
        self._last_start = None  # 这一段开始计时的时间戳

        self._was_idle = False   # 上一轮采样时是否处于空闲状态（用于检测状态切换）
        # 注意：self._idle_start 只在进入空闲时才赋值，所以这里不初始化
        self._last_media_active = None  # 上次检测到媒体活动（全屏/声音）的时间戳
        self._last_loop_wall = None  # 上一轮循环的墙钟时间，用于检测系统挂起

        # 内存缓冲区，定期 flush 到 DB
        # 每记一段就写一次数据库太频繁（每秒一次写盘），先攒在内存列表里，
        # 每 10 秒批量写一次，既减少磁盘 IO，万一崩了最多丢 10 秒数据。
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._flush_interval = 10.0  # 每 10 秒 flush 一次
        self._last_flush = time.time()
        self.activity_callback = None

    def _publish_activity(self, now=None):
        if self.activity_callback:
            self.activity_callback({"app_name": self._last_app,
                                    "is_idle": self._was_idle,
                                    "is_paused": self._paused or not self._running
                                                 or self._last_app in get_ignored_apps(),
                                    "observed_at": time.time() if now is None else now})

    @property
    def is_paused(self):
        """是否处于暂停状态。@property 让外部可以像读属性一样调用：monitor.is_paused"""
        return self._paused

    @property
    def idle_threshold(self):
        """空闲判定阈值（秒），从数据库设置里读取，默认 300 秒（5 分钟）。"""
        try:
            return int(get_setting("idle_threshold", "300"))
        except (ValueError, TypeError):
            # 设置里存了非法内容时不要崩溃，退回默认值
            return 300

    def start(self):
        """启动后台监控线程。"""
        self._running = True
        self._last_start = time.time()
        self._last_loop_wall = time.time()
        # daemon=True：主程序退出时这个线程会被自动结束，不会拖住程序不放
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止监控，并把当前还没记完的时间段补写进数据库。

        退出前必须把「正在计时但还没结束」的那一段收尾，否则最后几分钟会丢失。
        """
        self._running = False
        self._publish_activity()
        if self._thread:
            self._thread.join(timeout=5)  # 等线程真正结束，最多等 5 秒
        now = time.time()
        if self._was_idle:
            # 正处于空闲中：把空闲段收尾
            self._flush_idle(self._idle_start, now)
            self._was_idle = False
        else:
            # 正常使用中：把当前这一段收尾。
            # 用 cutoff 而不是 now，是为了不把「人已经离开但还没判定空闲」的
            # 那段时间也算进去。
            idle_secs = _get_idle_seconds()
            self._flush_current(
                _media_aware_active_end(
                    now, idle_secs, self.idle_threshold, self._last_media_active
                )
            )
        self._flush_to_db()

    def pause(self):
        """暂停统计（用户点「暂停统计」时调用）。

        暂停前要先把当前正在计时的一段结算掉，否则恢复后会把暂停期间的时间
        也算进去。
        """
        if not self._paused:
            now = time.time()
            if self._was_idle:
                self._flush_idle(self._idle_start, now)
                self._was_idle = False
            else:
                idle_secs = _get_idle_seconds()
                self._flush_current(
                    _media_aware_active_end(
                        now, idle_secs, self.idle_threshold,
                        self._last_media_active,
                    )
                )
            self._flush_to_db()
            self._paused = True
            self._publish_activity()

    def resume(self):
        """恢复统计。"""
        if self._paused:
            self._paused = False
            # 重置起点，让暂停期间的时间不落入任何一段记录
            self._last_start = time.time()
            # 同步更新挂起检测的基准，否则暂停这段时间会被误判成系统挂起
            self._last_loop_wall = time.time()
            self._publish_activity()

    # ── 前台窗口检测（GetForegroundWindow） ─────────────────────

    def _get_active_window_info(self):
        """用 GetForegroundWindow 获取前台窗口信息。

        返回 (窗口句柄, 应用名, 窗口标题, exe路径)。
        窗口不可用、没有标题、进程读不到、或属于被忽略的系统进程时，
        统一返回四个 None 表示「这一秒没什么可记的」。
        """
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            # 句柄为空，或窗口不可见（比如最小化到托盘的程序）都跳过
            return None, None, None, None

        if _is_desktop_window(hwnd):
            # 只开着桌面时前台就是桌面窗口，它属于 explorer.exe，
            # 但用户并没有在用文件资源管理器，不能记成它
            return None, None, None, None

        title = win32gui.GetWindowText(hwnd)
        if not title or len(title) < 2:
            # 标题太短的通常是临时/隐藏窗口，记下来没有意义
            return None, None, None, None

        try:
            # hwnd → 进程 ID → 进程信息（名称和 exe 路径）
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            name = proc.name().lower()  # 统一转小写，方便比较
            exe = proc.exe() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            # 进程刚退出、或是系统保护进程读不到信息，都当作无效
            return None, None, None, None

        if name in _IGNORED:
            return None, None, None, None

        # 应用合并规则
        # 例如 Chrome 会开一堆 chrome.exe 辅助进程，微信新版进程名从
        # wechat.exe 变成了 weixin.exe —— 这里把它们统一成一个「规范名」，
        # 统计时才不会一个软件被拆成好几行。
        rules = get_app_rules()
        name = canonical_app_name(name, rules, exe)

        return hwnd, name, title, exe

    # ── 缓冲区操作 ─────────────────────────────────────────────

    def _add_to_buffer(self, fragment):
        """把一条记录放进内存缓冲区。

        加锁是因为监控线程在写、其他线程可能也在操作，保证同一时刻只有
        一个线程在改这个列表。
        """
        with self._buffer_lock:
            self._buffer.append(fragment)

    def _flush_to_db(self):
        """将缓冲区中的片段写入数据库。"""
        with self._buffer_lock:
            if not self._buffer:
                return  # 没东西可写，直接返回，省一次数据库操作
            # 先整体取出再清空：真正写库的操作放在锁外面做，
            # 这样写数据库比较慢的时候不会一直占着锁。
            batch = self._buffer[:]
            self._buffer.clear()
        record_fragments_batch(batch)

    # ── Flush ─────────────────────────────────────────────────

    def _flush_current(self, end_time=None):
        """将当前正在计时的片段 flush 到缓冲区。

        参数 end_time 是这一段的结束时间戳。不传则用「现在」。
        """
        if self._last_app and self._last_start:
            end_time = time.time() if end_time is None else end_time
            duration = end_time - self._last_start
            # 太短的片段（比如 0.3 秒）多半是切窗口时的抖动，不值得记
            if duration >= 0.5:
                # 数据库里存 ISO 格式字符串（如 2026-09-22T13:05:00），
                # 好处是可读、且能直接按字符串排序比较
                start_iso = beijing_from_timestamp(self._last_start).isoformat()
                end_iso = beijing_from_timestamp(end_time).isoformat()
                ignored = get_ignored_apps()
                # 用户手动加入忽略名单的应用也跳过
                if self._last_app.lower() not in ignored:
                    self._add_to_buffer({
                        "app_name": self._last_app,
                        "window_title": self._last_title,
                        "exe_path": self._last_exe,
                        "start_time": start_iso,
                        "end_time": end_iso,
                        "duration": duration,
                        "is_idle": False,  # 这是「真实使用」的片段
                    })

    def _flush_idle(self, idle_start, idle_end):
        """把一段空闲时间记下来。

        空闲也记录是有用的：图表上能看出你什么时候离开过电脑。
        注意 is_idle=True，且不会计入「应用使用排行」（在 storage 里会区分处理）。
        """
        duration = idle_end - idle_start
        if duration >= 0.5:
            start_iso = beijing_from_timestamp(idle_start).isoformat()
            end_iso = beijing_from_timestamp(idle_end).isoformat()
            self._add_to_buffer({
                # 空闲时前台窗口可能还停在某个应用上，就记成那个应用；
                # 实在没有就用 "idle" 占位
                "app_name": self._last_app or "idle",
                "window_title": "idle",
                "exe_path": "",
                "start_time": start_iso,
                "end_time": end_iso,
                "duration": duration,
                "is_idle": True,
            })

    # ── 主循环 ────────────────────────────────────────────────

    def _loop(self):
        """监控线程的主体：一个每秒转一圈的死循环。"""
        while self._running:
            if self._paused:
                # 暂停期间什么也不做，只等待。
                # 注意没有更新 _last_start，所以恢复时会重新设置起点。
                time.sleep(self.poll_interval)
                continue

            now = time.time()

            # ── 挂起检测：合盖睡眠 / 休眠 ──
            # 如果两轮循环之间墙钟跳了远超 1 秒，说明系统中间挂起过。
            # 这时要把当前这一段截到挂起前最后一轮为止，中间这段记为空闲，
            # 否则整段睡眠时间都会被算成合盖前那个应用的使用时长。
            if self._last_loop_wall is not None:
                gap = now - self._last_loop_wall
                if gap > _SUSPEND_GAP_SECONDS:
                    if not self._was_idle:
                        self._flush_current(self._last_loop_wall)
                        self._idle_start = self._last_loop_wall
                        self._was_idle = True
                    # 重新起段：挂起这段时间不属于任何一段使用
                    self._last_start = now
                    self._last_hwnd = None
                    self._last_media_active = None
            self._last_loop_wall = now

            media_active = _is_media_active()
            if media_active:
                self._last_media_active = now
            idle_secs = _get_idle_seconds()
            idle_threshold = self.idle_threshold
            # 键鼠超时、但正在看东西（全屏 / 有声音）时，不算空闲
            is_idle = idle_secs > idle_threshold and not media_active

            if is_idle and not self._was_idle:
                # 刚进入 idle（上一秒还好好的，这一秒超时了）
                # 把当前应用这一段提前收尾，收尾时间用 cutoff 砍掉超出部分；
                # 如果中间一直在放媒体，收尾点取「最后一次媒体活动」，
                # 不能往回砍到最后一次操作，否则空闲段会和直播时间重叠
                active_end = _media_aware_active_end(
                    now, idle_secs, idle_threshold, self._last_media_active
                )
                self._flush_current(active_end)
                self._idle_start = active_end  # 空闲段从「最后一次操作/媒体活动」之后算起
                self._was_idle = True
                self._last_media_active = None

            elif not is_idle and self._was_idle:
                # 从 idle 恢复（用户回来了）
                self._flush_idle(self._idle_start, now)
                self._was_idle = False
                self._last_start = now  # 从此刻开始给新的一段计时
                self._last_hwnd = None  # 强制重新检测窗口
                self._last_media_active = None

            elif not is_idle:
                # 正常计时
                hwnd, app_name, title, exe = self._get_active_window_info()
                # 只在「窗口变了」的时候才结算上一段——同一个窗口持续用着
                # 就让它一直累加，最后一次写入即可
                if hwnd != self._last_hwnd:
                    self._flush_current()  # 先结算旧窗口那一段
                    self._last_hwnd = hwnd
                    self._last_app = app_name
                    self._last_title = title
                    self._last_exe = exe
                    self._last_start = time.time()  # 新一段的起点

            # 定期 flush 缓冲区到 DB（空闲期间不 flush）
            # 用 _last_flush 记录上次写盘时刻，攒够 10 秒再写一次。
            # 因为没有窗口切换时永远不会触发上面的 flush，定时兜底可以保证
            # 长时间停留在一个窗口时数据也能及时落盘。
            if not self._was_idle and now - self._last_flush >= self._flush_interval:
                self._flush_current()
                self._flush_to_db()
                self._last_start = time.time()  # 结算完，从此刻重新累计
                self._last_flush = now

            self._publish_activity(now)
            time.sleep(self.poll_interval)
