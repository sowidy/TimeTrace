"""Verify a server-only deployment against a disposable LOCAL MySQL instance.

No production configuration, ports, files or databases are used. Optionally pass
--client ../client to exercise the actual client uploader and offline recovery.
"""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pymysql

SERVER = Path(__file__).resolve().parents[1]
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def http(base, path, body=None, token=None, expected=200):
    request = Request(base + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json",
                               **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urlopen(request, timeout=8) as response:
            status, data = response.status, response.read()
    except HTTPError as exc:
        status, data = exc.code, exc.read()
    assert status == expected, (path, status, data[:1000])
    return json.loads(data)


def wait_http(base, process, log):
    deadline = time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        try:
            if http(base, "/api/v1/health")["database"]:
                return
        except (URLError, OSError, AssertionError):
            time.sleep(.2)
    raise RuntimeError("API startup failed: " + log.read_text(errors="replace")[-4000:])


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def contracts(base):
    key, token = str(uuid4()), uuid4().hex + uuid4().hex
    day = str(date.today() - timedelta(days=1))
    meta = {"user_key": key, "hostname": "disposable-test", "platform": "test"}
    post = lambda path, data, expected=200, auth=token: http(base, "/api/v1" + path, data, auth, expected)
    post("/device/register", meta)
    post("/device/register", meta)
    post("/device/register", meta, 409, "x" * 64)
    apps = {**meta, "days": [{"day": day, "apps": [
        {"app_name": "editor.exe", "display_name": "Editor", "seconds": 1800, "launches": 1}]}]}
    web = {**meta, "days": [{"day": day, "complete": True,
                            "sites": [{"host": "EXAMPLE.COM", "seconds": 900}],
                            "hourly": [{"hour": 9, "host": "EXAMPLE.COM", "seconds": 600},
                                       {"hour": 10, "host": "EXAMPLE.COM", "seconds": 300}],
                            "intervals": [{"host": "EXAMPLE.COM", "start_second": 9 * 3600,
                                           "end_second": 9 * 3600 + 600},
                                          {"host": "EXAMPLE.COM", "start_second": 10 * 3600,
                                           "end_second": 10 * 3600 + 300}]}]}
    timeline = {**meta, "days": [{"day": day, "complete": True, "bins": [
        {"bucket": 36, "app_name": "editor.exe", "seconds": 900},
        {"bucket": 37, "app_name": "editor.exe", "seconds": 900}]}]}
    post("/web-usage/upload", web, 401, "")
    for _ in range(2):
        post("/upload", apps, auth="")
        post("/web-usage/upload", web)
        post("/app-timeline/upload", timeline)
    result = http(base, f"/api/v1/summary/{key}?day={day}")
    assert result["total_seconds"] == 1800 and result["app_count"] == 1, result
    rows = http(base, f"/api/v1/web-usage/{key}?start={day}&end={day}", token=token)["records"]
    assert len(rows) == 1 and rows[0]["seconds"] == 900 and rows[0]["host"] == "example.com"
    hours = http(base, f"/api/v1/web-hourly/{key}?start={day}&end={day}", token=token)["records"]
    assert len(hours) == 2 and sum(r["seconds"] for r in hours) == 900, hours
    intervals = http(base, f"/api/v1/web-intervals/{key}?start={day}&end={day}", token=token)["records"]
    assert len(intervals) == 2 and sum(r["end_second"]-r["start_second"] for r in intervals) == 900
    post("/app-summary/complete", {**meta, "days": [{"day": day, "seconds": 1800}]})
    state = http(base, f"/api/v1/app-timeline/{key}?start={day}&end={day}", token=token)
    assert sum(r["seconds"] for r in state["records"]) == 1800
    assert {r["kind"] for r in state["readiness"] if r["complete"]} == {"apps", "web", "web_hourly", "web_intervals", "timeline"}
    post("/upload", apps, auth="")
    state = http(base, f"/api/v1/app-timeline/{key}?start={day}&end={day}", token=token)
    assert not next(r["complete"] for r in state["readiness"] if r["kind"] == "apps")
    return {"api": True, "auth": True, "idempotent": True, "readback": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqld", default=shutil.which("mysqld"))
    parser.add_argument("--client", type=Path, help="Optional independent client source directory")
    args = parser.parse_args()
    if not args.mysqld:
        parser.error("Pass --mysqld /path/to/mysqld")
    client_path = args.client.resolve() if args.client else None
    if client_path and not (client_path / "tools/verify_sync.py").is_file():
        parser.error("--client must contain tools/verify_sync.py; omit --client for a server-only checkout")
    with tempfile.TemporaryDirectory(prefix="timetrace-standalone-") as temp:
        root = Path(temp)
        isolated = root / "server"
        shutil.copytree(SERVER, isolated, ignore=shutil.ignore_patterns(
            ".env", ".env.local", "__pycache__", ".venv", "*.log"))
        if client_path:
            client_copy = root / "client-only" / "client"
            shutil.copytree(client_path, client_copy, ignore=shutil.ignore_patterns(
                "dist", "build", "data", ".env*", "__pycache__", ".venv", "*.log", "*.db*"))
            client_path = client_copy
        # A copy containing server/ alone proves there are no sibling-code imports.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("TIMETRACE_", "PCTIME_", "USAGETIME_")) and k != "PYTHONPATH"}
        db_port, api_port = free_port(), free_port()
        env.update(TIMETRACE_DB_HOST="127.0.0.1", TIMETRACE_DB_PORT=str(db_port),
                   TIMETRACE_DB_USER="root", TIMETRACE_DB_PASSWORD="", TIMETRACE_DB_INIT_SCHEMA="1",
                   TIMETRACE_MAIL_ENABLED="0", TIMETRACE_MAIL_SCHEDULER="0",
                   TIMETRACE_HOST="127.0.0.1", TIMETRACE_PORT=str(api_port), PYTHONUTF8="1")
        check = subprocess.run([sys.executable, str(isolated / "check.py")], cwd=root,
                               env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert check.returncode == 0, check.stderr
        data = root / "mysql-data"
        mysql_log = root / "mysql.log"
        with mysql_log.open("wb") as log:
            initialized = subprocess.run([args.mysqld, "--no-defaults", "--initialize-insecure",
                                          "--datadir=" + data.as_posix()], stdout=log, stderr=log,
                                         creationflags=FLAGS, timeout=120)
            if initialized.returncode:
                raise RuntimeError(mysql_log.read_text(errors="replace")[-4000:])
            mysql = subprocess.Popen([args.mysqld, "--no-defaults", "--datadir=" + data.as_posix(),
                "--bind-address=127.0.0.1", f"--port={db_port}", "--mysqlx=OFF", "--skip-log-bin",
                "--innodb-buffer-pool-size=32M", "--console"], stdout=log, stderr=log, creationflags=FLAGS)
            try:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        db = pymysql.connect(host="127.0.0.1", port=db_port, user="root", connect_timeout=1,
                                             autocommit=True)
                        break
                    except pymysql.MySQLError:
                        if mysql.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError(mysql_log.read_text(errors="replace")[-4000:])
                        time.sleep(.2)
                try:
                    scenarios = {}
                    for legacy in (False, True):
                        name = "legacy" if legacy else "fresh"
                        database = "verify_" + name
                        with db.cursor() as cur:
                            cur.execute(f"CREATE DATABASE {database} CHARACTER SET latin1")
                            if legacy:
                                # Simulate an old users table with a different collation.
                                user_ddl = (isolated / "db/schema.sql").read_text(encoding="utf-8")
                                user_ddl = user_ddl[user_ddl.index("CREATE TABLE"):].split(";")[0]
                                cur.execute("USE " + database)
                                cur.execute(user_ddl.replace("utf8mb4_unicode_ci", "utf8mb4_general_ci"))
                        env["TIMETRACE_DB_NAME"] = database
                        base = f"http://127.0.0.1:{api_port}"
                        api_log = root / (name + ".log")
                        with api_log.open("wb") as api_out:
                            api = subprocess.Popen([sys.executable, str(isolated / "run.py")],
                                cwd=root, env=env, stdout=api_out, stderr=api_out, creationflags=FLAGS)
                            try:
                                wait_http(base, api, api_log)
                                scenarios[name] = contracts(base)
                                if client_path and not legacy:
                                    client = subprocess.run([sys.executable, str(client_path / "tools/verify_sync.py"),
                                        "--url", base], cwd=root, env=env, capture_output=True, text=True,
                                        encoding="utf-8", timeout=60)
                                    assert client.returncode == 0, client.stdout + client.stderr
                                    scenarios["client"] = json.loads(client.stdout.strip().splitlines()[-1])
                            finally:
                                stop(api)
                        # Repeat init, render real optional mail modules without SMTP.
                        code = ("from db import conn; conn.init_schema(); conn.init_schema(); "
                                "from mail.tracking_report import render; from datetime import date; "
                                "render({'total_seconds':0,'apps':[]}, [], [], date.today(), date.today())")
                        result = subprocess.run([sys.executable, "-c", code], cwd=isolated,
                                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
                        assert result.returncode == 0, result.stderr
                        # Runtime mode with only DML rights must not require CREATE.
                        with db.cursor() as cur:
                            username = "verify_" + name
                            cur.execute(f"CREATE USER '{username}'@'127.0.0.1' IDENTIFIED BY 'temporary-test'")
                            cur.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {database}.* TO '{username}'@'127.0.0.1'")
                        runtime = dict(env, TIMETRACE_DB_USER=username, TIMETRACE_DB_PASSWORD="temporary-test",
                                       TIMETRACE_DB_INIT_SCHEMA="0")
                        result = subprocess.run([sys.executable, str(isolated / "check.py"), "--database"],
                                                cwd=root, env=runtime, capture_output=True, text=True,
                                                encoding="utf-8", timeout=30)
                        assert result.returncode == 0, result.stderr
                        scenarios[name].update(schema_repeatable=True, read_only_schema_check=True)
                        # Partial upgrades must fail before any request is served.
                        with db.cursor() as cur:
                            cur.execute(f"DROP TABLE {database}.tracking_uploads")
                        incomplete = subprocess.run([sys.executable, str(isolated / "check.py"), "--database"],
                            cwd=root, env=runtime, capture_output=True, text=True, encoding="utf-8", timeout=30)
                        assert incomplete.returncode != 0 and "tracking_uploads" in incomplete.stderr
                        scenarios[name]["incomplete_schema_rejected"] = True
                    print(json.dumps({"ok": True, "server_only": True, "scenarios": scenarios}))
                finally:
                    with db.cursor() as cur:
                        cur.execute("SHUTDOWN")
                    db.close()
                    mysql.wait(timeout=20)
            finally:
                stop(mysql)


if __name__ == "__main__":
    main()
