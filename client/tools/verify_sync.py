"""Synthetic localhost sync check; uses temporary SQLite, never the user's data."""
import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import socket
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Disposable localhost server only")
    args = parser.parse_args()
    from urllib.parse import urlsplit
    if urlsplit(args.url).hostname != "127.0.0.1":
        parser.error("Verification only permits 127.0.0.1")
    from core import config, storage, site_storage
    from core.timeutil import beijing_today
    from net import uploader, tracking_upload
    config.SERVER_URL = args.url
    config.REQUEST_TIMEOUT, config.RETRY_COUNT = 1, 0
    with tempfile.TemporaryDirectory(prefix="pctime-client-verify-") as temp:
        storage.DB_PATH = str(Path(temp) / "usage.db")
        storage.init_db()
        day = beijing_today() - timedelta(days=1)
        start = datetime.combine(day, time(9))

        def record(at):
            storage.record_fragment("editor.exe", "private title", "C:/private/editor.exe",
                                    at.isoformat(), (at + timedelta(seconds=900)).isoformat(), 900)

        record(start)
        # Real refused connection: failure must preserve local history and watermark.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            config.SERVER_URL = f"http://127.0.0.1:{unavailable.getsockname()[1]}"
            failed = uploader.upload_pending()
        assert not failed["ok"] and not storage.get_setting("upload_watermark"), failed
        record(start + timedelta(minutes=15))
        assert storage.get_daily_total(str(day)) == 1800
        config.SERVER_URL = args.url
        first = uploader.upload_pending()
        assert first["ok"] and first["days"] == 1, first
        again = uploader.upload_pending()
        assert again["ok"], again
        # Opt-in extra datasets use the real client serializer and Bearer handshake.
        site_storage.record_segment({"event_id": str(uuid4()), "session_id": str(uuid4()),
                                    "browser": "chrome", "host": "example.com"},
                                   [(start.timestamp(), start.timestamp() + 900)])
        storage.set_setting("web_upload_enabled", "1")
        storage.set_setting("timeline_upload_enabled", "1")
        extra = tracking_upload.run_once()
        assert extra == {"web": "ok", "timeline": "ok"}, extra
        from urllib.request import urlopen, Request
        import hashlib
        key = uploader.get_or_create_user_key()
        token = storage.get_setting("device_token_" + hashlib.sha256(args.url.encode()).hexdigest()[:16])
        def read(path, bearer=False):
            request = Request(args.url + "/api/v1/" + path,
                              headers={"Authorization": "Bearer " + token} if bearer else {})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        summary = read(f"summary/{key}?day={day}")
        sites = read(f"web-usage/{key}?start={day}&end={day}", True)["records"]
        bins = read(f"app-timeline/{key}?start={day}&end={day}", True)["records"]
        assert summary["total_seconds"] == 1800 and summary["app_count"] == 1, summary
        assert len(sites) == 1 and sites[0]["seconds"] == 900, sites
        assert sum(r["seconds"] for r in bins) == 1800, bins
        assert "private" not in json.dumps([summary, sites, bins]), "Private title/path leaked"
        print(json.dumps({"ok": True, "offline_recording": True, "retry_sync": True,
                          "idempotent": True, "application_seconds": 1800,
                          "website_seconds": 900, "timeline_seconds": 1800}))


if __name__ == "__main__":
    main()
