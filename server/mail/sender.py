"""发信：一封（SMTP）、一天（一个用户）、一轮（所有订阅用户）。

三层是套着的：run() 按天按人循环调 send_day()，send_day() 渲染完调
send_mail()。同模块内的这两次调用写**裸名**（`send_day(...)` / `send_mail(...)`），
测试是靠 patch 本模块的同名属性来换实现的，写成别的引用就 patch 不到了。

「一天」那一层发的是日报还是周报，由那一天是星期几决定（is_weekly_day，
配置项 config.MAIL_WEEKLY_DAY）。默认周日发周报 —— 覆盖「以该天结尾的 7 天」
的聚合；其余六天各发当天的日报。一周里每天恒定一封，所以 mail_log 的
UNIQUE KEY (user_key, day) 不需要动，详见 mail/__init__.py。
"""
import logging
import base64
# smtplib 必须是模块级 import（而不是 from smtplib import SMTP_SSL）：
# 测试是拿 mock.patch.object(sender.smtplib, "SMTP_SSL", ...) 顶掉它的，
# 这就要求本模块里存在一个叫 smtplib 的属性。
import smtplib
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import config
from db import mail_log, usage_records, users
from mail import merge as mail_merge
from mail import report

log = logging.getLogger("pctime.mail.sender")


def send_mail(to, subject, text, html_body):
    """通过 SMTP 发一封信。失败直接抛异常，由调用方决定怎么记录。

    multipart/alternative + 纯文本在前：邮件客户端能显示 HTML 就用 HTML，
    不能就用纯文本。先纯文本是约定 —— 越靠后的部分越「高级」，客户端
    从后往前挑自己认识的。
    """
    msg = MIMEMultipart("alternative")
    # 主题里有中文，必须显式编码，否则会出现乱码或直接被拒
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(config.MAIL_FROM_NAME, "utf-8")),config.MAIL_FROM))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    domain = config.MAIL_FROM.split("@")[-1] if "@" in config.MAIL_FROM else None
    msg["Message-ID"] = make_msgid(domain=domain)

    msg.attach(MIMEText(text, "plain", "utf-8"))
    # Data URIs are convenient for local previews, but poorly supported in email.
    # Convert only generated PNGs to inline CID attachments; no network downloads.
    from mail.charts import IMAGE_DATA
    images = []
    def attach_image(match):
        cid = make_msgid(domain=domain)[1:-1]
        image = MIMEImage(base64.b64decode(match.group(1), validate=True), _subtype="png")
        image.add_header("Content-ID", f"<{cid}>")
        image.add_header("Content-Disposition", "inline", filename=f"chart-{len(images)+1}.png")
        images.append(image)
        return "cid:" + cid
    html_body = IMAGE_DATA.sub(attach_image, html_body)
    if images:
        related = MIMEMultipart("related")
        related.attach(MIMEText(html_body, "html", "utf-8"))
        for image in images:
            related.attach(image)
        msg.attach(related)
    else:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    if config.SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,timeout=config.SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,timeout=config.SMTP_TIMEOUT)

    # with 退出时会 quit()，异常路径下也会尽量把连接关掉，不留半开连接
    with server:
        if not config.SMTP_USE_SSL:
            server.ehlo()
            server.starttls()
            # STARTTLS 之后必须再 ehlo 一次：加密层建立后，服务器宣告的
            # 能力（尤其 AUTH 支持哪些方式）可能变了，用的是握手前那份
            # 旧信息的话，login 会以奇怪的方式失败。
            server.ehlo()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


def is_weekly_day(day):
    """这一天默认该发周报吗。大小写/编号都跟着 date.weekday()：0=周一 … 6=周日。

    这只是**默认**：日报和周报是两种模板，调用方可以显式指定 weekly
    覆盖它（见 send_day / send_merged_day 的 weekly 参数），任何一天都
    可以发日报、也可以发周报。定时器不传 weekly，所以仍按星期几走
    （周日发周报）；手工触发时传 weekly 就能指定种类。
    mail_log 按 (user_key, day, weekly) 去重，同一天日报周报可以各发一封。

    刻意留成模块级的裸名函数（而不是把比较内联进 send_day），这样测试可以
    mock.patch.object(sender, "is_weekly_day") 直接指定种类，不必真的等到某个周日。
    """
    return day.weekday() == config.MAIL_WEEKLY_DAY


def send_day(user_key, day, email=None, dry_run=False, now=None, weekly=None):
    """给一个用户发某一天的报告。

    发的是哪一种，由 weekly 参数决定：传 True 发周报（覆盖「以这一天结尾的
    7 天」的聚合），传 False 发当天日报，不传则按星期几默认（见 is_weekly_day，
    周报日发周报）。除了「取哪份数据」「用哪个渲染」这两步，别的检查两种报告
    完全一样 —— 跳过和失败的规则不是各写一遍。

    返回一个 dict，不抛异常 —— 一个地址发失败不该让整轮发送中断。
        {"status": "sent"/"skipped"/"failed", "reason": ..., "day": ...,
         "user_key": ..., "email": ..., "subject": ..., "weekly": bool}

    跳过（skipped）的几种情况，reason 分别是：
        already_sent  这天这种报告已经成功发过了（看 mail_log）
        no_email      这个用户没有可用邮箱
        no_data       服务端没有这天的记录 —— 这天机器没开机，或者没上传；
                      周报则是整个区间一行记录都没有
        dry_run       只渲染不发送
    """
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    weekly = is_weekly_day(day) if weekly is None else bool(weekly)
    out = {"user_key": user_key, "day": day.isoformat(),"email": email or "", "subject": "", "weekly": weekly}

    if mail_log.was_mail_sent(user_key, day, weekly=weekly):
        return {**out, "status": "skipped", "reason": "already_sent"}
    if not email:
        return {**out, "status": "skipped", "reason": "no_email"}

    if weekly:
        # 周报的数据源是**区间聚合**，不是某一天。start/end 由 report.week_range
        # 算，这里只是照着用 —— 「一周是哪 7 天」只该有一个定义处。
        start, end = report.week_range(day)
        summary = usage_records.get_range_summary(user_key, start, end)
    else:
        summary = usage_records.get_daily_summary(user_key, day)
    site_rows, site_hours, site_intervals, app_intervals, timeline_rows, readiness = [], [], [], [], [], []
    if config.MAIL_TRACKING_ENABLED:
        from db import tracking
        range_start, range_end = report.week_range(day) if weekly else (day, day)
        unavailable = set()
        for kind, rows in (("web", site_rows), ("web_hourly", site_hours),
                           ("web_intervals", site_intervals),
                           ("app_intervals", app_intervals), ("timeline", timeline_rows)):
            if weekly and kind == "web_intervals":
                continue
            if weekly and kind == "app_intervals":
                continue
            if not weekly and kind == "timeline":
                continue
            try:
                rows.extend(tracking.read_range(kind, user_key, range_start, range_end))
            except Exception:
                unavailable.add(kind)
                log.warning("报告 %s 数据不可用，继续生成其他报告内容", kind, exc_info=True)
        try:
            readiness = tracking.readiness(user_key, range_start, range_end)
            readiness = [r for r in readiness if r["kind"] not in unavailable]
            if site_rows and not any(
                    str(r["day"]) == str(day) and r["kind"] == "apps" and r["complete"] for r in readiness):
                log.warning("报告应用数据尚未确认日终完成 user=%s day=%s", user_key, day)
        except Exception:
            log.warning("附加报告数据不可用，继续生成原应用报告", exc_info=True)
    app_summary_missing = summary is None
    if summary is None and site_rows:
        # Never use website durations as the device total.
        summary = {"total_seconds": 0, "app_count": 0, "apps": [], "_app_summary_missing": True}
    if summary is None:
        return {**out, "status": "skipped", "reason": "no_data"}

    user = users.get_user(user_key)
    # Rendering-only fields, no change to upload/API contracts.
    summary = {**summary, "_readiness": readiness}
    kind = mail_merge.device_kind((user or {}).get("platform"))
    summary["_app_sources"] = {a["app_name"]: kind for a in summary.get("apps") or []}
    summary["_site_sources"] = {str(r["host"]): kind for r in site_rows}
    if app_summary_missing:
        subject, text, html_body = report.build_missing_app_message(day, user, weekly)
    elif weekly:
        subject, text, html_body = report.build_weekly_message(day, summary, user)
    else:
        subject, text, html_body = report.build_message(day, summary, user)
    if config.MAIL_TRACKING_ENABLED:
        from mail.tracking_report import append_sections
        try:
            subject, text, html_body = append_sections(
                (subject, text, html_body), summary, timeline_rows, site_rows,
                range_start, range_end, readiness, site_hours=site_hours,
                site_intervals=site_intervals, app_intervals=app_intervals)
        except Exception:
            log.warning("附加报告渲染失败，继续发送应用报告", exc_info=True)
    out["subject"] = subject

    if dry_run:
        return {**out, "status": "skipped", "reason": "dry_run"}

    try:
        send_mail(email, subject, text, html_body)
    except Exception as e:                       # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        # 失败也留痕：用户来问「怎么没收到」时，这张表就是答案
        try:
            mail_log.record_mail(user_key, day, email, mail_log.MAIL_FAILED,detail, now=now, weekly=weekly)
        except Exception as log_err:             # noqa: BLE001
            log.warning("连失败记录都写不进去：%s", log_err)
        log.warning("%s发送失败 user=%s day=%s to=%s：%s","周报" if weekly else "日报", user_key, day, email, detail)
        return {**out, "status": "failed", "reason": detail}

    try:
        mail_log.record_mail(user_key, day, email, mail_log.MAIL_SENT, subject,now=now, weekly=weekly)
    except Exception as e:                       # noqa: BLE001
        # 信已经发出去了，记录没写上。最坏结果是下次运行重发一封，
        # 比「明明发了却因为写记录失败而报错」要好。
        log.warning("邮件已发出但记录失败 user=%s day=%s：%s", user_key, day, e)

    log.info("%s已发送 user=%s day=%s to=%s",
             "周报" if weekly else "日报", user_key, day, email)
    return {**out, "status": "sent", "reason": ""}


def send_merged_day(devices, day, email, dry_run=False, now=None, weekly=None):
    """Send one merged report for all devices sharing one mailbox.

    ``devices`` is a group from mail.merge.group_devices_by_email, earliest
    created first. One mail per mailbox per day: mail_log is keyed by the
    earliest-created device (the anchor), with the merged device count in
    the detail. Single-device groups never reach this function; they keep
    going through send_day() with byte-identical behaviour.

    Returns the same outcome dict shape as send_day(), plus
    ``merged_devices``. Never raises: a failed address must not stop the
    rest of the round.
    """
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    weekly = is_weekly_day(day) if weekly is None else bool(weekly)
    anchor = devices[0]
    anchor_key = anchor["user_key"]
    email = mail_merge.normalize_email(email)
    out = {"user_key": anchor_key, "day": day.isoformat(), "email": email,
           "subject": "", "weekly": weekly, "merged_devices": len(devices)}

    if mail_log.was_mail_sent(anchor_key, day, weekly=weekly):
        return {**out, "status": "skipped", "reason": "already_sent"}
    if not email:
        return {**out, "status": "skipped", "reason": "no_email"}

    range_start, range_end = report.week_range(day) if weekly else (day, day)
    range_days = {str(range_start + timedelta(days=i))
                  for i in range((range_end - range_start).days + 1)}
    named_summaries = []
    site_rows, site_hours, site_intervals = [], [], []
    site_rows_by_device = []
    app_intervals, timeline_rows, readiness = [], [], []
    # Per-device app intervals, kept per device (not flattened) so the merged
    # mail can collapse the same physical device registered twice.
    app_intervals_by_device = []
    for device in devices:
        key = device["user_key"]
        if weekly:
            summary = usage_records.get_range_summary(key, range_start, range_end)
        else:
            summary = usage_records.get_daily_summary(key, day)
        named_summaries.append((mail_merge.device_label(device), summary))
        device_intervals = []
        web_rows = []
        if config.MAIL_TRACKING_ENABLED:
            from db import tracking
            try:
                web_rows = tracking.read_range("web", key, range_start, range_end)
                site_rows.extend(web_rows)
                site_hours.extend(tracking.read_range("web_hourly", key, range_start, range_end))
                site_intervals.extend(tracking.read_range("web_intervals", key, range_start, range_end))
                # Intervals are fetched even for weekly mail: the weekly
                # charts do not draw them, but overlap needs exact spans.
                device_intervals = tracking.read_range("app_intervals", key, range_start, range_end)
                app_intervals.extend(device_intervals)
                timeline_rows.extend(tracking.read_range("timeline", key, range_start, range_end))
                readiness.extend(tracking.readiness(key, range_start, range_end))
            except Exception:
                log.warning("合并报告 %s 数据不可用，继续生成其他报告内容", key, exc_info=True)
        site_rows_by_device.append(web_rows)
        app_intervals_by_device.append(device_intervals)
    merged = mail_merge.merge_app_summaries(named_summaries)
    app_summary_missing = merged is None
    if merged is None and site_rows:
        merged = {"total_seconds": 0, "app_count": 0, "apps": [],
                  "_app_summary_missing": True,
                  "_devices": [{"label": mail_merge.device_label(d),
                                "total_seconds": 0, "app_count": 0} for d in devices]}
    if merged is None:
        return {**out, "status": "skipped", "reason": "no_data"}

    # Exact spans first: union intervals per app (for the Gantt) and union
    # bins (for the heatmap / hourly stacking). Overlap is the seconds two
    # or more devices were simultaneously in *any* app; bins alone cannot
    # prove same-second overlap, so without intervals there is nothing to
    # subtract and the merged total is just the plain sum.
    union_intervals = mail_merge.union_intervals_by_app(app_intervals)
    union_bins = mail_merge.union_bins_from_intervals(union_intervals)
    # Per-app same-app overlap: the daily Gantt draws union spans while the
    # listed per-app totals sum both devices, so the chart's completeness
    # check must allow exactly this shortfall or merged mail loses its chart.
    merged["_app_overlap_seconds"] = mail_merge.per_app_overlap_seconds(app_intervals)
    # Which device each app / site ran on: 'P' phone, 'C' computer, 'M' both.
    # The Gantt charts mark every row and legend entry with it.
    merged["_app_sources"] = mail_merge.sources_by_key(
        (mail_merge.device_kind(d.get("platform")),
         {r.get("app_name") for r in rows if r.get("app_name")})
        for d, rows in zip(devices, app_intervals_by_device))
    merged["_site_sources"] = mail_merge.sources_by_key(
        (mail_merge.device_kind(d.get("platform")),
         {str(r.get("host")) for r in rows if r.get("host")})
        for d, rows in zip(devices, site_rows_by_device))
    if union_bins:
        timeline_rows = union_bins
        app_intervals = union_intervals
        merged["daily_apps"] = mail_merge.daily_apps_from_bins(union_bins)
        merged["days_with_data"] = len({str(r["day"]) for r in union_bins})
    overlap = 0.0
    # Same physical device, registered twice, is one label: its own duplicate
    # span is not overlap, and a device with no data that day drops out.
    # Overlap is measured only across different labels, per day.
    merged["_devices"], label_day_spans = mail_merge.dedupe_devices_by_label(
        merged["_devices"], app_intervals_by_device, devices)
    # The count the mail reports is physical devices, not user_key rows: a
    # phone reinstalled three times is still one phone. The deduped list
    # above is the authority; the raw len(devices) in `out` was set before
    # the dedup ran.
    out["merged_devices"] = len(merged["_devices"])
    for day_key in range_days:
        overlap += mail_merge.compute_overlap_seconds(
            [spans.get(day_key, []) for spans in label_day_spans])
    merged["_overlap_seconds"] = round(overlap, 1)
    merged["total_seconds"] = round(
        sum(float(d["total_seconds"]) for d in merged["_devices"]) - overlap, 1)
    # Charts use per-app union bins (real spans, see mail.merge); keep the
    # stacked-chart input on the same basis so it validates against them.
    merged["_readiness"] = readiness

    user = {"user_key": anchor_key, "email": email,
            "hostname": anchor.get("hostname") or "", "platform": anchor.get("platform") or ""}
    if app_summary_missing:
        subject, text, html_body = report.build_missing_app_message(day, user, weekly)
    elif weekly:
        subject, text, html_body = report.build_weekly_message(day, merged, user)
    else:
        subject, text, html_body = report.build_message(day, merged, user)
    if config.MAIL_TRACKING_ENABLED:
        from mail.tracking_report import append_sections
        try:
            subject, text, html_body = append_sections(
                (subject, text, html_body), merged, timeline_rows, site_rows,
                range_start, range_end, readiness, site_hours=site_hours,
                site_intervals=site_intervals, app_intervals=app_intervals)
        except Exception:
            log.warning("附加报告渲染失败，继续发送应用报告", exc_info=True)
    out["subject"] = subject
    detail_suffix = f" · 合并 {len(merged['_devices'])} 台设备"

    if dry_run:
        return {**out, "status": "skipped", "reason": "dry_run"}

    try:
        send_mail(email, subject, text, html_body)
    except Exception as e:                       # noqa: BLE001
        detail = f"{type(e).__name__}: {e}{detail_suffix}"
        try:
            mail_log.record_mail(anchor_key, day, email, mail_log.MAIL_FAILED, detail, now=now, weekly=weekly)
        except Exception as log_err:             # noqa: BLE001
            log.warning("连失败记录都写不进去：%s", log_err)
        log.warning("%s合并发送失败 day=%s to=%s：%s", "周报" if weekly else "日报", day, email, detail)
        return {**out, "status": "failed", "reason": detail}

    try:
        mail_log.record_mail(anchor_key, day, email, mail_log.MAIL_SENT,
                             subject + detail_suffix, now=now, weekly=weekly)
    except Exception as e:                       # noqa: BLE001
        log.warning("邮件已发出但记录失败 day=%s：%s", day, e)

    log.info("%s合并已发送 day=%s to=%s devices=%d",
             "周报" if weekly else "日报", day, email, len(merged["_devices"]))
    return {**out, "status": "sent", "reason": ""}


def run(day=None, catchup_days=0, dry_run=False, user_key=None, now=None, weekly=None):
    """给所有订阅用户发某一天的报告。

    发的是日报还是周报，由 weekly 参数决定：传 True 发周报，传 False 发日报，
    不传则按星期几默认（见 is_weekly_day，周日发周报）。日报和周报是两种模板，
    任何一天都可以套用任一种。外部 cron 不用改：每天 00:03 照旧调
    POST /api/v1/admin/daily-mail，默认会发送昨天；昨天是周报日时自然生成周报。

    参数：
        day          发哪一天，默认昨天，保证默认路径只处理已经结束且留出
                     数据上传缓冲的完整自然日。显式传日期用于手工补发/排查。
        catchup_days 顺带补发前几天（默认 0）。定时器和启动补发会带上它，
                     这样某天因为重启没发出去时能自愈。已经发过的会被
                     mail_log 挡掉，不会重复发。补发出来的报告种类也是那一天
                     该有的那种（种类只看日期，不看「什么时候跑的」）。
        user_key     只给这一个设备发，并且**忽略订阅开关** —— 这是手工
                     排查用的入口，用户主动要求重发时不该被订阅状态拦住。
        dry_run      只渲染不发送。

    返回：
        {"ok": bool, "day": ..., "days": [...],
         "sent"/"skipped"/"failed": 计数, "results": [每次尝试的明细],
         "reason": 整轮没跑成时的原因}
    """
    now = now or datetime.now()
    target = day or (now.date() - timedelta(days=1))
    if not isinstance(target, date):
        target = date.fromisoformat(str(target))

    result = {"ok": False, "day": target.isoformat(), "days": [],"sent": 0, "skipped": 0, "failed": 0, "results": []}

    if not config.MAIL_ENABLED:
        result["reason"] = "disabled"
        return result

    # 补发的日子只取「已经过完的」。哪怕调用方手滑把 day 传成了明天，
    # 也不会把还没结束的一天当报告发出去。
    days = [target] + [target - timedelta(days=i) for i in range(1, max(0, int(catchup_days)) + 1)]
    days = [d for d in days if d <= now.date()]
    result["days"] = [d.isoformat() for d in days]

    if user_key:
        user = users.get_user(user_key)
        if user is None:
            result["reason"] = "no_such_user"
            return result
        # 身份用调用方传进来的那个，不从查出来的行里再取一遍：少一个
        # 「行里恰好没有 user_key 这一列就 KeyError」的隐患。
        groups = [{"email": mail_merge.normalize_email(user.get("email")),
                   "devices": [{**user, "user_key": user_key}]}]
    else:
        subscribers = users.get_subscribers()
        if not subscribers:
            result["ok"] = True
            result["reason"] = "no_subscribers"
            return result
        # 同一邮箱下的全部 user_key 视为同一人，合并成一封邮件；
        # 不同邮箱仍各发各的，单设备行为与过去完全一致。
        groups = mail_merge.group_devices_by_email(subscribers)

    for d in days:
        for group in groups:
            if len(group["devices"]) == 1:
                device = group["devices"][0]
                outcome = send_day(
                    device["user_key"], d,
                    email=device.get("email"), dry_run=dry_run, now=now,
                    weekly=weekly,
                )
            else:
                outcome = send_merged_day(
                    group["devices"], d,
                    email=group["email"], dry_run=dry_run, now=now,
                    weekly=weekly,
                )
            result[outcome["status"]] += 1
            result["results"].append(outcome)

    result["ok"] = result["failed"] == 0
    log.info("报告发送完成 day=%s 天=%d 成功=%d 跳过=%d 失败=%d",
             result["day"], len(days), result["sent"],
             result["skipped"], result["failed"])
    return result
