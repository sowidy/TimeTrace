const byId = id => document.getElementById(id);
const render = value => {
  byId("status").textContent = value === "connected" ? "已连接，本地网站记录已就绪。"
    : "等待桌面端连接。请确认已启动新版 TimeTrace 并开启本地网站统计。";
};
byId("extension-id").textContent = chrome.runtime.id;
const saved = await chrome.storage.local.get(["config", "connection_status"]);
byId("browser").value = saved.config?.browser || "auto";
byId("token").value = saved.config?.token || "";
render(saved.connection_status);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.connection_status) render(changes.connection_status.newValue);
});
byId("save").addEventListener("click", async () => {
  const current = await chrome.storage.local.get("config");
  await chrome.storage.local.set({config: {
    ...(current.config || {profile: crypto.randomUUID()}), browser: byId("browser").value,
    token: byId("token").value.trim(), reconnect_at: Date.now()
  }});
  render("waiting");
});
