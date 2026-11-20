"""进程内的每日定时器：次日 00:03 发送前一天的完整报告。

只看这一个文件时，记住三条：
    * 「下一次几点」是一个**时刻**（带日期），不是「还有多少秒」——
      这样睡过头时，发的还是该触发时刻对应的报告（见包 docstring 的 ②）；
    * 触发日不是报告日：某天 00:03 触发，发送的是前一天；
    * 异常必须被吃掉，绝不能逃出线程（见 DailyMailScheduler._guarded_run）。

这里**不区分日报和周报** —— 它只管「到点跑一轮」，跑哪一天、那一天该发哪种
报告，都是 mail/sender.py 按报告日期算出来的。所以定时点是每天 00:03，一个
自然日结束后留出 3 分钟完成最后的数据上报，再发送完整报告。
"""
import logging
import threading
from datetime import datetime, time, timedelta

import config
from mail.sender import run

log = logging.getLogger("timetrace.mail.scheduler")

DEFAULT_SEND_TIME = time(0, 3)


# ── 定时点解析 ────────────────────────────────────────────────

def parse_send_time(raw):
    """把 TIMETRACE_MAIL_TIME 解析成 datetime.time，解析不了就用默认值。

    这里不抛异常：配置写错了就不该让整个服务起不来 —— 大不了按默认时间发，
    日志里有告警，比服务直接崩掉好。
    """
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).time()
        except ValueError:
            continue
    log.warning("TIMETRACE_MAIL_TIME=%r 解析不了，改用默认的 00:03:00", raw)
    return DEFAULT_SEND_TIME


SEND_TIME = parse_send_time(config.MAIL_SEND_TIME)


# ── 定时器 ────────────────────────────────────────────────────

class DailyMailScheduler:
    """进程内的每日定时器。跑在一个 daemon 线程里。

    线程是 daemon：服务端退出时不指望它自己收尾，别为了等它而卡住关机。
    """

    def __init__(self, send_time=None, catchup_days=None, runner=None):
        self.send_time = send_time or SEND_TIME
        self.catchup_days = (config.MAIL_CATCHUP_DAYS if catchup_days is None else catchup_days)
        # runner 只是为了测试能塞一个假的进来，生产里就是上面的 run()
        self._runner = runner or run
        self._thread = None
        self._stop = threading.Event()
        self.last_result = None

    @staticmethod
    def report_day_for_fire(fire):
        """返回一次触发应发送的报告日。

        邮件在自然日结束后的次日发送，所以 10 月 22 日 00:03 的触发对应
        10 月 21 日。这个映射必须只依赖计划触发时刻，不能依赖实际醒来时刻。
        """
        return fire.date() - timedelta(days=1)

    def latest_due_day(self, now=None):
        """返回在 ``now`` 时刻已经到达发送期限的最近报告日。

        00:03 前，昨天仍处在等待末尾数据上报的缓冲期，最近到期的是前天；
        00:03 起，昨天才进入可发送状态。启动补发使用这个边界，避免服务在
        00:01 重启时抢先发出一份尚未收齐的报告。
        """
        now = now or datetime.now()
        fire_date = now.date()
        if now.time() < self.send_time:
            fire_date -= timedelta(days=1)
        return fire_date - timedelta(days=1)

    def next_fire(self, now=None):
        """下一次该发的**时刻**（不是「还有多少秒」）。

        时刻里带着「属于哪一天」的信息，这正是修复「睡过头跨过午夜」
        那个坑的关键：睡醒之后要发哪一天由它决定，不看现在几点。
        """
        now = now or datetime.now()
        fire = datetime.combine(now.date(), self.send_time)
        if fire <= now:
            fire += timedelta(days=1)
        return fire

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="timetrace-mail-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # 大概率是卡在某次 SMTP 上（有 timeout= 兜着，总会回来）。
                # 不强杀线程 —— Python 里没有安全的杀线程手段。
                log.warning("邮件定时器没能在 %.0f 秒内退出，先不管它", timeout)
            self._thread = None

    def _loop(self):
        # 开机先补一次：只从「发送期限已经到达」的最近报告日开始。
        # 例如 00:01 重启时昨天还在 3 分钟缓冲期，不能抢先发送；00:03 后
        # 才能补昨天。已经发送过的日期会由 mail_log 幂等跳过。
        if self.catchup_days > 0:
            self._guarded_run(day=self.latest_due_day(),
                              catchup_days=self.catchup_days - 1,
                              note="启动补发")

        while not self._stop.is_set():
            now = datetime.now()
            fire = self.next_fire(now)
            wait = (fire - now).total_seconds()
            log.info("下一次邮件：%s（%.0f 秒后）",
                     fire.isoformat(sep=" ", timespec="seconds"), wait)
            if self._stop.wait(max(0.0, wait)):
                return                       # 收到停止信号
            # 用计划触发时刻推导报告日，而不是看醒来时的 date.today()：
            # 即使 00:03 的唤醒延迟到 00:05，也仍发送昨天。
            self._guarded_run(day=self.report_day_for_fire(fire), note="定时")

    def _guarded_run(self, day, catchup_days=None, note=""):
        """跑一轮，并且保证异常不逃出去。

        定时器线程里逃出去的异常会静默地杀掉这个线程，之后每天都不发邮件，
        而且没有任何迹象 —— 这是最难查的一类故障。
        """
        try:
            result = self._runner(
                day=day,
                catchup_days=(self.catchup_days if catchup_days is None else catchup_days),
            )
        except Exception as e:                   # noqa: BLE001
            log.exception("%s：报告任务异常：%s", note, e)
            return None
        self.last_result = result
        log.info("%s完成：day=%s 成功=%d 失败=%d",
                 note, result.get("day"), result.get("sent", 0),
                 result.get("failed", 0))
        return result


# 进程内只允许有一个定时器。lifespan 每次启动调一次 start_scheduler()，
# 重复调用不该起出第二个线程。
_scheduler = None
_scheduler_lock = threading.Lock()


def start_scheduler():
    """按配置启动进程内定时器。没启用就返回 None，调用方不用管分支。"""
    global _scheduler

    if not config.MAIL_ENABLED:
        log.info("每日邮件未启用（没配 TIMETRACE_SMTP_HOST），跳过定时器")
        return None
    if not config.MAIL_SCHEDULER_ENABLED:
        log.info("进程内定时器已关闭，请用外部 cron 调POST /api/v1/admin/daily-mail")
        return None

    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = DailyMailScheduler()
            _scheduler.start()
            log.info("每日邮件定时器已启动，每天 %s 发送",
                     _scheduler.send_time.strftime("%H:%M:%S"))
        return _scheduler
