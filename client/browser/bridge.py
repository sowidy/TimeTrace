"""Qt main-thread WebSocket bridge. No network requests leave loopback."""
import hmac
import json
import logging
import re
import secrets
import time

from PySide6.QtCore import QObject, QTimer, QEventLoop, Signal, Slot
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer

from core import storage, site_storage
from .coordinator import BrowserCoordinator
from .protocol import BROWSERS, parse_message, segment
from .identity import BUNDLED_EXTENSION_ID

log = logging.getLogger(__name__)


def pairing_token(reset=False):
    token = storage.get_setting("web_pairing_token", "")
    if reset or not token:
        token = secrets.token_urlsafe(32)
        storage.set_setting("web_pairing_token", token)
    return token


class BrowserBridge(QObject):
    activity = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent=None, port=8909):
        super().__init__(parent)
        self.server = QWebSocketServer("TimeTrace", QWebSocketServer.NonSecureMode, self)
        self.server.setMaxPendingConnections(8)
        self.server.newConnection.connect(self._accept)
        self.activity.connect(self._activity)
        self.clients = {}
        self.coordinator = BrowserCoordinator()
        self.state = {"is_paused": True}
        self._shutting_down = False
        self.port = port
        self.status = "已关闭"
        self.token = pairing_token()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def send(self, socket, data):
        socket.sendTextMessage(json.dumps(data, separators=(",", ":")))

    def refresh(self):
        if self._shutting_down:
            return
        enabled = storage.get_setting("web_tracking_enabled", "0") == "1"
        token = pairing_token()
        if token != self.token:
            for socket in list(self.clients):
                socket.close()
            self.token = token
        if enabled and not self.server.isListening():
            if not self.server.listen(QHostAddress.LocalHost, self.port):
                self.status = "监听失败：8909 端口被占用"
        elif not enabled:
            self.stop()
        now = time.time()
        permitted = {v.strip() for v in storage.get_setting("web_extension_ids", "").split(",")}
        permitted.add(BUNDLED_EXTENSION_ID)
        for socket, peer in list(self.clients.items()):
            if peer.get("extension_id") not in permitted or now - peer["seen"] > (60 if peer.get("profile") else 5):
                socket.close()
        if self.server.isListening():
            count = sum(bool(p.get("profile")) for p in self.clients.values())
            self.status = f"监听 127.0.0.1:{self.server.serverPort()} · 已连接 {count}"
        self._update(now)
        self.status_changed.emit(self.status)

    def shutdown(self, timeout_ms=1500):
        """Freeze grants and briefly keep Qt alive for the extension's final segment/ACK."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.timer.stop()
        self.server.close()  # Stop accepting connections, preserve existing sockets to drain.
        self.coordinator._end(min(time.time(), self.coordinator.last_observed + 1))
        paired = [(socket, peer) for socket, peer in self.clients.items() if peer.get("profile")]
        for socket, peer in paired:
            peer["allowed"] = False
            self.send(socket, {"type": "suspend"})
        if paired and timeout_ms > 0:
            loop = QEventLoop(self)
            deadline = QTimer(loop)
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(min(int(timeout_ms), 2000))
            loop.exec()
            deadline.stop()
            loop.deleteLater()
        self.stop()

    def stop(self):
        self.server.close()
        for socket in list(self.clients):
            self.clients.pop(socket, None)
            socket.disconnected.disconnect()
            self.send(socket, {"type": "suspend"})
            socket.close()
            socket.deleteLater()
        self.coordinator._end(time.time())
        self.status = "已关闭"

    @Slot(object)
    def _activity(self, state):
        if self._shutting_down:
            return
        self.state = state
        self._update(state["observed_at"])

    def _update(self, now):
        if self._shutting_down:
            return
        peers = [p for p in self.clients.values() if p.get("profile")]
        state = dict(self.state)
        if now - state.get("observed_at", 0) > 3:
            state["is_paused"] = True
        owner = self.coordinator.observe(state, peers, now)
        for socket, peer in list(self.clients.items()):
            if not peer.get("profile"):
                continue
            allowed = peer["profile"] == owner
            if allowed != peer.get("allowed"):
                peer["allowed"] = allowed
                self.send(socket, {"type": "resume" if allowed else "suspend"})

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            allowed = storage.get_setting("web_extension_ids", "").split(",")
            allowed.append(BUNDLED_EXTENSION_ID)
            origin = socket.origin()
            extension_id = origin.removeprefix("chrome-extension://")
            if (len(self.clients) >= 8 or socket.requestUrl().path() not in
                    ("/timetrace-browser", "/pctime-browser", "/usagetime-browser")
                    or not origin.startswith("chrome-extension://")
                    or not re.fullmatch(r"[a-p]{32}", extension_id)
                    or extension_id not in [x.strip() for x in allowed]):
                socket.close()
                socket.deleteLater()
                continue
            socket.setMaxAllowedIncomingMessageSize(16384)
            self.clients[socket] = {"seen": time.time(), "rate": [], "focused": False, "extension_id": extension_id}
            socket.textMessageReceived.connect(lambda raw, s=socket: self._message(s, raw))
            socket.binaryMessageReceived.connect(lambda _raw, s=socket: s.close())
            socket.disconnected.connect(lambda s=socket: self._remove(s))

    def _remove(self, socket):
        if self.clients.pop(socket, None) is None:
            return
        self._update(time.time())
        from shiboken6 import isValid
        if isValid(socket):
            socket.deleteLater()

    def _message(self, socket, raw):
        peer = self.clients.get(socket)
        if peer is None:
            return
        now = time.time()
        peer["rate"] = [t for t in peer["rate"] if t > now - 1]
        peer["rate"].append(now)
        if len(peer["rate"]) > 10:
            socket.close()
            return
        try:
            data = parse_message(raw)
            if not peer.get("profile"):
                from uuid import UUID
                automatic = (peer["extension_id"] == BUNDLED_EXTENSION_ID
                             and data.get("auto_connect") is True)
                token_valid = (isinstance(data.get("token"), str)
                               and hmac.compare_digest(data["token"], self.token))
                if (data["type"] != "hello" or data.get("protocol") != 1
                        or data.get("browser") not in BROWSERS
                        or not (automatic or token_valid)):
                    raise ValueError("unauthorized")
                profile = str(UUID(data["profile_instance_id"]))
                if any(p.get("profile") == profile for s, p in self.clients.items() if s != socket):
                    raise ValueError("duplicate_profile")
                peer.update(profile=profile, browser=data["browser"])
                self.send(socket, {"type": "hello_ack", "protocol": 1, "capture_level": "host",
                                   "checkpoint_seconds": 30, "state": "suspended"})
            elif data["type"] == "heartbeat":
                focused = data.get("focused") is True
                if focused and not peer.get("focused"):
                    peer["focus_at"] = now
                peer["focused"] = focused
                self._update(now)
            elif data["type"] == "segment":
                item = segment(data, now)
                if data["profile_instance_id"] != peer["profile"] or item["browser"] != peer["browser"]:
                    raise ValueError("profile_mismatch")
                intervals = self.coordinator.intersections(peer["profile"], item["start"], item["end"])
                inserted = site_storage.record_segment(item, intervals)
                if inserted:
                    self.coordinator.committed(intervals)
                self.send(socket, {"type": "ack", "event_id": item["event_id"]})
            peer["seen"] = now
        except (ValueError, KeyError, TypeError, OverflowError):
            self.send(socket, {"type": "error", "code": "invalid_message"})
            socket.close()
        except Exception:
            # Do not ACK failed SQLite commits; queue retries on reconnection.
            log.exception("Browser bridge storage error")
            socket.close()
