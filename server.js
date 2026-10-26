const http = require("http");
const { WebSocketServer } = require("ws");

const stats = { started: Date.now(), msgsIn: 0, msgsOut: 0, bytesIn: 0, bytesOut: 0 };
const log = (...a) => console.log(new Date().toISOString(), ...a);

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
  for (const [key, party] of parties) {
    out.parties[key] = [...party].map((c) => ({
      name: c.name, hp: c.hp, chakra: c.chakra, status: c.status, mode: c.mode, mode_t: c.modeT,
      combat: c.combat, inv: c.inv, focus: c.focus, frozen: c.frozen,
    }));
  }
  return out;
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://relay");
  const admin = process.env.ADMIN_KEY;
  if (url.pathname === "/stats" && admin && url.searchParams.get("key") === admin) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(snapshot(), null, 2));
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
  const client = { ws, name: null, key: null, hp: null, chakra: null, status: null, avatar: null, mode: null, modeT: null, inv: false, focus: true, frozen: false, combat: false };

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
