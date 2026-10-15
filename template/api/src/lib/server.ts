// The Fastify app: JSON errors, same-origin writes, forge's health and route probes, and the built SPA.
import { existsSync } from "node:fs";
import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import { MUTANT_ENV, ROLLBACK_WRITES, mutant, prisma } from "./db.js";
import { mapError } from "./errors.js";

export const HEALTH_PATH = "/__forge/health";
export const ROUTES_PATH = "/__forge/routes";
export const TEST_MODE_ENV = "FORGE_TEST_MODE";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export interface RouteEntry {
  method: string;
  path: string;
}

export interface ServerOptions {
  /** Registers the generated operation routes. */
  registerRoutes: (app: FastifyInstance) => void;
  /** Directory of the built SPA (web/dist); skipped when it does not exist. */
  webDist?: string;
  logger?: boolean;
}

function isInternal(url: string): boolean {
  return url.startsWith("/__forge/") || url === "/*";
}

function openApiPath(url: string): string {
  return url.replace(/:([A-Za-z_][A-Za-z0-9_]*)/g, "{$1}");
}

function isApiLike(url: string): boolean {
  const path = url.split("?", 1)[0];
  return path === "/api" || path.startsWith("/api/") || path.startsWith("/__forge/");
}

/** Reject writes from other origins. Browsers always send Origin on cross-origin non-GET requests. */
async function sameOriginWrites(request: FastifyRequest, reply: FastifyReply): Promise<void> {
  if (SAFE_METHODS.has(request.method)) return;
  const origin = request.headers.origin;
  if (origin === undefined) return;
  if (origin !== `${request.protocol}://${request.headers.host ?? ""}`) {
    await reply.code(403).send({ error: "Cross-origin request blocked" });
  }
}

export async function createServer(options: ServerOptions): Promise<FastifyInstance> {
  const app = Fastify({
    logger: options.logger ?? { level: process.env.LOG_LEVEL ?? "info" },
    bodyLimit: 1024 * 1024,
  });

  const mode = mutant();
  if (mode === ROLLBACK_WRITES) app.log.warn(`${MUTANT_ENV}=${mode}: every write is rolled back (negative control)`);
  else if (mode) app.log.warn(`${MUTANT_ENV}=${mode} is not a known mode and is ignored`);

  // Every registered route, for the test-mode route probe (OpenAPI-style paths).
  const routes: RouteEntry[] = [];
  app.addHook("onRoute", (route) => {
    const url = route.url ?? (route as { path?: string }).path ?? "";
    if (isInternal(url)) return;
    const methods = Array.isArray(route.method) ? route.method : [route.method];
    for (const method of methods) {
      if (method === "HEAD") continue;
      routes.push({ method, path: openApiPath(url) });
    }
  });

  // JSON only. An empty JSON body counts as no body, so DELETE/POST without a payload always works.
  const defaultJson = app.getDefaultJsonParser("error", "error");
  app.removeAllContentTypeParsers();
  app.addContentTypeParser("application/json", { parseAs: "string" }, (request, body, done) => {
    const text = typeof body === "string" ? body : body.toString("utf8");
    if (text.trim() === "") {
      done(null, undefined);
      return;
    }
    defaultJson(request, text, done);
  });

  app.addHook("onRequest", sameOriginWrites);

  app.setErrorHandler((err, request, reply) => {
    const mapped = mapError(err);
    if (mapped.status >= 500) request.log.error({ err }, "request failed");
    return reply.code(mapped.status).send({ error: mapped.message });
  });

  app.get(HEALTH_PATH, async (_request, reply) => {
    try {
      await prisma.$queryRaw`SELECT 1`;
      return { status: "ok" };
    } catch (err) {
      app.log.error({ err }, "health check: database unavailable");
      return reply.code(503).send({ status: "unavailable" });
    }
  });

  if (process.env[TEST_MODE_ENV] === "1") {
    app.get(ROUTES_PATH, async () =>
      [...routes].sort((a, b) => (a.path === b.path ? a.method.localeCompare(b.method) : a.path < b.path ? -1 : 1)),
    );
  }

  options.registerRoutes(app);

  const webDist = options.webDist && existsSync(options.webDist) ? options.webDist : null;
  if (webDist) {
    await app.register(fastifyStatic, { root: webDist, wildcard: true, index: ["index.html"] });
  }

  // SPA fallback: browser paths get the app shell; API paths and non-GET requests get JSON 404s.
  app.setNotFoundHandler((request, reply) => {
    const isRead = request.method === "GET" || request.method === "HEAD";
    if (webDist && isRead && !isApiLike(request.url)) {
      return reply.header("cache-control", "no-cache").type("text/html; charset=utf-8").sendFile("index.html");
    }
    return reply.code(404).send({ error: "Not found" });
  });

  return app;
}
