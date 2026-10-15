// Errors are always JSON: {"error": "<message>"}.
import { Prisma } from "@prisma/client";
import type { EntityConfig } from "./types.js";

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

export const notFound = (): HttpError => new HttpError(404, "Not found");

export const unprocessable = (message: string): HttpError => new HttpError(422, message);

/** Labels for unique-constraint errors, keyed by Prisma field name. */
const uniqueLabels = new Map<string, string>();

export function registerEntityLabels(entities: Record<string, EntityConfig>): void {
  for (const entity of Object.values(entities)) {
    for (const field of entity.fields) uniqueLabels.set(field.name, field.label);
  }
}

function uniqueMessage(target: unknown): string {
  const names = Array.isArray(target) ? target.map(String) : typeof target === "string" ? [target] : [];
  const labels = names.map((name) => uniqueLabels.get(name)).filter((label): label is string => Boolean(label));
  return labels.length ? `${labels.join(", ")} already exists` : "Already exists";
}

export interface MappedError {
  status: number;
  message: string;
}

/** Translate any thrown value into an HTTP status and a client-safe message. */
export function mapError(err: unknown): MappedError {
  if (err instanceof HttpError) return { status: err.status, message: err.message };
  if (err instanceof Prisma.PrismaClientKnownRequestError) {
    switch (err.code) {
      case "P2025":
        return { status: 404, message: "Not found" };
      case "P2002":
        return { status: 409, message: uniqueMessage(err.meta?.target) };
      case "P2003":
        return { status: 422, message: "Related record not found" };
      case "P2000":
        return { status: 422, message: "Value is too long" };
      default:
        return { status: 500, message: "Internal server error" };
    }
  }
  if (err instanceof Prisma.PrismaClientValidationError) return { status: 400, message: "Invalid request" };
  const status = (err as { statusCode?: unknown } | null)?.statusCode;
  if (typeof status === "number" && status >= 400 && status < 500) {
    return { status, message: err instanceof Error ? err.message : "Bad request" };
  }
  return { status: 500, message: "Internal server error" };
}
