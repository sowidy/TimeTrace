"""三端统一时间：北京时间（Asia/Shanghai）。

统计、上传、处理所用的「今天」「现在」一律以北京时间为准，不再依赖电脑的
系统时区。系统时区一旦被改动（或换一台时区不同的电脑），按本地时间算出
的自然日就会和服务端 / 手机端错开，同一天的记录被拆到两天里，邮件和手机
上的数字就对不上。

实现说明：
- 北京时间是 UTC+8，且中国自 1991 年起不再实行夏令时，所以用固定 +8
  时区即可，不需要 tzdata，Windows 上也能直接用。
- 返回的是 naive 时间（不带 tzinfo），形状和原来的 datetime.now() /
  date.today() / datetime.fromtimestamp() 完全一致：库里已存的时间戳
  都是 naive 本地时间字符串，两边直接比较、拼接不会报 TypeError；
  只是「本地」的基准从系统时区改成了北京时间。
"""

from datetime import date, datetime, timedelta, timezone

# Asia/Shanghai：UTC+8，无夏令时。
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """当前北京时间（naive，与库里已存时间戳同一口径）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_today() -> date:
    """今天（北京时间的自然日）。"""
    return datetime.now(BEIJING_TZ).date()


def beijing_from_timestamp(ts: float) -> datetime:
    """Unix 时间戳 → 北京时间（naive，与库里已存时间戳同一口径）。"""
    return datetime.fromtimestamp(ts, tz=BEIJING_TZ).replace(tzinfo=None)
