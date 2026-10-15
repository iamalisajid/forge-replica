// Fake replica for verify tests: the fixture items app behind the forge stack contract.
// /__forge/health, /__forge/routes (FORGE_TEST_MODE=1), FORGE_MUTANT=rollback-writes, data on a volume.
import fs from "node:fs";
import http from "node:http";

const DATA = process.env.DATA_FILE ?? "/data/items.json";
const MUTANT = process.env.FORGE_MUTANT === "rollback-writes";
const TEST_MODE = process.env.FORGE_TEST_MODE === "1";
const html = fs.readFileSync(new URL("./app.html", import.meta.url));
const ROUTES = [["GET", "/api/items"], ["POST", "/api/items"], ["GET", "/api/items/:item_id"], ["DELETE", "/api/items/:item_id"]];

const load = () => { try { return JSON.parse(fs.readFileSync(DATA, "utf8")); } catch { return { next: 1, items: [] }; } };
const save = (db) => { if (!MUTANT) fs.writeFileSync(DATA, JSON.stringify(db)); };  // the mutant forgets every write

function send(res, status, body, type = "application/json") {
  const data = type === "application/json" ? (body === undefined ? "" : JSON.stringify(body)) : body;
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(data);
}

function readJson(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => { try { resolve(JSON.parse(raw || "{}")); } catch { resolve({}); } });
  });
}

http.createServer(async (req, res) => {
  const path = new URL(req.url, "http://replica").pathname;
  if (path === "/__forge/health") return send(res, 200, { status: "ok" });
  if (path === "/__forge/routes" && TEST_MODE) return send(res, 200, ROUTES.map(([method, url]) => ({ method, url })));
  if (path === "/api/items" && req.method === "GET") return send(res, 200, { items: [...load().items].reverse() });
  if (path === "/api/items" && req.method === "POST") {
    const name = String((await readJson(req)).name ?? "").trim();
    if (!name) return send(res, 422, { error: "Name is required" });
    const db = load();
    const item = { id: db.next++, name };
    db.items.push(item);
    save(db);
    return send(res, 201, item);
  }
  const match = path.match(/^\/api\/items\/(\d+)$/);
  if (match && req.method === "GET") {
    const item = load().items.find((i) => i.id === Number(match[1]));
    return item ? send(res, 200, item) : send(res, 404, { error: "Not found" });
  }
  if (match && req.method === "DELETE") {
    const db = load();
    const before = db.items.length;
    db.items = db.items.filter((i) => i.id !== Number(match[1]));
    if (db.items.length === before) return send(res, 404, { error: "Not found" });
    save(db);
    return send(res, 204);
  }
  if (path.startsWith("/api/")) return send(res, 404, { error: "Not found" });
  return send(res, 200, html, "text/html; charset=utf-8");
}).listen(3000, "0.0.0.0");
