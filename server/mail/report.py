"""报告正文渲染：把使用汇总变成人看的文本 / HTML。

两种报告，共用同一套版式：
    * **日报** —— build_message()，一天一封，覆盖当天；
    * **周报** —— build_weekly_message()，一周一封，覆盖「以某天结尾的 7 天」，
      正文是按应用**跨天聚合**过的（数据源不同：日报用 get_daily_summary，
      周报用 get_range_summary，见 db/usage_records.py）。

两者共用纯文本明细和 HTML 外壳；周报使用柔色堆叠柱与折线，
日报的软件使用时段和时长图由 tracking_report 根据真实起止记录追加。

**哪天发周报不在这里决定** —— 本模块只管「给定区间和汇总怎么渲染」，
「周日发周报」那条策略在 mail/sender.py。这样这个模块保持纯计算。

这个模块是**纯计算**的 —— 不碰数据库、不碰网络，所以 dry_run 和单元测试
都能直接调它检查内容；可选时间线与网站图由 tracking_report 追加。
"""
import html
from datetime import timedelta

import config
from mail.charts import application_chart

FOOTER_TEXT = (
    "TimeTrace 自动报告。退订：POST /api/v1/subscription/<设备ID>。"
)


def format_duration(seconds):
    """秒 → 人看的时长。刚好整分/整小时时不带零头，读起来清爽。"""
    total = int(round(float(seconds or 0)))
    if total < 60:
        return f"{total} 秒"

    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分"

    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def build_subject(day, summary):
    """日报主题。周报的主题在 build_weekly_subject。"""
    return (f"【TimeTrace】{day.isoformat()} 使用报告 · "
            f"共 {format_duration(summary['total_seconds'])}")


def week_range(day):
    """返回「以 day 结尾的那 7 天」：(start, end)，都是 date。

    只做算术，不看 day 是星期几 —— 「哪一天发周报」是 config.MAIL_WEEKLY_DAY
    的事（见 mail/sender.py）。默认周日发，所以这里算出来的正好是周一..周日。

    刻意不用 strftime("%A") / isocalendar() 这类接口：
        * %A / %a 的结果跟着进程 locale 走（LANG 一变，"Sunday" 就变成「星期日」），
          同一个函数在不同服务器上会给出不同的东西；
        * isocalendar() 是 ISO 周（周一起算），周日属于「下一周」，直接拿来算
          区间就是经典的差一周。
    直接减 6 天最直观，也不依赖任何本地化。
    """
    return day - timedelta(days=6), day


def _visible_apps(summary, limit):
    """返回 (要列出的应用, 被折叠掉的应用数, 被折叠掉的秒数)。

    单日应用数没有上限（有人开几百个进程），全列出来邮件会大到被收件方
    拒收，所以只列前 limit 个，剩下的折成一行汇总。

    周报直接用同一个函数：一周的应用更多，但「列前 N 个 + 折一行」的规则
    没有理由不一样。
    """
    apps = summary.get("apps") or []
    if limit > 0 and len(apps) > limit:
        shown = apps[:limit]
        rest = apps[limit:]
        return shown, len(rest), sum(float(a["seconds"]) for a in rest)
    return apps, 0, 0.0


def _app_label(app):
    """优先用显示名；显示名是空的时候退回进程名。"""
    return app.get("display_name") or app.get("app_name") or "(未知)"


def _pct(seconds, total):
    """占比百分数。总时长为 0 时返回 0.0 —— 免得 0/0 把整封信炸掉。

    周报里更容易撞上这种情况：某天有记录但秒数全是 0（只是启动过、没累计
    出时长），7 天加起来仍然是 0。
    """
    return (float(seconds) / total * 100) if total else 0.0


def _totals_text(summary):
    """「总计」那一行要的两个数：(总秒数, 「共 N 个应用」里的 N)。日/周共用。

    注意 N 取的是 summary['app_count']，也就是**折叠前**的应用总数 ——
    折叠只该影响「列出几行」，不该让「共 N 个应用」跟着变小。
    取不到时才退回数 apps 的长度（手工拼的 summary 可能没这个键）。
    """
    total = float(summary.get("total_seconds") or 0)
    app_count = summary.get("app_count") or len(summary.get("apps") or [])
    return total, app_count


def _text_app_lines(apps, total, hidden_count, hidden_seconds):
    """无图片客户端使用的应用明细（不编号），日报和周报共用。"""
    lines = []
    for app in apps:
        pct = _pct(app["seconds"], total)
        lines.append(
            f"• {_app_label(app)} — {format_duration(app['seconds'])}"
            f"（{pct:.1f}%，启动 {int(app['launches'])} 次）"
        )
    if hidden_count:
        lines.append(
            f"     （另有 {hidden_count} 个应用未列出，合计 {format_duration(hidden_seconds)}）"
        )
    return lines


def _merged_device_text_lines(summary):
    """Merged-mail breakdown: per-device totals plus the overlap line.

    Only for summaries built by mail.merge (``_devices`` with >= 2 entries).
    Single-device mail is untouched. The overlap line must stay explicit:
    merged total = devices summed - overlap, never silently deducted.
    """
    devices = summary.get("_devices") or []
    if len(devices) < 2:
        return []
    lines = ["分设备："]
    for device in devices:
        lines.append(f"  • {device.get('label') or '未知设备'} — "
                     f"{format_duration(device.get('total_seconds') or 0)}")
    overlap = summary.get("_overlap_seconds")
    if overlap:
        lines.append(f"双设备重叠：{format_duration(overlap)}"
                     f"（同一秒两台设备都有记录，已从合并总计中扣除）")
    return lines


def _merged_device_html(summary):
    """HTML version of the breakdown above (device rows + overlap row)."""
    devices = summary.get("_devices") or []
    if len(devices) < 2:
        return ""
    rows = "".join(
        f'<div style="color:#5f6368;font-size:13px;margin-top:2px;">'
        f'{html.escape(str(device.get("label") or "未知设备"))}：'
        f'{html.escape(format_duration(device.get("total_seconds") or 0))}</div>'
        for device in devices)
    overlap = summary.get("_overlap_seconds")
    overlap_row = (
        f'<div style="color:#5f6368;font-size:13px;margin-top:2px;">'
        f'双设备重叠：{html.escape(format_duration(overlap))}'
        f'（同一秒两台设备都有记录，已从合并总计中扣除）</div>'
        if overlap else "")
    return (f'<div style="margin-top:8px;"><div style="color:#5f6368;'
            f'font-size:13px;">分设备：</div>{rows}{overlap_row}</div>')


def _html_device_row(user):
    """设备名那一行（HTML 版）。不知道机器名就返回空串，整行不出现。"""
    if user and user.get("hostname"):
        return (
            f'<div style="color:#5f6368;font-size:13px;margin-top:4px;">'
            f'设备：{html.escape(str(user["hostname"]))}</div>'
        )
    return ""


def _html_totals(total, app_count):
    """「总计 …，共 N 个应用」那段的内层 HTML（外面那层 div 在外壳里）。

    这里 int() 一下：app_count 是从 summary 里读出来的，手工拼的数据可能给个
    字符串或浮点，而它要落进 HTML 正文。
    """
    return (f'总计 <strong>{format_duration(total)}</strong>，共\n'
            f'      {int(app_count)} 个应用')


def _html_shell(heading, totals_html, extra_html, rows):
    """整封 HTML 的外壳：样式、表头、页脚都在这儿，日/周共用一份。

    heading 要**已经转义好**再传进来（这个函数自己不转义）—— 日报传的是
    html.escape(day.isoformat())，和周报一样。

    extra_html 是「总计」下面那块附加信息：日报是设备那一行，周报是周期行
    加上设备行。空串就不占位。
    """
    return f"""<!DOCTYPE html>
    <html><head><meta charset="utf-8"></head>
    <body style="margin:0;padding:24px 12px;background:#f5f6f7;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;">
        <div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;">
            <div style="color:#5f6368;font-size:13px;letter-spacing:1px;">TimeTrace</div>
            <div style="font-size:22px;font-weight:600;color:#202124;margin-top:6px;">
                {heading}
            </div>
            <div style="color:#202124;font-size:15px;margin-top:12px;">
              {totals_html}
            </div>
            {extra_html}
            {''.join(rows)}
            <!-- tracking-sections -->
            <div style="margin-top:24px;padding-top:14px;border-top:1px solid #eceff1;color:#9aa0a6;font-size:12px;line-height:1.7;">
            {html.escape(FOOTER_TEXT)}
            </div>
        </div>
    </body>
    </html>"""


def _period_line(start, end, summary):
    """周报的「周期：…（7 天，有数据 5 天）」那一串，文本和 HTML 共用。

    天数由 (end - start).days + 1 算出来，不写死 7 —— week_range 哪天改成
    不是 7 天，这里不会跟着说错话。有数据的天数取不到就只报区间。
    """
    span = (end - start).days + 1
    days = summary.get("days_with_data")
    if days:
        return (f"{start.isoformat()} ~ {end.isoformat()}"
                f"（{span} 天，有数据 {int(days)} 天）")
    return f"{start.isoformat()} ~ {end.isoformat()}（{span} 天）"


# ── 日报 ──────────────────────────────────────────────────────

def build_text(day, summary, user=None):
    """日报的纯文本正文。"""
    total, app_count = _totals_text(summary)
    apps, hidden_count, hidden_seconds = _visible_apps(summary, config.MAIL_MAX_APPS)

    lines = [
        "TimeTrace 每日使用报告",
        "=" * 24,
        "",
        f"日期：{day.isoformat()}",
    ]
    if user and user.get("hostname"):
        lines.append(f"设备：{user['hostname']}")
    lines += [f"总计：{format_duration(total)}，共 {app_count} 个应用", ""]
    lines += _merged_device_text_lines(summary)
    if summary.get("_devices") and len(summary["_devices"]) >= 2:
        lines += [""]
    lines += _text_app_lines(apps, total, hidden_count, hidden_seconds)
    lines += ["", "-" * 24, FOOTER_TEXT]
    return "\n".join(lines)


def build_html(day, summary, user=None):
    """日报的 HTML 正文。"""
    total, app_count = _totals_text(summary)

    return _html_shell(
        heading=f"{html.escape(day.isoformat())} 使用报告",
        totals_html=_html_totals(total, app_count),
        extra_html=_html_device_row(user) + _merged_device_html(summary),
        rows=[],
    )


def build_message(day, summary, user=None):
    """渲染一封**日报**，返回 (主题, 纯文本正文, HTML 正文)。不发信，纯计算 ——
    所以 dry_run 和测试都能直接用它检查内容。周报用 build_weekly_message。"""
    return (
        build_subject(day, summary),
        build_text(day, summary, user),
        build_html(day, summary, user),
    )


def build_missing_app_message(day, user=None, weekly=False):
    """Website data can arrive first; absence is not zero usage."""
    start, end = week_range(day) if weekly else (day, day)
    period = f"{start} ~ {end}" if weekly else str(day)
    heading = f"{period} {'周报' if weekly else '使用报告'}"
    note = "应用汇总尚未收到，暂展示已收到的网站或时间线数据。"
    return (f"【TimeTrace】{heading} · 应用汇总未收到",
            f"{heading}\n{note}\n\n{FOOTER_TEXT}",
            _html_shell(html.escape(heading), html.escape(note), _html_device_row(user), []))


# ── 周报 ──────────────────────────────────────────────────────

def build_weekly_subject(day, summary):
    """周报主题。带上区间，收件箱里一眼就能和日报区分开。"""
    start, end = week_range(day)
    return (f"【TimeTrace】{start.isoformat()} ~ {end.isoformat()} 周报 · "
            f"共 {format_duration(summary['total_seconds'])}")


def build_weekly_text(day, summary, user=None):
    """周报的纯文本正文。day 是这一周的**最后一天**（默认周日）。

    和日报共用 _text_app_lines —— 应用明细是同一套渲染，不同的只有
    抬头、「周期」行，以及多出来的「有数据 N 天」。
    """
    start, end = week_range(day)
    total, app_count = _totals_text(summary)
    apps, hidden_count, hidden_seconds = _visible_apps(summary, config.MAIL_MAX_APPS)

    lines = [
        "TimeTrace 每周使用报告",
        "=" * 24,
        "",
        f"周期：{_period_line(start, end, summary)}",
    ]
    if user and user.get("hostname"):
        lines.append(f"设备：{user['hostname']}")
    lines += [f"总计：{format_duration(total)}，共 {app_count} 个应用", ""]
    lines += _merged_device_text_lines(summary)
    if summary.get("_devices") and len(summary["_devices"]) >= 2:
        lines += [""]
    lines += _text_app_lines(apps, total, hidden_count, hidden_seconds)
    lines += ["", "-" * 24, FOOTER_TEXT]
    return "\n".join(lines)


def build_weekly_html(day, summary, user=None):
    """周报的 HTML 正文。"""
    start, end = week_range(day)
    total, app_count = _totals_text(summary)

    span = (end - start).days + 1
    days_with_data = summary.get("days_with_data")
    period_row = (f'<div style="color:#5f6368;font-size:13px;margin-top:4px;">'
                  f'有数据 {int(days_with_data)}/{span} 天</div>'
                  if days_with_data is not None and int(days_with_data) < span else "")

    return _html_shell(
        heading=(f"{html.escape(start.isoformat())} ~ "
                 f"{html.escape(end.isoformat())} 周报"),
        totals_html=_html_totals(total, app_count),
        extra_html=period_row + _html_device_row(user) + _merged_device_html(summary),
        rows=[application_chart(summary, start, end)],
    )


def build_weekly_message(day, summary, user=None):
    """渲染一封**周报**，(主题, 纯文本正文, HTML 正文)。和 build_message
    一样是纯计算。day 是这一周的最后一天。"""
    return (
        build_weekly_subject(day, summary),
        build_weekly_text(day, summary, user),
        build_weekly_html(day, summary, user),
    )
