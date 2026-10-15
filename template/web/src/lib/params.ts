// Resolve path and route parameters from the current route, the screen's record, a list row or a write result.
import type { ParamMap, RouteTarget, Row } from "./types";

export interface ParamContext {
  route?: Readonly<Record<string, string | undefined>>;
  record?: Row | null;
  item?: Row | null;
  result?: Row | null;
}

function text(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  return String(value);
}

/** All parameters as strings, or null while any of them is not available yet. */
export function resolveParams(params: ParamMap, ctx: ParamContext): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const [name, source] of Object.entries(params)) {
    let value: string | undefined;
    switch (source.from) {
      case "route":
        value = ctx.route?.[source.name];
        break;
      case "record":
        value = text(ctx.record?.[source.field]);
        break;
      case "item":
        value = text(ctx.item?.[source.field]);
        break;
      case "result":
        value = text(ctx.result?.[source.field]);
        break;
    }
    if (value === undefined) return null;
    out[name] = value;
  }
  return out;
}

/** Fill a react-router path ("/projects/:project_id"); null while a parameter is missing. */
export function buildRoute(target: RouteTarget, ctx: ParamContext): string | null {
  const values = resolveParams(target.params, ctx);
  if (!values) return null;
  let missing = false;
  const path = target.path.replace(/:([A-Za-z_][A-Za-z0-9_]*)/g, (_match, name: string) => {
    const value = values[name];
    if (value === undefined) {
      missing = true;
      return "";
    }
    return encodeURIComponent(value);
  });
  return missing ? null : path;
}

/** How a field value reads on screen: the raw wire value, as the original shows it. */
export function display(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
