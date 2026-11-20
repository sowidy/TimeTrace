"""Real Qt WebSocket handshake on an ephemeral loopback port and temporary SQLite."""
import json
import base64
import hashlib
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtTest import QTest

from browser.bridge import BrowserBridge, pairing_token
from browser.identity import BUNDLED_EXTENSION_ID
from core import storage, site_storage


class BridgeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", os.path.join(self.temp.name, "usage.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.init_db()
        storage.set_setting("web_tracking_enabled", "1")
        storage.set_setting("web_extension_ids", "a" * 32)
        self.bridge = BrowserBridge(port=0)
        self.addCleanup(self.bridge.stop)
        self.addCleanup(self.bridge.timer.stop)
        self.profile = str(uuid4())
        self.sockets = []

    def spin(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertTrue(predicate(), "WebSocket operation timed out")

    def connect(self, origin=None, token=None, auto=False, path="/timetrace-browser"):
        socket = QWebSocket(origin or "chrome-extension://" + "a" * 32)
        self.sockets.append(socket)
        self.addCleanup(socket.abort)
        messages, closed = [], []
        socket.textMessageReceived.connect(lambda raw: messages.append(json.loads(raw)))
        socket.disconnected.connect(lambda: closed.append(True))
        socket.connected.connect(lambda: socket.sendTextMessage(json.dumps({
            "type": "hello", "protocol": 1, "browser": "chrome", "profile_instance_id": self.profile,
            "auto_connect": auto,
            "token": token if token is not None else ("" if auto else pairing_token())})))
        socket.open(QUrl(f"ws://127.0.0.1:{self.bridge.server.serverPort()}{path}"))
        return socket, messages, closed

    def test_legacy_extension_path_still_connects(self):
        _, messages, _ = self.connect(path="/usagetime-browser")
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))

    def test_plain_web_origin_and_wrong_token_rejected(self):
        for origin, token in (("https://evil.example", None), ("a" * 32, None), (None, "wrong-token")):
            with self.subTest(origin=origin):
                socket, messages, closed = self.connect(origin, token)
                self.spin(lambda: bool(closed))
                self.assertNotIn("hello_ack", [m["type"] for m in messages])

    def test_hello_segment_ack_and_replay(self):
        socket, messages, closed = self.connect()
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))
        self.assertEqual(self.bridge.server.serverAddress().toString(), "127.0.0.1")
        now = time.time()
        self.bridge.coordinator.grants.append((self.profile, now - 2, now))
        event = {"type": "segment", "protocol": 1, "event_id": str(uuid4()), "session_id": str(uuid4()),
                 "profile_instance_id": self.profile, "browser": "chrome", "host": "example.com",
                 "started_at_ms": (now - 2) * 1000, "ended_at_ms": now * 1000}
        socket.sendTextMessage(json.dumps(event))
        self.spin(lambda: sum(m["type"] == "ack" for m in messages) == 1)
        socket.sendTextMessage(json.dumps(event))
        self.spin(lambda: sum(m["type"] == "ack" for m in messages) == 2)
        rows = site_storage.get_site_stats()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["total_seconds"], 2, places=2)

    def test_bundled_identity_matches_manifest_public_key(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] /
                               "client/browser_extension/chromium/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "TimeTrace 网站使用记录")
        digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
        self.assertEqual("".join(chr(97 + int(c, 16)) for c in digest), BUNDLED_EXTENSION_ID)

    def test_bundled_extension_auto_connects_without_id_registration_or_token(self):
        storage.set_setting("web_extension_ids", "")
        socket, messages, closed = self.connect("chrome-extension://" + BUNDLED_EXTENSION_ID, auto=True)
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))
        self.bridge.refresh()
        self.assertFalse(closed)
        self.assertEqual(len(self.bridge.clients), 1)
        self.assertEqual(next(iter(self.bridge.clients.values()))["profile"], self.profile)
        storage.set_setting("web_tracking_enabled", "0")
        self.bridge.refresh()
        self.assertFalse(self.bridge.server.isListening())
        self.assertEqual(self.bridge.clients, {})

    def test_auto_connect_does_not_bypass_custom_extension_authentication(self):
        _, messages, closed = self.connect(auto=True)
        self.spin(lambda: bool(closed))
        self.assertFalse(any(m["type"] == "hello_ack" for m in messages))

    def test_unregistered_extension_cannot_claim_auto_connect(self):
        _, messages, closed = self.connect("chrome-extension://" + "b" * 32, auto=True)
        self.spin(lambda: bool(closed))
        self.assertFalse(any(m["type"] == "hello_ack" for m in messages))

    def test_ungranted_segment_gets_no_duration(self):
        socket, messages, _ = self.connect()
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))
        now = time.time()
        socket.sendTextMessage(json.dumps({"type": "segment", "protocol": 1,
            "event_id": str(uuid4()), "session_id": str(uuid4()), "profile_instance_id": self.profile,
            "browser": "chrome", "host": "example.com", "started_at_ms": (now-2)*1000,
            "ended_at_ms": now*1000}))
        self.spin(lambda: any(m["type"] == "ack" for m in messages))
        self.assertEqual(site_storage.get_site_stats(), [])

    def test_settings_and_local_history_construct_without_uploading(self):
        from ui.browser_panel import BrowserSettingsPanel, WebsiteUsagePanel
        panel = BrowserSettingsPanel()
        history = WebsiteUsagePanel()
        from datetime import date
        history.set_range(date.today(), date.today())
        self.assertIn("等待", panel.status.text())
        self.assertIn("暂无记录", history.top_sites.text())
        self.assertEqual(storage.get_setting("web_upload_enabled", "0"), "0")
        panel.timer.stop()
        panel.deleteLater()
        history.deleteLater()

    def test_shutdown_drains_final_segment_without_regranting(self):
        socket, messages, _ = self.connect()
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))
        now = time.time()
        self.bridge.coordinator.grants.append((self.profile, now - 2, now))
        event = {"type": "segment", "protocol": 1, "event_id": str(uuid4()),
                 "session_id": str(uuid4()), "profile_instance_id": self.profile,
                 "browser": "chrome", "host": "example.com",
                 "started_at_ms": (now - 2) * 1000, "ended_at_ms": now * 1000}

        def final_segment(raw):
            if json.loads(raw)["type"] == "suspend":
                # A late foreground callback/heartbeat must not reopen the grant.
                self.bridge.activity.emit({"app_name": "chrome.exe", "observed_at": time.time()})
                socket.sendTextMessage(json.dumps({"type": "heartbeat", "focused": True}))
                socket.sendTextMessage(json.dumps(event))

        socket.textMessageReceived.connect(final_segment)
        self.bridge.shutdown(timeout_ms=200)
        self.assertEqual(site_storage.get_site_stats()[0]["total_seconds"], 2)
        self.assertTrue(any(m["type"] == "ack" for m in messages))
        self.assertFalse(any(m["type"] == "resume" for m in messages))
        self.assertFalse(self.bridge.server.isListening())
        self.assertEqual(self.bridge.clients, {})
        self.bridge.refresh()
        self.assertFalse(self.bridge.server.isListening())

    def test_shutdown_is_bounded_when_extension_does_not_reply(self):
        _, messages, _ = self.connect()
        self.spin(lambda: any(m["type"] == "hello_ack" for m in messages))
        started = time.monotonic()
        self.bridge.shutdown(timeout_ms=50)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.bridge.timer.isActive())
        self.bridge.shutdown()  # Repeated shutdown does not wait again or reopen.
