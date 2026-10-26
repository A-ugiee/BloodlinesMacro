const http = require("http");
const { WebSocketServer } = require("ws");

const stats = { started: Date.now(), msgsIn: 0, msgsOut: 0, bytesIn: 0, bytesOut: 0 };
const log = (...a) => console.log(new Date().toISOString(), ...a);

const SYS_KEYS = ["version", "os", "screen", "scale", "ocr", "areas"];
const cleanSys = (raw) => {
  if (!raw || typeof raw !== "object") return null;
  const out = {};
  for (const k of SYS_KEYS) {
    const v = raw[k];
    if (typeof v === "string") out[k] = v.slice(0, 100);
    else if (typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v))) out[k] = v;
    else if (Array.isArray(v)) out[k] = v.filter((x) => typeof x === "string").slice(0, 10).map((x) => x.slice(0, 20));
  }
  return out;
};
const ago = (t) => (t ? Math.round((Date.now() - t) / 1000) : null);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const users = () =>
  [...parties].flatMap(([key, party]) =>
    [...party].map((c) => ({
      party: key, name: c.name, connected_s: ago(c.joinedAt), last_update_s: ago(c.updatedAt),
      hp: c.hp, chakra: c.chakra, status: c.status, mode: c.mode, mode_t: c.modeT,
      combat: c.combat, inv: c.inv, focus: c.focus, frozen: c.frozen, sys: c.sys,
    })),
  );

const dur = (s) => (s == null ? "-" : s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);

const btn = (key, label, party, name) =>
  `<form method="post" action="/admin/update?key=${encodeURIComponent(key)}${party != null ? "&party=" + encodeURIComponent(party) + "&name=" + encodeURIComponent(name) : ""}" style="margin:0">` +
  `<button>${label}</button></form>`;

const adminPage = (key, note) => {
  const rows = users()
    .map((u) => {
      const sys = u.sys || {};
      const state = u.status === "menu" ? "Menu" : [u.combat && "Combat", u.inv && "Inventory", u.focus === false && "Tabbed out"].filter(Boolean).join(", ") || "In game";
      return `<tr><td>${esc(u.party)}</td><td><b>${esc(u.name)}</b></td><td>${esc(u.hp ?? "-")}%</td><td>${esc(u.chakra ?? "-")}%</td>
<td>${esc(u.mode ?? "-")}${u.mode_t != null ? " " + esc(u.mode_t) + "s" : ""}</td><td>${esc(state)}</td>
<td>${dur(u.connected_s)}</td><td>${dur(u.last_update_s)} ago</td><td>${esc(sys.version ?? "?")}</td><td>${esc(sys.os ?? "?")}</td>
<td>${esc(sys.screen ?? "?")}${sys.scale ? " @ " + esc(sys.scale) + "%" : ""}</td><td>${sys.ocr == null ? "?" : sys.ocr ? "yes" : "no"}</td><td>${sys.areas ? esc(sys.areas.join(", ") || "none") : "?"}</td>
<td>${btn(key, "Update", u.party, u.name)}</td></tr>`;
    })
    .join("");
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>Bloodlines relay</title>
<style>body{font:14px system-ui,sans-serif;background:#111214;color:#ececee;margin:24px}table{border-collapse:collapse;width:100%}
th,td{padding:6px 10px;border-bottom:1px solid #2a2c31;text-align:left;white-space:nowrap}th{color:#8b8d94;font-weight:600}
.m{color:#8b8d94}button{background:#d12f55;color:#fff;border:0;border-radius:6px;padding:4px 10px;font:600 12px system-ui;cursor:pointer}
button:hover{background:#e64a6e}.note{color:#3ecf72}.bar{display:flex;gap:16px;align-items:center}</style>
<div class="bar"><h2>Bloodlines relay</h2>${btn(key, "Update everyone")}${note ? `<span class="note">${esc(note)}</span>` : ""}</div>
<p class="m">Up ${dur(Math.round((Date.now() - stats.started) / 1000))} · ${wss.clients.size} connected · ${stats.msgsIn} msgs in · ${Math.round(stats.bytesOut / 1024)} KB out · refreshes every 3s</p>
<table><tr><th>Party</th><th>Name</th><th>HP</th><th>Chakra</th><th>Mode</th><th>State</th><th>Online</th><th>Last update</th><th>App</th><th>Windows</th><th>Screen</th><th>OCR</th><th>Areas set</th><th></th></tr>
${rows || '<tr><td colspan="14" class="m">Nobody connected</td></tr>'}</table>
<p class="m">Update asks apps to install the newest GitHub release now. Only works on apps that already have this feature; older apps update the next time they restart.</p>`;
};

const snapshot = () => {
  const out = {
    uptime_s: Math.round((Date.now() - stats.started) / 1000),
    connections: wss.clients.size,
    messages_in: stats.msgsIn,
    messages_out: stats.msgsOut,
    kb_in: Math.round(stats.bytesIn / 1024),
    kb_out: Math.round(stats.bytesOut / 1024),
    parties: {},
  };
  for (const u of users()) (out.parties[u.party] ||= []).push(u);
  return out;
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://relay");
  const admin = process.env.ADMIN_KEY;
  const authed = admin && url.searchParams.get("key") === admin;
  if (url.pathname === "/stats" && authed) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(snapshot(), null, 2));
    return;
  }
  if (url.pathname === "/admin" && authed) {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(adminPage(admin, url.searchParams.get("note")));
    return;
  }
  if (url.pathname === "/admin/update" && req.method === "POST" && authed) {
    const party = url.searchParams.get("party");
    const name = url.searchParams.get("name");
    let sent = 0;
    for (const [key, members] of parties) {
      for (const c of members) {
        if (party != null && (key !== party || c.name !== name)) continue;
        if (c.ws.readyState !== 1) continue;
        c.ws.send(JSON.stringify({ t: "update_now" }));
        sent++;
      }
    }
    log(`admin update request -> ${party != null ? `${name} in ${party}` : "everyone"} (${sent} sent)`);
    const note = party != null ? `Update sent to ${name}` : `Update sent to ${sent} app${sent === 1 ? "" : "s"}`;
    res.writeHead(303, { location: `/admin?key=${encodeURIComponent(admin)}&note=${encodeURIComponent(note)}` });
    res.end();
    return;
  }
  res.writeHead(200);
  res.end("ok");
});
const wss = new WebSocketServer({ server });
const parties = new Map();

const pct = (v) => (Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : null);
const secs = (v) => (Number.isInteger(v) && v >= 0 && v < 1000 ? v : null);
const MODES = ["ready", "active", "cooldown"];

const broadcast = (key) => {
  const party = parties.get(key);
  if (!party) return;
  const members = {};
  for (const c of party) {
    members[c.name] = { hp: c.hp, chakra: c.chakra, status: c.status, avatar: c.avatar, mode: c.mode, mode_t: c.modeT, inv: c.inv, focus: c.focus, frozen: c.frozen, combat: c.combat };
  }
  const msg = JSON.stringify({ t: "state", members });
  for (const c of party) {
    if (c.ws.readyState !== 1) continue;
    c.ws.send(msg);
    stats.msgsOut++;
    stats.bytesOut += msg.length;
  }
};

wss.on("connection", (ws) => {
  const client = { ws, name: null, key: null, hp: null, chakra: null, status: null, avatar: null, mode: null, modeT: null, inv: false, focus: true, frozen: false, combat: false, sys: null, joinedAt: null, updatedAt: null };

  ws.on("message", (raw) => {
    stats.msgsIn++;
    stats.bytesIn += raw.length;
    let m;
    try {
      m = JSON.parse(raw);
    } catch {
      return;
    }
    if (m.t === "join" && !client.key && typeof m.party === "string" && typeof m.name === "string") {
      client.key = m.party.slice(0, 64);
      client.name = m.name.slice(0, 24);
      client.avatar = typeof m.avatar === "string" && /^https?:\/\//i.test(m.avatar) ? m.avatar.slice(0, 500) : null;
      client.sys = cleanSys(m.sys);
      client.joinedAt = Date.now();
      if (!parties.has(client.key)) parties.set(client.key, new Set());
      parties.get(client.key).add(client);
      log(`join  party=${client.key} name=${client.name} (${parties.get(client.key).size} in party)`);
      broadcast(client.key);
    } else if (m.t === "update" && client.key) {
      client.hp = pct(m.hp);
      client.chakra = pct(m.chakra);
      client.status = m.status === "menu" ? "menu" : "game";
      client.mode = MODES.includes(m.mode) ? m.mode : null;
      client.modeT = secs(m.mode_t);
      client.inv = m.inv === true;
      client.focus = m.focus !== false;
      client.frozen = m.frozen === true;
      client.combat = m.combat === true;
      client.updatedAt = Date.now();
      if (process.env.LOG_UPDATES === "1") log(`update party=${client.key} name=${client.name}`, JSON.stringify(m));
      broadcast(client.key);
    }
  });

  ws.on("close", () => {
    const party = parties.get(client.key);
    if (!party) return;
    party.delete(client);
    log(`leave party=${client.key} name=${client.name} (${party.size} left)`);
    if (party.size === 0) parties.delete(client.key);
    else broadcast(client.key);
  });
});

server.listen(process.env.PORT || 8080, () => log("relay up"));