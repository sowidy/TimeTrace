import {originOf, trimQueue, detectBrowser} from "./protocol.js";

let socket = null, ready = false, allowed = false, current = null, focused = false;
let config, pending = [], serial = Promise.resolve(), sentAt = 0, dropped = 0;
const browserName = () => config.browser && config.browser !== "auto" ? config.browser
  : (navigator.brave ? "brave" : detectBrowser(navigator.userAgent));
const status = state => chrome.storage.local.set({connection_status: state});
const enqueue = fn => { serial = serial.then(fn).catch(() => {}); return serial; };
const initialized = (async () => {
  const saved = await chrome.storage.local.get(["config", "pending_events", "dropped_events"]);
  config = saved.config || {browser: "auto", profile: crypto.randomUUID()};
  pending = trimQueue(saved.pending_events || []);
  dropped = (saved.dropped_events || 0) + (saved.pending_events || []).length - pending.length;
  await chrome.storage.local.set({config, pending_events: pending, dropped_events: dropped});
  await status("waiting");
})();

function send(data) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data));
}

async function checkpoint(end = Date.now()) {
  if (!current) return;
  const previous = current;
  current = {...previous, start: end, sequence: previous.sequence + 1};
  if (end <= previous.start || end - previous.start > 120000) return;
  pending.push({type: "segment", protocol: 1, event_id: crypto.randomUUID(),
    session_id: previous.session, sequence: previous.sequence, browser: browserName(),
    profile_instance_id: config.profile, ...previous.origin,
    started_at_ms: previous.start, ended_at_ms: end});
  const trimmed = trimQueue(pending);
  dropped += pending.length - trimmed.length;
  pending = trimmed;
  await chrome.storage.local.set({pending_events: pending, dropped_events: dropped});
}

async function refresh() {
  await initialized;
  const window = await chrome.windows.getLastFocused().catch(() => ({focused: false}));
  focused = !!window.focused;
  const tabs = focused ? await chrome.tabs.query({active: true, windowId: window.id}) : [];
  const origin = originOf(tabs[0]);
  if (current && (!allowed || !focused || !origin || current.tab !== tabs[0]?.id
      || current.url !== tabs[0]?.url)) {
    await checkpoint();
    current = null;
  }
  if (!current && ready && allowed && focused && origin) {
    current = {tab: tabs[0].id, url: tabs[0].url, origin,
      session: crypto.randomUUID(), sequence: 0, start: Date.now()};
  }
  if (ready) send({type: "heartbeat", focused, pending_events: pending.length, dropped_events: dropped});
}

async function connect() {
  await initialized;
  if (socket) return;
  const ws = new WebSocket("ws://127.0.0.1:8909/timetrace-browser");
  socket = ws;
  ws.onopen = () => {
    if (socket === ws) send({type: "hello", protocol: 1, auto_connect: true, token: config.token || "",
      browser: browserName(), profile_instance_id: config.profile, extension_version: "0.1.1"});
  };
  ws.onmessage = event => enqueue(async () => {
    if (socket !== ws) return;
    const data = JSON.parse(event.data);
    if (data.type === "hello_ack") {
      ready = true; allowed = false; await status("connected"); await refresh();
    } else if (data.type === "ack") {
      pending = pending.filter(e => e.event_id !== data.event_id);
      await chrome.storage.local.set({pending_events: pending});
      sentAt = 0;
    } else if (["suspend", "flush"].includes(data.type)) {
      await checkpoint(); current = null;
      if (data.type === "suspend") allowed = false;
    } else if (data.type === "resume") {
      allowed = true; await refresh();
    }
  });
  ws.onclose = () => enqueue(async () => {
    if (socket === ws) {
      // Count only periods the desktop granted. No speculative offline duration.
      current = null; ready = false; allowed = false; socket = null;
      await status("waiting");
    }
  });
  ws.onerror = () => ws.close();
}

setInterval(() => enqueue(async () => {
  await initialized;
  if (!socket) await connect();
  if (ready) { await checkpoint(); await refresh(); }
}), 20000);

setInterval(() => enqueue(async () => {
  if (!ready) return;
  const trimmed = trimQueue(pending);
  if (trimmed.length !== pending.length) {
    dropped += pending.length - trimmed.length;
    pending = trimmed; await chrome.storage.local.set({pending_events: pending, dropped_events: dropped});
  }
  if (pending.length && Date.now() - sentAt >= 5000) {
    send(pending[0]); sentAt = Date.now();
  }
}), 500);

for (const event of [chrome.tabs.onActivated, chrome.tabs.onUpdated, chrome.tabs.onRemoved,
                      chrome.windows.onFocusChanged]) {
  event.addListener(() => enqueue(refresh));
}
chrome.runtime.onStartup.addListener(() => enqueue(connect));
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("reconnect", {periodInMinutes: 1});
  enqueue(connect);
});
chrome.alarms.onAlarm.addListener(() => enqueue(connect));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.config) return;
  enqueue(async () => {
    await initialized;
    if (JSON.stringify(config) === JSON.stringify(changes.config.newValue)) return;
    const previous = socket;
    current = null; ready = allowed = false; socket = null;
    await status("waiting");
    previous?.close();
    config = changes.config.newValue;
    await connect();
  });
});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
enqueue(connect);
