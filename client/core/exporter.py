"""定时导出：把使用时间自动写成 CSV，按「年 / 月 / 日」分层存到 data 目录下。

在整个项目中的位置：
    本模块只依赖 storage.py（取数据）和 paths.py（决定存哪），
    刻意不导入 PySide6 —— 因为导出是在后台线程里跑的，而且可能在界面还没
    创建出来之前就先跑了一次。不碰 Qt 就不会有线程安全和启动顺序的麻烦。

目录结构（以 2026-10-14 为例）：
    data/2026/10/14/daily_2026-10-14.csv              每日：当天各应用汇总
    data/2026/10/12/weekly_2026-10-12_2026-10-18.csv  每周：整周各应用汇总
    data/2026/10/01/monthly_2026-10.csv               每月：整月各应用汇总

    规则是统一的：每个周期都放进一个「年/月/日」文件夹，文件夹名取该周期
    的**起始日**（每日就是当天、每周是周一、每月是 1 号）。

关于「重复导出」：
    文件名由周期本身决定，不含运行时间。所以同一个周期重复导出只会覆盖
    同一个文件，不会越导越多，天然是幂等的 —— 不需要额外记录「导过没有」。
"""

import csv
import os
import threading
import time
from datetime import date, timedelta
from core.timeutil import beijing_today

from core.storage import (
    format_duration,
    get_available_dates,
    get_setting,
    get_summary_range,
    set_setting,
)
from core.paths import data_dir

# 用哪个设置键记录「上次导出到哪一天」，用来决定下次要回填哪些日期
_WATERMARK_KEY = "export_watermark"
# 是否开启自动导出
_ENABLED_KEY = "export_enabled"

# 后台线程默认多久检查一次（秒）。导出本身很便宜，但也没必要太频繁，
# 半小时一次足够保证「当天数据基本是最新的」。
DEFAULT_INTERVAL = 1800


# ── 写 CSV ────────────────────────────────────────────────────

def write_summary_csv(path, rows):
    """把每日汇总行写进 CSV 文件。

    参数：
        path：目标文件路径（所在目录必须已存在）。
        rows：storage.get_summary_range() 的返回值，每项含 date / app_name /
              display_name / total_seconds / session_count。

    两个容易踩坑的细节：
        encoding="utf-8-sig" 会在文件开头写一个 BOM 标记，用来告诉 Excel
        「这是 UTF-8」，否则用 Excel 打开时中文会乱码。
        newline="" 是 Python 的 csv 模块在 Windows 上的标准要求，
        不加的话每写一行会多出一个空行。
    """
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        # 表头。列含义见 storage.get_summary_range 的返回说明
        writer.writerow(["日期", "应用名", "显示名", "使用时长(秒)", "使用时长", "使用次数"])
        for r in rows:
            writer.writerow([
                r["date"],
                r["app_name"],
                r["display_name"],
                round(r["total_seconds"], 1),
                format_duration(r["total_seconds"]),
                r["session_count"],
            ])


# ── 目录与文件名 ──────────────────────────────────────────────

def period_dir(anchor):
    """返回某个周期对应的目录路径，并确保它已存在。

    参数：
        anchor：该周期的起始日（datetime.date）。
    返回：形如 <data>/2026/10/14 的完整路径。

    说明：月、日都补足两位（10 而不是 10…其实 10 本来就是两位，
    这里主要保证 1 月写成 01），这样按文件名排序时顺序和实际时间一致。
    """
    d = os.path.join(data_dir(), f"{anchor.year:04d}", f"{anchor.month:02d}",
                     f"{anchor.day:02d}")
    os.makedirs(d, exist_ok=True)
    return d


def week_start_of(day):
    """返回 day 所在那一周的周一。

    datetime.weekday() 的约定是「周一 = 0」，所以减去它得到的就是周一。
    整个项目（包括界面上的「周一/周二/…」标签）都按「周一是一周第一天」
    处理，保持一致。
    """
    return day - timedelta(days=day.weekday())


# ── 三种粒度的导出 ────────────────────────────────────────────

def export_daily(day):
    """导出某一天的各应用使用汇总。

    参数：
        day：datetime.date。
    返回：写出的文件路径；当天没有数据时返回 None（不写空文件）。
    """
    rows = get_summary_range(day.isoformat(), day.isoformat())
    if not rows:
        return None
    path = os.path.join(period_dir(day), f"daily_{day.isoformat()}.csv")
    write_summary_csv(path, rows)
    return path


def export_weekly(week_start):
    """导出某一整周（周一到周日）的各应用使用汇总。

    参数：
        week_start：该周的周一（datetime.date）。
    """
    week_end = week_start + timedelta(days=6)
    rows = get_summary_range(week_start.isoformat(), week_end.isoformat())
    if not rows:
        return None
    name = f"weekly_{week_start.isoformat()}_{week_end.isoformat()}.csv"
    path = os.path.join(period_dir(week_start), name)
    write_summary_csv(path, rows)
    return path


def export_monthly(year, month):
    """导出某一个自然月（1 号到月底）的各应用使用汇总。

    参数：
        year：年份，如 2026。
        month：月份 1~12。
    """
    first = date(year, month, 1)
    # 下个月 1 号往前退一天 = 本月最后一天。这样不用管闰年和大小月。
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = next_month - timedelta(days=1)

    rows = get_summary_range(first.isoformat(), last.isoformat())
    if not rows:
        return None
    path = os.path.join(period_dir(first), f"monthly_{year:04d}-{month:02d}.csv")
    write_summary_csv(path, rows)
    return path


# ── 自动导出：决定「这次该导哪些」 ────────────────────────────

def _weeks_spanning(start, end):
    """列出 [start, end] 这段时间覆盖到的所有周一（用于补导漏掉的周）。"""
    cur = week_start_of(start)
    out = []
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def _months_spanning(start, end):
    """列出 [start, end] 覆盖到的所有 (年, 月)。"""
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    out = []
    while cur <= last:
        out.append((cur.year, cur.month))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return out


def export_pending(today=None):
    """把「还没有导出过」的数据补齐导出，返回本次写出的文件路径列表。

    为什么需要这个函数（而不是简单地「每天导今天」）：
        这个程序不一定会一直在后台跑，用户可能关了一周再打开。如果只导
        「今天」，中间那几天就永远没有对应的 CSV 了。所以这里用一个
        「水位线」记录上次导出到哪一天，每次从水位线补到今天就补齐了。

    具体做法：
        1. 读水位线（存在 settings 表里）。第一次运行没有水位线，就从
           最早有数据的那天开始，相当于把历史数据一次性补出来。
        2. 导出水位线 ~ 今天之间每一个有数据的日期（每日）。
        3. 导出这段时间覆盖到的每一周、每一个月。
        4. 把水位线推到今天。

    幂等性：文件名只由周期决定，所以重复调用只会覆盖同样的文件。
    因此「今天」的文件每次都会被刷新成最新状态，不用担心今天是半截数据。

    参数：
        today：覆盖「今天」是哪天，默认取北京时间当天。传参主要是为了方便测试。
    返回：写出的文件路径列表（没有数据的周期会被跳过）。
    """
    today = today or beijing_today()
    available = get_available_dates()   # ISO 字符串，从新到旧
    if not available:
        return []

    have_days = sorted(date.fromisoformat(d) for d in available)
    oldest = have_days[0]

    watermark = get_setting(_WATERMARK_KEY)
    try:
        start = date.fromisoformat(watermark) if watermark else oldest
    except (TypeError, ValueError):
        # 水位线被写坏了（比如手工改过数据库），退回从最早的数据开始补
        start = oldest

    written = []

    # ① 每日：补导水位线到今天的每一天
    for d in have_days:
        if start <= d <= today:
            p = export_daily(d)
            if p:
                written.append(p)

    # ② 每周 / ③ 每月：覆盖 [start, today] 跨到的所有周期。
    #    这里传的是日期区间而不是「有数据的天」，所以那些整周都没用电脑的
    #    周期也会被检查一遍（没数据自然就不写文件）。
    for ws in _weeks_spanning(start, today):
        p = export_weekly(ws)
        if p:
            written.append(p)
    for (y, m) in _months_spanning(start, today):
        p = export_monthly(y, m)
        if p:
            written.append(p)

    set_setting(_WATERMARK_KEY, today.isoformat())
    return written


def export_all():
    """不管水位线，把库里所有有数据的日期全导一遍（并在界面里手动触发时用）。

    返回：写出的文件路径列表。
    """
    available = get_available_dates()
    if not available:
        return []
    days = sorted(date.fromisoformat(d) for d in available)
    written = []
    for d in days:
        p = export_daily(d)
        if p:
            written.append(p)
    for ws in _weeks_spanning(days[0], days[-1]):
        p = export_weekly(ws)
        if p:
            written.append(p)
    for (y, m) in _months_spanning(days[0], days[-1]):
        p = export_monthly(y, m)
        if p:
            written.append(p)
    return written


# ── 后台定时器 ────────────────────────────────────────────────

class ExportScheduler:
    """在后台线程里定期执行导出的小调度器。

    用法和 monitor.py 里的 WindowMonitor 完全一样：
        sched = ExportScheduler()
        sched.start()   # 启动（内部开一个守护线程，不阻塞主线程）
        ...
        sched.stop()

    为什么用后台线程而不是 QTimer：
        导出要查数据库，数据多的时候（比如首次把所有历史补出来）会花上
        一两秒。放在界面线程里会卡住界面，所以丢到后台去跑。
    """

    def __init__(self, interval=DEFAULT_INTERVAL):
        """创建调度器（此时还没有任何线程）。

        参数：
            interval：两次检查之间隔多少秒，默认半小时。
        """
        self.interval = interval
        self._running = False
        self._thread = None
        self.last_error = None    # 最近一次失败的原因，供界面显示
        self.last_written = []    # 最近一次写出的文件，供界面显示

    def start(self):
        """启动后台线程。重复调用不会有副作用。"""
        if self._running:
            return
        self._running = True
        # daemon=True：主程序退出时这个线程会被自动结束，不会拖住程序不放
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止后台线程并等它退出。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)   # 最多等 5 秒，避免卡住退出流程
            self._thread = None

    def _loop(self):
        """线程主循环：先立刻跑一次，然后每隔 interval 秒跑一次。"""
        while self._running:
            self.run_once()
            # 把长的等待切成小段，这样 stop() 能较快生效，
            # 不用等满一整个 interval
            slept = 0.0
            while self._running and slept < self.interval:
                time.sleep(1.0)
                slept += 1.0

    def run_once(self):
        """执行一次导出。任何异常都被吞掉并记在 last_error 里。

        为什么不让异常抛出去：这是后台线程，异常会直接把线程干掉，之后
        就再也不会导出了，而且用户完全看不到。所以这里兜住，记下来即可。
        """
        if get_setting(_ENABLED_KEY, "1") != "1":
            return []
        try:
            written = export_pending()
            from core import site_storage
            written += site_storage.export_pending()
            self.last_written = written
            self.last_error = None
            return written
        except Exception as e:   # noqa: BLE001 —— 后台线程必须兜住所有异常
            self.last_error = str(e)
            return []
