"""Readable, email-safe daily itineraries from approximate 15-minute records."""
from collections import defaultdict
from datetime import timedelta
import html
import math

from mail.colors import color_map
from mail.display_names import display_name_for
from mail import chart_theme as theme

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def clock(bucket):
    return f"{bucket // 4:02d}:{bucket % 4 * 15:02d}"


def build_spans(rows):
    """Merge adjacent bins with the same main app, never across unrecorded gaps.

    Bins cannot recover exact switch times or app order inside a quarter-hour.
    Keep all secondary apps and actual seconds rather than inventing sessions.
    """
    bins = [defaultdict(float) for _ in range(96)]
    for row in rows:
        bucket, seconds = int(row["bucket"]), float(row["seconds"])
        if 0 <= bucket < 96 and math.isfinite(seconds) and seconds > 0:
            bins[bucket][row["app_name"]] += seconds
    spans = []
    for bucket, apps in enumerate(bins):
        if not apps:
            continue
        main = min(apps, key=lambda app: (-apps[app], app))
        if spans and spans[-1]["end"] == bucket and spans[-1]["app_name"] == main:
            span = spans[-1]
            span["end"] = bucket + 1
        else:
            span = {"start": bucket, "end": bucket + 1, "app_name": main,
                    "apps": defaultdict(float), "seconds": 0}
            spans.append(span)
        for app, seconds in apps.items():
            span["apps"][app] += seconds
            span["seconds"] += seconds
    return spans


def visible_indices(spans, limit):
    """Retain first/last and longest periods, in chronological order."""
    if len(spans) <= limit:
        return set(range(len(spans)))
    shown = {0, len(spans) - 1}
    candidates = sorted(range(1, len(spans)-1), key=lambda i: (-spans[i]["seconds"], i))
    shown.update(candidates[:max(0, limit-2)])
    return shown


def _time_row(time, content, color="#dceaf1", background=theme.SURFACE, marker="●"):
    # Tables, solid colors and inline text work even without CSS layout support.
    return ('<tr><td width="116" valign="top" style="padding:10px 7px 10px 0;'
            f'font-size:12px;color:{theme.MUTED};white-space:nowrap">{html.escape(time)}</td>'
            f'<td width="16" valign="top" style="padding-top:10px;border-left:2px solid #e3edf3;'
            f'color:{color};font-size:13px">{marker}</td>'
            f'<td valign="top" bgcolor="{background}" style="padding:9px 12px;border-bottom:5px solid #fff;'
            f'border-radius:8px;line-height:1.7">{content}</td></tr>')


def render(summary, rows, start, end, readiness=()):
    """Return text + HTML: explicit time/app labels, no heatmap or percent encoding."""
    from mail.report import format_duration
    weekly = start != end
    heading = "一周应用时间线" if weekly else "当天应用时间线"
    days = [start + timedelta(days=i) for i in range((end-start).days+1)]
    data = {str(day): [] for day in days}
    for row in rows:
        key = str(row.get("day", start))
        if key in data:
            data[key].append(row)
    spans_by_day = {key: build_spans(value) for key, value in data.items()}
    total = sum(s["seconds"] for spans in spans_by_day.values() for s in spans)
    names = {r["app_name"]: display_name_for(r["app_name"], r.get("display_name")) for r in summary.get("apps", [])}
    colors = color_map(list(names) + [r["app_name"] for value in data.values() for r in value])
    complete = {(str(r["day"]), r["kind"]) for r in readiness if r["complete"]}
    text = [heading]
    markup = [f'<h2 style="font-size:18px;color:#334e68;margin-top:28px">{heading}</h2>']
    if not any(spans_by_day.values()):
        if all((str(day), "timeline") in complete for day in days):
            note = ("本周" if weekly else "当天") + "已完成时间线上传，没有记录到应用使用时段。"
        else:
            note = (("本周" if weekly else "当天") + "尚未收到应用时间线。请在客户端“网站记录”中启用"
                    "“邮件中加入应用时间线”，并确认已手动上传或开启自动上传，服务端连接可用。")
        return "\n".join(text + [note]), "".join(markup) + f'<p style="font-size:13px;color:#64748b">{note}</p>'
    line = f"已记录 {format_duration(total)} · 有记录 {sum(bool(s) for s in spans_by_day.values())} 天"
    text.append(line)
    markup.append(f'<p style="font-size:13px;color:#48677e">{line}</p>')
    note = "从上往下看：左边是时段，右边是应用。数据按 15 分钟汇总，起止时间为近似值，不代表一直在操作。"
    text.append(note)
    markup.append(f'<p style="font-size:12px;color:#667f92;line-height:1.8">{note}</p>')
    expected = float(summary.get("total_seconds") or 0)
    if summary.get("_app_summary_missing") or abs(total-expected) > max(60, expected*.01):
        note = "时间线数据不完整，以下只展示已收到的时段；总使用时长仍以应用汇总为准。"
        text.append(note)
        markup.append(f'<p style="font-size:12px;color:#946432">{note}</p>')
    for day in days:
        spans = spans_by_day[str(day)]
        title = f"{day:%m-%d} {WEEKDAYS[day.weekday()]}"
        text.append(str(day))
        if weekly:
            duration = f"记录 {format_duration(sum(s['seconds'] for s in spans))}" if spans else ""
            markup.append('<div style="margin:18px 0 7px;padding:7px 10px;background:#eff8fc;'
                          f'border-radius:8px;color:#355c73;font-size:13px"><strong>{title}</strong> '
                          f'<span style="color:#627e90">{duration}</span></div>')
        if not spans:
            note = "当天无使用记录" if (str(day), "timeline") in complete else "无时间线数据（未收到）"
            text.append(note)
            markup.append(f'<p style="font-size:12px;color:#7b8e9e;padding-left:10px">{note}</p>')
            continue
        shown = visible_indices(spans, 4 if weekly else 12)
        events, omitted = [], []
        for index, span in enumerate(spans):
            period = f"{clock(span['start'])}–{clock(span['end'])}"
            main = span["app_name"]
            others = [(app, seconds) for app, seconds in span["apps"].items() if app != main]
            duration_label = "时段共记录" if others else "记录"
            gap = (f"{clock(spans[index-1]['end'])}–{clock(span['start'])}"
                   if index and span["start"] > spans[index-1]["end"] else None)
            if gap:
                text.append(f"    {gap} 未记录时段（不代表未使用）")
            text.append(f"  约 {period} · {names.get(main, main)} · {duration_label} {format_duration(span['seconds'])}")
            text.extend(f"    {names.get(app, app)} {format_duration(seconds)}"
                        for app, seconds in sorted(span["apps"].items(), key=lambda item: (-item[1], item[0])))
            if index not in shown:
                omitted.append(span)
                continue
            if omitted:
                omission = f"另有 {len(omitted)} 个时段，完整明细见邮件纯文本。"
                events.append(_time_row(f"{clock(omitted[0]['start'])}–{clock(omitted[-1]['end'])}",
                                        f'<span style="font-size:11px;color:#7b8e9e">{omission}</span>', marker="·"))
                omitted = []
            if gap:
                events.append(_time_row(gap, '<span style="font-size:11px;color:#7b8e9e">未记录时段</span>', marker="·"))
            label = html.escape(names.get(main, main))
            content = (f'<strong style="font-size:14px;color:#243f54">{label}</strong>'
                       f'<span style="font-size:12px;color:#627e90">　{duration_label} {format_duration(span["seconds"])}</span>')
            if others:
                detail = "、".join(f"{names.get(app, app)} {format_duration(seconds)}" for app, seconds in others[:2])
                if len(others) > 2:
                    detail += f" 等 {len(others)} 个应用"
                content += f'<br><span style="font-size:11px;color:#627e90">本时段主要使用 {label}；还用过 {html.escape(detail)}</span>'
            events.append(_time_row(period, content, colors[main], theme.SURFACE))
        markup.append('<table data-timeline="itinerary" role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                      + "".join(events) + '</table>')
    return "\n".join(text), "".join(markup)
