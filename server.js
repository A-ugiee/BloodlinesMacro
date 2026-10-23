const http = require("http");
const { WebSocketServer } = require("ws");

const server = http.createServer((req, res) => {
  res.writeHead(200);
  res.end("ok");
});
const wss = new WebSocketServer({ server });
const parties = new Map();

const broadcast = (key) => {
  const party = parties.get(key);
  if (!party) return;
  const members = {};
  for (const c of party) members[c.name] = c.hp;
  const msg = JSON.stringify({ t: "state", members });
  for (const c of party) if (c.ws.readyState === 1) c.ws.send(msg);
};

wss.on("connection", (ws) => {
  const client = { ws, name: null, hp: null, key: null };

  ws.on("message", (raw) => {
    let m;
    try {
      m = JSON.parse(raw);
    } catch {
      return;
    }
    if (m.t === "join" && !client.key && typeof m.party === "string" && typeof m.name === "string") {
      client.key = m.party.slice(0, 64);
      client.name = m.name.slice(0, 24);
      if (!parties.has(client.key)) parties.set(client.key, new Set());
      parties.get(client.key).add(client);
      broadcast(client.key);
    } else if (m.t === "hp" && client.key && Number.isFinite(m.hp)) {
      client.hp = Math.max(0, Math.min(100, Math.round(m.hp)));
      broadcast(client.key);
    }
  });

  ws.on("close", () => {
    const party = parties.get(client.key);
    if (!party) return;
    party.delete(client);
    if (party.size === 0) parties.delete(client.key);
    else broadcast(client.key);
  });
});

server.listen(process.env.PORT || 8080, () => console.log("relay up"));