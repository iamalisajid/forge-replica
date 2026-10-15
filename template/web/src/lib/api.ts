// A small fetch client for the generated operations. Errors carry the API's {"error": "..."} message.
import type { OperationWire, Row } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Fill an OpenAPI-style path template ("/api/projects/{project_id}"). */
export function fillPath(template: string, values: Record<string, string>): string {
  return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_match, name: string) => {
    const value = values[name];
    if (value === undefined) throw new Error(`Missing path parameter "${name}" for ${template}`);
    return encodeURIComponent(value);
  });
}

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "error" in body && typeof (body as Row).error === "string") {
    return (body as Row).error as string;
  }
  return `Request failed (${status})`;
}

export async function request(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, credentials: "same-origin", signal };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const text = res.status === 204 ? "" : await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(errorMessage(data, res.status), res.status, data);
  return data;
}

export function callOperation(
  op: OperationWire,
  params: Record<string, string>,
  body?: Row,
  signal?: AbortSignal,
): Promise<unknown> {
  return request(op.method, fillPath(op.path, params), body, signal);
}

/** The rows of a list response, whether bare or wrapped in {<listKey>: [...]}. */
export function listItems(op: OperationWire, data: unknown): Row[] {
  const list = op.response === "list_envelope" && data && typeof data === "object" ? (data as Row)[op.listKey ?? "items"] : data;
  return Array.isArray(list) ? (list as Row[]) : [];
}

export function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong";
}
