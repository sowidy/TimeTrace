"""Authenticated, opt-in datasets. Bootstrap retains the legacy UUID capability."""
import hashlib
import hmac
from datetime import date, datetime, timedelta
from uuid import UUID

import pymysql
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from api.deps import require_admin
from api.tracking_schemas import DeviceRequest, TimelineUpload, WebUpload, CompleteApps
from db import conn as db_conn, tracking as store, users

router = APIRouter()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(user_key, authorization):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(401, "需要设备 Bearer token")
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM device_tokens WHERE user_key=%s", (str(user_key),))
            row = cur.fetchone()
    if not row or not hmac.compare_digest(row["token_hash"], token_hash(token)):
        raise HTTPException(401, "设备凭据不正确")


@router.post("/api/v1/device/register")
def register_device(req: DeviceRequest, authorization: str = Header(default="")):
    """Claim a token once; retries require the same token. Rotation uses admin reset.

    Token is generated and saved by the client *before* the request so response loss
    cannot strand a registration. On upgrade possession of legacy UUID authorizes
    initial claim; existing UUID endpoints retain their historical capability model.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(401, "需要新的随机 Bearer token")
    key, digest = str(req.user_key), token_hash(token)
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            users.upsert_user(cur, key, req.hostname, req.platform, req.client_version)
            # Serialize competing first claims on the stable parent row.
            cur.execute("SELECT user_key FROM users WHERE user_key=%s FOR UPDATE", (key,))
            cur.fetchone()
            cur.execute("SELECT token_hash FROM device_tokens WHERE user_key=%s", (key,))
            old = cur.fetchone()
            if old and not hmac.compare_digest(old["token_hash"], digest):
                raise HTTPException(409, "该设备已绑定其他凭据，请联系管理员重置")
            if not old:
                cur.execute("INSERT INTO device_tokens VALUES (%s,%s,%s)", (key, digest, datetime.now()))
    return {"ok": True}


@router.delete("/api/v1/admin/device-token/{user_key}", dependencies=[Depends(require_admin)])
def reset_device_token(user_key: UUID):
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM device_tokens WHERE user_key=%s", (str(user_key),))
    return {"ok": True}


def upload_dataset(kind, req, authorization):
    authenticate(req.user_key, authorization)
    if len({d.day for d in req.days}) != len(req.days):
        raise HTTPException(422, "请求内日期不得重复")
    if any(d.day > date.today() or (d.complete and d.day >= date.today()) for d in req.days):
        raise HTTPException(422, "仅已结束自然日可标记 complete")
    count = 0
    try:
        with db_conn.connect() as conn:
            with conn.cursor() as cur:
                users.upsert_user(cur, str(req.user_key), req.hostname, req.platform, req.client_version)
                for day in req.days:
                    rows = day.sites if kind == "web" else day.bins
                    count += store.replace_day(cur, kind, str(req.user_key), day.day,
                                               [r.model_dump() for r in rows], day.complete)
                    if kind == "web":
                        if day.hourly is None:
                            store.clear_day(cur, "web_hourly", str(req.user_key), day.day)
                        else:
                            count += store.replace_day(cur, "web_hourly", str(req.user_key), day.day,
                                                       [r.model_dump() for r in day.hourly], day.complete)
                        if day.intervals is None:
                            store.clear_day(cur, "web_intervals", str(req.user_key), day.day)
                        else:
                            count += store.replace_day(cur, "web_intervals", str(req.user_key), day.day,
                                                       [r.model_dump() for r in day.intervals], day.complete)
                    else:
                        if day.intervals is None:
                            store.clear_day(cur, "app_intervals", str(req.user_key), day.day)
                        else:
                            count += store.replace_day(cur, "app_intervals", str(req.user_key), day.day,
                                                       [r.model_dump() for r in day.intervals], day.complete)
                        # Android unlocks ride on the timeline upload; absent
                        # means "old client / not an Android day" and leaves
                        # any previously stored value alone.
                        if getattr(day, "unlocks", None) is not None:
                            store.save_unlocks(cur, str(req.user_key), day.day, day.unlocks)
    except pymysql.MySQLError:
        raise HTTPException(503, "写入失败，请稍后重试")
    return {"ok": True, "days_written": len(req.days), "rows_written": count}


@router.post("/api/v1/web-usage/upload")
def upload_web(req: WebUpload, authorization: str = Header(default="")):
    return upload_dataset("web", req, authorization)


@router.post("/api/v1/app-timeline/upload")
def upload_timeline(req: TimelineUpload, authorization: str = Header(default="")):
    return upload_dataset("timeline", req, authorization)


@router.post("/api/v1/app-summary/complete")
def complete_apps(req: CompleteApps, authorization: str = Header(default="")):
    """Confirm the old upload's snapshot total before declaring a day complete."""
    authenticate(req.user_key, authorization)
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            for day in req.days:
                if day.day >= date.today():
                    raise HTTPException(422, "当天尚未结束")
                cur.execute("SELECT COALESCE(SUM(seconds),0) AS seconds FROM usage_records "
                            "WHERE user_key=%s AND day=%s", (str(req.user_key), day.day))
                total = float(cur.fetchone()["seconds"])
                if abs(total - day.seconds) > max(2, total * .001):
                    raise HTTPException(409, "应用汇总尚未同步")
                cur.execute("INSERT INTO tracking_uploads (user_key,day,kind,complete,uploaded_at) "
                            "VALUES (%s,%s,'apps',TRUE,%s) ON DUPLICATE KEY UPDATE "
                            "complete=TRUE,uploaded_at=VALUES(uploaded_at)",
                            (str(req.user_key), day.day, datetime.now()))
    return {"ok": True}


def checked_range(start, end):
    end = end or date.today()
    start = start or end
    if start > end or (end - start).days > 366:
        raise HTTPException(422, "日期范围为 0..366 天")
    return start, end


@router.get("/api/v1/web-usage/{user_key}")
def read_web(user_key: UUID, start: date | None = None, end: date | None = None,
             authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    start, end = checked_range(start, end)
    return {"records": store.read_range("web", str(user_key), start, end)}


@router.get("/api/v1/web-usage-summary/{user_key}")
def web_summary(user_key: UUID, day: date | None = None, authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    day = day or date.today()
    records = store.read_range("web", str(user_key), day, day)
    return {"day": str(day), "total_seconds": sum(r["seconds"] for r in records), "sites": records}


@router.get("/api/v1/web-hourly/{user_key}")
def read_web_hourly(user_key: UUID, start: date | None = None, end: date | None = None,
                    authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    start, end = checked_range(start, end)
    return {"records": store.read_range("web_hourly", str(user_key), start, end),
            "readiness": store.readiness(str(user_key), start, end)}


@router.get("/api/v1/web-intervals/{user_key}")
def read_web_intervals(user_key: UUID, start: date | None = None, end: date | None = None,
                       authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    start, end = checked_range(start, end)
    return {"records": store.read_range("web_intervals", str(user_key), start, end),
            "readiness": store.readiness(str(user_key), start, end)}


@router.get("/api/v1/app-timeline/{user_key}")
def read_timeline(user_key: UUID, start: date | None = None, end: date | None = None,
                  authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    start, end = checked_range(start, end)
    return {"records": store.read_range("timeline", str(user_key), start, end),
            "readiness": store.readiness(str(user_key), start, end)}


@router.get("/api/v1/app-intervals/{user_key}")
def read_app_intervals(user_key: UUID, start: date | None = None, end: date | None = None,
                       authorization: str = Header(default="")):
    authenticate(user_key, authorization)
    start, end = checked_range(start, end)
    return {"records": store.read_range("app_intervals", str(user_key), start, end),
            "readiness": store.readiness(str(user_key), start, end)}


@router.get("/api/v1/merged-timeline/{user_key}")
def read_merged_timeline(user_key: UUID, day: date | None = None,
                         authorization: str = Header(default="")):
    """One fused timeline for a day, across all devices sharing one mailbox.

    The Android app pulls this to draw the merged Android + PC timeline:
    segments follow the same winner rule as the daily mail (at any moment
    the app whose current continuous segment is longest wins; a brief
    pop-up on the other device never breaks the main app's segment).
    When the mailbox has no computer data, only the phone's segments come
    back — the app draws exactly what is here, no special-casing needed.

    ``day`` defaults to yesterday (the last complete day). The caller
    decides the display window: for today it draws up to the current time
    and leaves the rest blank; for past days the segments already cover
    the whole day.
    """
    authenticate(user_key, authorization)
    target = day or (date.today() - timedelta(days=1))
    me = users.get_user(str(user_key))
    email = (me or {}).get("email") or ""
    devices = []
    if email:
        with db_conn.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_key, hostname, platform, created_at FROM users"
                    " WHERE email = %s ORDER BY created_at ASC",
                    (email,),
                )
                devices = [dict(r) for r in cur.fetchall()]
    if not devices:
        devices = [me] if me else []
    from db import tracking as track_store, usage_records
    from mail import charts as mail_charts
    from mail import merge as mail_merge

    app_intervals, site_intervals = [], []
    app_intervals_by_device = []
    named_summaries = []
    for device in devices:
        key = str(device["user_key"])
        try:
            summary = usage_records.get_daily_summary(key, target)
        except Exception:
            summary = None
        if summary:
            named_summaries.append((mail_merge.device_label(device), summary))
        try:
            device_intervals = track_store.read_range(
                "app_intervals", key, target, target)
            app_intervals.extend(device_intervals)
            app_intervals_by_device.append(device_intervals)
            site_intervals.extend(track_store.read_range(
                "web_intervals", key, target, target))
        except Exception:
            app_intervals_by_device.append([])
    merged = mail_merge.merge_app_summaries(named_summaries)
    summary = merged or {"total_seconds": 0, "app_count": 0, "apps": []}
    segments, first, last = mail_charts.timeline_segments(
        summary, app_intervals, site_intervals, target)
    sources = mail_merge.sources_by_key(
        (mail_merge.device_kind(d.get("platform")),
         {r.get("app_name") for r in rows if r.get("app_name")})
        for d, rows in zip(devices, app_intervals_by_device))
    out_segments = []
    for start, end, app_name, label, subs in segments:
        out_segments.append({
            "start_second": start,
            "end_second": end,
            "app_name": app_name,
            "display_name": label,
            "source": sources.get(app_name, "P"),
            "sites": [
                {"start_second": s, "end_second": e, "host": host,
                 "display_name": site_label}
                for s, e, host, site_label in subs
            ],
        })
    return {
        "day": target.isoformat(),
        "devices": [
            {"label": mail_merge.device_label(d),
             "kind": mail_merge.device_kind(d.get("platform"))}
            for d in devices
        ],
        "segments": out_segments,
        "first_second": first,
        "last_second": last,
        "total_seconds": summary.get("total_seconds", 0),
    }
