"""Independent opt-in uploads to the configured HTTP(S) server; no token logging."""
from datetime import timedelta
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from core import config, storage, site_storage, timeline
from core.timeutil import beijing_now, beijing_today

_lock = threading.Lock()
MAX_DAYS_PER_REQUEST = 7


class UploadFailure(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__("server_unsupported" if status in (404, 501) else f"HTTP {status or 'network_error'}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer tokens must not follow redirects to a different origin/plaintext URL.
        return None


def _post(path, payload, token):
    request = urllib.request.Request(config.base_url() + config.API_PREFIX + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=config.REQUEST_TIMEOUT) as response:
            result = json.loads(response.read(1024 * 1024))
        if result.get("ok") is not True:
            raise UploadFailure()
        return result
    except urllib.error.HTTPError as exc:
        raise UploadFailure(exc.code) from None
    except (OSError, ValueError):
        raise UploadFailure() from None


def post(path, payload, token):
    for attempt in range(max(1, config.RETRY_COUNT)):
        try:
            return _post(path, payload, token)
        except UploadFailure as exc:
            if (exc.status is not None and exc.status != 429 and exc.status < 500
                    or exc.status == 501 or attempt + 1 >= max(1, config.RETRY_COUNT)):
                raise
            time.sleep(min(8, config.RETRY_BACKOFF * (2 ** attempt)))


def describe_status(status):
    """Translate stored results without confusing application and website uploads."""
    return {
        "ok": "上传成功",
        "disabled": "未上传（上传开关未开启）",
        "busy": "上传正在进行，请稍后重试",
        "server_unsupported": "服务端不支持明细上传，请更新服务端",
        "HTTP 401": "设备认证失败，请检查服务端设备凭据",
        "HTTP 409": "设备凭据冲突，请联系管理员重置",
        "HTTP 422": "服务端未接受明细数据，请检查客户端与服务端版本",
        "HTTP 503": "服务端暂时无法写入数据，请稍后重试",
        "HTTP network_error": "网络连接失败，请检查服务端地址和连接",
        "error": "上传发生异常，请重试",
    }.get(status, status)


def run_once(force=False):
    enabled = [kind for kind, key in (("web", "web_upload_enabled"), ("timeline", "timeline_upload_enabled"))
               if storage.get_setting(key, "0") == "1"]
    if not enabled or (not force and storage.get_setting("upload_enabled", "1") != "1"):
        return {"reason": "disabled"}
    if not _lock.acquire(blocking=False):
        return {"reason": "busy"}
    try:
        from net import uploader
        meta = {"user_key": uploader.get_or_create_user_key(), **uploader._machine_info()}
        # Persist before registering. Scope credentials by server URL, never reuse at another server.
        import hashlib
        token_key = "device_token_" + hashlib.sha256(config.base_url().encode()).hexdigest()[:16]
        token = storage.get_setting(token_key, "")
        if not token:
            token = secrets.token_urlsafe(32)
            storage.set_setting(token_key, token)
        try:
            post("/device/register", meta, token)
        except UploadFailure as exc:
            for kind in enabled:
                storage.set_setting(f"{kind}_upload_status", str(exc))
            return {kind: str(exc) for kind in enabled}
        if storage.get_setting("tracking_server_url", "") != config.base_url():
            for kind in ("web", "timeline"):
                storage.set_setting(f"{kind}_upload_watermark", "")
            storage.set_setting("tracking_server_url", config.base_url())
        today = beijing_today()
        results = {}
        try:
            yesterday = today - timedelta(days=1)
            post("/app-summary/complete", {**meta, "days": [{"day": str(yesterday),
                 "seconds": storage.get_daily_total(str(yesterday))}]}, token)
        except UploadFailure:
            # A failed/stale legacy snapshot must never become a false completion marker.
            pass
        for kind in enabled:
            try:
                watermark = storage.get_setting(f"{kind}_upload_watermark", "")
                detail_backfill = ((kind == "web" and
                                    (storage.get_setting("web_hourly_backfill_v1", "") != "1" or
                                     storage.get_setting("web_intervals_backfill_v1", "") != "1")) or
                                   (kind == "timeline" and
                                    storage.get_setting("app_intervals_backfill_v1", "") != "1"))
                available = set(storage.get_available_dates()) if kind == "timeline" else set()
                if kind == "web":
                    with storage._get_conn() as conn:
                        available.update(r[0] for r in conn.execute("SELECT DISTINCT date FROM site_usage"))
                if detail_backfill:
                    # Existing installations uploaded only daily totals. Retry a bounded
                    # local window once; repeat on failure because uploads are idempotent.
                    available.update(str(today - timedelta(days=i)) for i in range(14))
                # Recent dates are explicit snapshots, including empty days to clear stale data.
                available.update(str(today - timedelta(days=i)) for i in range(3))
                days = sorted(d for d in available if d <= str(today)
                              and (force or not watermark or d >= watermark or d >= str(today - timedelta(days=3))
                                   or detail_backfill and d >= str(today - timedelta(days=13))))
                pending = []
                for day in days:
                    key = "web_upload_enabled" if kind == "web" else "timeline_upload_enabled"
                    if (not force and storage.get_setting("upload_enabled", "1") != "1"
                            or storage.get_setting(key, "0") != "1"):
                        raise ValueError("disabled")
                    if kind == "web":
                        records = [{"host": r["host"], "display_name": r["display_name"],
                                    "seconds": round(r["total_seconds"], 3)} for r in site_storage.get_site_stats(day)]
                        block = {"day": day, "sites": records, "complete": day < str(today),
                                 "hourly": site_storage.get_site_hourly_bins_for_date(day),
                                 "intervals": site_storage.get_site_intervals_for_date(day)}
                        path = "/web-usage/upload"
                    else:
                        bins, intervals = timeline.build_snapshot(day)
                        block = {"day": day, "bins": bins, "intervals": intervals,
                                 "complete": day < str(today)}
                        path = "/app-timeline/upload"
                    pending.append(block)
                    if len(pending) == MAX_DAYS_PER_REQUEST or day == days[-1]:
                        # A batch is transactional on the server. Advance only after
                        # acceptance, so failed batches remain eligible for retry.
                        post(path, {**meta, "days": pending}, token)
                        storage.set_setting(f"{kind}_upload_watermark", day)
                        pending = []
                if detail_backfill:
                    if kind == "web":
                        storage.set_setting("web_hourly_backfill_v1", "1")
                        storage.set_setting("web_intervals_backfill_v1", "1")
                    else:
                        storage.set_setting("app_intervals_backfill_v1", "1")
                storage.set_setting(f"{kind}_upload_last_success", beijing_now().isoformat(timespec="seconds"))
                results[kind] = "ok"
            except (UploadFailure, ValueError) as exc:
                results[kind] = str(exc)
            storage.set_setting(f"{kind}_upload_status", results[kind])
        return results
    finally:
        _lock.release()
