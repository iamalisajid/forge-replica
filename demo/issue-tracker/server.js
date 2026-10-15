// Demo target for forge: projects and issues over a small REST API, served with a vanilla-JS SPA.
// Deliberately a different stack from the replica template (Express + node:sqlite, no ORM, no React).
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import express from "express";

const here = path.dirname(fileURLToPath(import.meta.url));
const db = new DatabaseSync(process.env.DB_PATH ?? path.join(here, "tracker.sqlite3"));
db.exec(`
  PRAGMA foreign_keys = ON;
  CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'closed')),
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created_at TEXT NOT NULL);
`);

const AUTH = process.env.AUTH === "on";
const USER = {
  email: process.env.DEMO_EMAIL ?? "demo@example.test",
  password: process.env.DEMO_PASSWORD ?? "demo-password",
};
const STATUSES = ["open", "in_progress", "closed"];
const PRIORITIES = ["low", "medium", "high"];
const now = () => new Date().toISOString();
const text = (value) => (typeof value === "string" ? value.trim() : "");

const getProject = (id) => db.prepare("SELECT * FROM projects WHERE id = ?").get(id);
const getIssue = (id) => db.prepare("SELECT * FROM issues WHERE id = ?").get(id);
const invalid = (res, error) => res.status(422).json({ error });
const notFound = (res) => res.status(404).json({ error: "Not found" });

function sessionToken(req) {
  const match = (req.headers.cookie ?? "").match(/(?:^|;\s*)sid=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function signedIn(req) {
  const token = sessionToken(req);
  return Boolean(token && db.prepare("SELECT 1 FROM sessions WHERE token = ?").get(token));
}

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "100kb" }));
app.use(express.static(path.join(here, "public"), { index: false }));

// Same-origin check for writes (the browser always sends Origin on cross-origin requests).
app.use("/api", (req, res, next) => {
  const origin = req.headers.origin;
  if (req.method !== "GET" && origin && origin !== `${req.protocol}://${req.headers.host}`) {
    return res.status(403).json({ error: "Cross-origin request blocked" });
  }
  next();
});

app.get("/api/config", (req, res) => res.json({ auth: AUTH }));

app.post("/api/login", (req, res) => {
  if (text(req.body?.email) !== USER.email || req.body?.password !== USER.password) {
    return res.status(401).json({ error: "Invalid email or password" });
  }
  const token = crypto.randomUUID();
  db.prepare("INSERT INTO sessions (token, created_at) VALUES (?, ?)").run(token, now());
  res.setHeader("Set-Cookie", `sid=${token}; HttpOnly; SameSite=Lax; Path=/`);
  res.json({ email: USER.email });
});

app.post("/api/logout", (req, res) => {
  const token = sessionToken(req);
  if (token) db.prepare("DELETE FROM sessions WHERE token = ?").run(token);
  res.setHeader("Set-Cookie", "sid=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0");
  res.status(204).end();
});

app.get("/api/me", (req, res) => {
  if (AUTH && !signedIn(req)) return res.status(401).json({ error: "Sign in required" });
  res.json({ email: AUTH ? USER.email : null });
});

app.use("/api", (req, res, next) => {
  if (AUTH && !signedIn(req)) return res.status(401).json({ error: "Sign in required" });
  next();
});

// ---- Projects ----
app.get("/api/projects", (req, res) => {
  res.json({ items: db.prepare("SELECT * FROM projects ORDER BY id DESC").all() });
});

app.post("/api/projects", (req, res) => {
  const name = text(req.body?.name);
  if (!name) return invalid(res, "Name is required");
  if (name.length > 120) return invalid(res, "Name must be 120 characters or fewer");
  const result = db.prepare("INSERT INTO projects (name, created_at) VALUES (?, ?)").run(name, now());
  res.status(201).json(getProject(result.lastInsertRowid));
});

app.get("/api/projects/:id", (req, res) => {
  const project = getProject(Number(req.params.id));
  return project ? res.json(project) : notFound(res);
});

app.patch("/api/projects/:id", (req, res) => {
  const project = getProject(Number(req.params.id));
  if (!project) return notFound(res);
  const name = text(req.body?.name);
  if (!name) return invalid(res, "Name is required");
  db.prepare("UPDATE projects SET name = ? WHERE id = ?").run(name, project.id);
  res.json(getProject(project.id));
});

app.delete("/api/projects/:id", (req, res) => {
  const result = db.prepare("DELETE FROM projects WHERE id = ?").run(Number(req.params.id));
  return result.changes ? res.status(204).end() : notFound(res);
});

// ---- Issues ----
app.get("/api/projects/:id/issues", (req, res) => {
  const project = getProject(Number(req.params.id));
  if (!project) return notFound(res);
  res.json({ items: db.prepare("SELECT * FROM issues WHERE project_id = ? ORDER BY id DESC").all(project.id) });
});

app.post("/api/projects/:id/issues", (req, res) => {
  const project = getProject(Number(req.params.id));
  if (!project) return notFound(res);
  const title = text(req.body?.title);
  const status = req.body?.status ?? "open";
  const priority = req.body?.priority ?? "medium";
  if (!title) return invalid(res, "Title is required");
  if (title.length > 200) return invalid(res, "Title must be 200 characters or fewer");
  if (!STATUSES.includes(status)) return invalid(res, "Status is not valid");
  if (!PRIORITIES.includes(priority)) return invalid(res, "Priority is not valid");
  const ts = now();
  const result = db
    .prepare("INSERT INTO issues (project_id, title, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    .run(project.id, title, status, priority, ts, ts);
  res.status(201).json(getIssue(result.lastInsertRowid));
});

app.get("/api/issues/:id", (req, res) => {
  const issue = getIssue(Number(req.params.id));
  return issue ? res.json(issue) : notFound(res);
});

app.patch("/api/issues/:id", (req, res) => {
  const issue = getIssue(Number(req.params.id));
  if (!issue) return notFound(res);
  const next = { ...issue };
  if ("title" in (req.body ?? {})) {
    next.title = text(req.body.title);
    if (!next.title) return invalid(res, "Title is required");
  }
  if ("status" in (req.body ?? {})) {
    if (!STATUSES.includes(req.body.status)) return invalid(res, "Status is not valid");
    next.status = req.body.status;
  }
  if ("priority" in (req.body ?? {})) {
    if (!PRIORITIES.includes(req.body.priority)) return invalid(res, "Priority is not valid");
    next.priority = req.body.priority;
  }
  db.prepare("UPDATE issues SET title = ?, status = ?, priority = ?, updated_at = ? WHERE id = ?")
    .run(next.title, next.status, next.priority, now(), issue.id);
  res.json(getIssue(issue.id));
});

app.post("/api/issues/:id/close", (req, res) => {
  const issue = getIssue(Number(req.params.id));
  if (!issue) return notFound(res);
  db.prepare("UPDATE issues SET status = 'closed', updated_at = ? WHERE id = ?").run(now(), issue.id);
  res.json(getIssue(issue.id));
});

app.delete("/api/issues/:id", (req, res) => {
  const result = db.prepare("DELETE FROM issues WHERE id = ?").run(Number(req.params.id));
  return result.changes ? res.status(204).end() : notFound(res);
});

app.use("/api", (req, res) => notFound(res));

// SPA fallback: every non-API path serves the app shell.
app.get(/^(?!\/api\/).*/, (req, res) => res.sendFile(path.join(here, "public", "index.html")));

const port = Number(process.env.PORT ?? 4001);
app.listen(port, "0.0.0.0", () => console.log(`demo issue tracker on :${port} (auth ${AUTH ? "on" : "off"})`));
