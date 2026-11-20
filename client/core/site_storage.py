"""Site storage shares the application SQLite connection and write lock."""
import csv
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from core import storage
from core.timeutil import beijing_from_timestamp, beijing_now, beijing_today
from browser.sanitizer import normalize_host


def init_db():
    with storage._lock, storage._get_conn() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS site_event_receipts (
                event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS site_fragments (
                id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, part_index INTEGER NOT NULL,
                session_id TEXT NOT NULL, date TEXT NOT NULL, browser TEXT NOT NULL,
                host TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                duration_seconds REAL NOT NULL, UNIQUE(event_id, part_index));
            CREATE INDEX IF NOT EXISTS idx_site_frag_date_host ON site_fragments(date, host);
            CREATE TABLE IF NOT EXISTS site_usage (
                date TEXT NOT NULL, host TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                total_seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY(date, host));
            CREATE TABLE IF NOT EXISTS ignored_sites (host TEXT PRIMARY KEY);
        ''')


def record_segment(item, intervals):
    """Commit receipt and trusted intersections together; callers ACK only after return."""
    with storage._lock, storage._get_conn() as conn:
        if not conn.execute("INSERT OR IGNORE INTO site_event_receipts VALUES (?, ?)",
                            (item["event_id"], beijing_now().isoformat())).rowcount:
            return False
        if conn.execute("SELECT 1 FROM ignored_sites WHERE host=?", (item["host"],)).fetchone():
            return True
        index = 0
        cleared = conn.execute("SELECT value FROM settings WHERE key='site_clear_before'").fetchone()
        cutoff = float(cleared[0]) if cleared else 0
        for start, end in intervals:
            start = max(start, cutoff)
            if start >= end:
                continue
            cursor, finish = beijing_from_timestamp(start), beijing_from_timestamp(end)
            while cursor < finish:
                stop = min(finish, datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time()))
                seconds = (stop - cursor).total_seconds()
                if seconds >= 0.001:
                    day = cursor.date().isoformat()
                    conn.execute("INSERT INTO site_fragments (event_id,part_index,session_id,date,browser,host,"
                                 "start_time,end_time,duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (item["event_id"], index, item["session_id"], day, item["browser"],
                                  item["host"], cursor.isoformat(), stop.isoformat(), seconds))
                    conn.execute("INSERT INTO site_usage(date,host,total_seconds) VALUES (?,?,?) "
                                 "ON CONFLICT(date,host) DO UPDATE SET total_seconds=total_seconds+excluded.total_seconds",
                                 (day, item["host"], seconds))
                    index += 1
                cursor = stop
        # Events older than the protocol replay window can never be accepted again.
        conn.execute("DELETE FROM site_event_receipts WHERE received_at < ?",
                     ((beijing_now() - timedelta(days=1)).isoformat(),))
    return True


def get_site_summary_range(start, end):
    with storage._get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT date,host,display_name,total_seconds FROM site_usage WHERE date BETWEEN ? AND ? "
            "ORDER BY date, total_seconds DESC", (str(start), str(end)))]


def get_site_stats(day=None, start=None):
    day = str(day or beijing_today())
    with storage._get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT host,MAX(display_name) AS display_name,SUM(total_seconds) AS total_seconds "
            "FROM site_usage WHERE date BETWEEN ? AND ? GROUP BY host ORDER BY total_seconds DESC",
            (str(start or day), day))]


def get_week_site_stats(end_date=None):
    end = date.fromisoformat(str(end_date or beijing_today()))
    return get_site_stats(end, end - timedelta(days=6))


def get_site_fragments_for_date(day):
    with storage._get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM site_fragments WHERE date=? ORDER BY start_time,id", (str(day),))]


def get_site_hourly_bins_for_date(day):
    """Build exact host/hour totals from local intervals, never infer from daily totals.

    None means the hourly detail is unavailable or too large for the upload contract.
    An empty list means a verified day with no site time.
    """
    day = date.fromisoformat(str(day))
    rows = get_site_fragments_for_date(day)
    buckets = {}
    midnight = datetime.combine(day, datetime.min.time())
    for row in rows:
        try:
            cursor = max(datetime.fromisoformat(row["start_time"]), midnight)
            finish = min(datetime.fromisoformat(row["end_time"]), midnight + timedelta(days=1))
        except (TypeError, ValueError):
            return None
        while cursor < finish:
            hour = cursor.hour
            boundary = midnight + timedelta(hours=hour + 1)
            stop = min(finish, boundary)
            key = (hour, row["host"])
            buckets[key] = buckets.get(key, 0.0) + (stop - cursor).total_seconds()
            cursor = stop
    result = [{"hour": hour, "host": host, "seconds": round(seconds, 3)}
              for (hour, host), seconds in sorted(buckets.items()) if seconds > 0]
    expected = sum(float(row["total_seconds"]) for row in get_site_stats(day))
    if len(result) > 1000 or abs(sum(row["seconds"] for row in result) - expected) > max(2, expected * .001):
        return None
    return result


def get_site_intervals_for_date(day):
    """Return real domain-only visit intervals, or None if detail is incomplete."""
    day = date.fromisoformat(str(day))
    midnight = datetime.combine(day, datetime.min.time())
    end_of_day = midnight + timedelta(days=1)
    rows = get_site_fragments_for_date(day)
    if len(rows) > 3000:
        return None
    result = []
    for row in rows:
        try:
            start = max(datetime.fromisoformat(row["start_time"]), midnight)
            end = min(datetime.fromisoformat(row["end_time"]), end_of_day)
        except (TypeError, ValueError):
            return None
        if end <= start:
            return None
        result.append({"host": row["host"],
                       "start_second": round((start - midnight).total_seconds(), 3),
                       "end_second": round((end - midnight).total_seconds(), 3)})
    expected = sum(float(row["total_seconds"]) for row in get_site_stats(day))
    actual = sum(row["end_second"] - row["start_second"] for row in result)
    if abs(actual - expected) > max(2, expected * .001):
        return None
    return result


def get_ignored_sites():
    with storage._get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT host FROM ignored_sites ORDER BY host")]


def set_ignored_site(host, ignored=True):
    host = normalize_host(host)
    with storage._lock, storage._get_conn() as conn:
        if ignored:
            conn.execute("INSERT OR IGNORE INTO ignored_sites VALUES (?)", (host,))
        else:
            conn.execute("DELETE FROM ignored_sites WHERE host=?", (host,))


def clear_site_data():
    with storage._lock, storage._get_conn() as conn:
        conn.execute("DELETE FROM site_fragments")
        conn.execute("DELETE FROM site_usage")
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('site_clear_before',?)", (str(time.time()),))
        # Keep receipts during replay window so pending events cannot resurrect deleted data.


def export_sites(start, end, path):
    """Explicit, independent export; neutralize spreadsheet formula injection."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["日期", "网站", "显示名", "使用秒数"])
        for r in get_site_summary_range(start, end):
            writer.writerow([("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                             for v in (r["date"], r["host"], r["display_name"], round(r["total_seconds"], 2))])
    return str(target)


def export_pending():
    """Separate website files/watermark, matching application daily/weekly/monthly folders."""
    if storage.get_setting("web_export_enabled", "0") != "1":
        return []
    from core.paths import data_dir
    import calendar
    today = beijing_today()
    watermark = storage.get_setting("site_export_watermark", "")
    with storage._get_conn() as conn:
        days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT DISTINCT date FROM site_usage ORDER BY date")
                if r[0] >= watermark or r[0] >= str(today - timedelta(days=3))]
    written, weeks, months = [], set(), set()
    def folder(day):
        return Path(data_dir()) / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    for day in days:
        written.append(export_sites(day, day, folder(day) / f"website_daily_{day}.csv"))
        weeks.add(day - timedelta(days=day.weekday()))
        months.add(day.replace(day=1))
    for first in sorted(weeks):
        last = first + timedelta(days=6)
        written.append(export_sites(first, last, folder(first) / f"website_weekly_{first}_{last}.csv"))
    for first in sorted(months):
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        written.append(export_sites(first, last, folder(first) / f"website_monthly_{first:%Y-%m}.csv"))
    if days:
        storage.set_setting("site_export_watermark", str(max(days)))
    return written
