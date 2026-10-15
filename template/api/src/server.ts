// Entry point: serve the API, forge's probes and the built SPA on port 3000.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { registerRoutes } from "./generated/routes.js";
import { createServer, prisma } from "./lib/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = await createServer({ registerRoutes, webDist: path.resolve(here, "../../web/dist") });

let closing = false;
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, () => {
    if (closing) return;
    closing = true;
    app
      .close()
      .then(() => prisma.$disconnect())
      .catch((err: unknown) => app.log.error({ err }, "shutdown failed"))
      .finally(() => process.exit(0));
  });
}

await app.listen({ host: "0.0.0.0", port: Number(process.env.PORT ?? 3000) });
