// Field rules: parse request values, coerce spec literals (defaults, action effects) and shape rows for the wire.
import type { EntityConfig, FieldConfig, Row } from "./types.js";

const INT_MIN = -2147483648;
const INT_MAX = 2147483647;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:$|T)/;

export const requiredMessage = (field: FieldConfig): string => `${field.label} is required`;
export const invalidMessage = (field: FieldConfig): string => `${field.label} is not valid`;

const INVALID: unique symbol = Symbol("invalid");
type Parsed = unknown;

export type IdType = "integer" | "uuid";
export type IdTypeOf = (entity: string | null) => IdType | null;

/** Parse an id (path segment or body value). Returns null when it cannot be an id of that type. */
export function parseId(idType: IdType, raw: unknown): number | string | null {
  if (idType === "integer") {
    const n = parseInteger(raw);
    return n === INVALID ? null : (n as number);
  }
  return typeof raw === "string" && UUID_RE.test(raw) ? raw.toLowerCase() : null;
}

function parseInteger(raw: unknown): number | typeof INVALID {
  let n: number;
  if (typeof raw === "number") n = raw;
  else if (typeof raw === "string" && /^[-+]?\d{1,10}$/.test(raw.trim())) n = Number(raw.trim());
  else return INVALID;
  return Number.isInteger(n) && n >= INT_MIN && n <= INT_MAX ? n : INVALID;
}

function parseNumber(raw: unknown): number | typeof INVALID {
  if (typeof raw === "number") return Number.isFinite(raw) ? raw : INVALID;
  if (typeof raw === "string" && raw.trim() !== "") {
    const n = Number(raw.trim());
    return Number.isFinite(n) ? n : INVALID;
  }
  return INVALID;
}

function parseBoolean(raw: unknown): boolean | typeof INVALID {
  if (typeof raw === "boolean") return raw;
  if (raw === 1 || raw === 0) return raw === 1;
  if (typeof raw === "string") {
    const s = raw.trim().toLowerCase();
    if (s === "true" || s === "1") return true;
    if (s === "false" || s === "0") return false;
  }
  return INVALID;
}

function parseDateTime(raw: unknown): Date | typeof INVALID {
  if (typeof raw === "number" && Number.isFinite(raw)) return new Date(raw);
  if (typeof raw === "string" && raw.trim() !== "") {
    const ms = Date.parse(raw.trim());
    return Number.isNaN(ms) ? INVALID : new Date(ms);
  }
  return INVALID;
}

function parseDate(raw: unknown): Date | typeof INVALID {
  if (typeof raw !== "string") return INVALID;
  const m = DATE_RE.exec(raw.trim());
  if (!m) return INVALID;
  const date = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00.000Z`);
  // Reject dates that roll over, such as 2026-02-30.
  return Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== `${m[1]}-${m[2]}-${m[3]}` ? INVALID : date;
}

function parseValue(field: FieldConfig, raw: unknown, idTypeOf: IdTypeOf): Parsed | typeof INVALID {
  switch (field.type) {
    case "string":
      return typeof raw === "string" ? raw.trim() : INVALID;
    case "text":
      return typeof raw === "string" ? raw : INVALID;
    case "enum":
      return typeof raw === "string" && field.enumValues.includes(raw) ? raw : INVALID;
    case "integer":
      return parseInteger(raw);
    case "number":
      return parseNumber(raw);
    case "boolean":
      return parseBoolean(raw);
    case "datetime":
      return parseDateTime(raw);
    case "date":
      return parseDate(raw);
    case "reference": {
      const idType = idTypeOf(field.references);
      if (!idType) return INVALID;
      const id = parseId(idType, raw);
      return id === null ? INVALID : id;
    }
  }
}

export type Input = { kind: "skip" } | { kind: "value"; value: unknown } | { kind: "error"; message: string };

const SKIP: Input = { kind: "skip" };
const value = (v: unknown): Input => ({ kind: "value", value: v });
const error = (message: string): Input => ({ kind: "error", message });

/**
 * Read one request field from a JSON body.
 * "create" applies the spec default to missing values and enforces ``required``;
 * "patch" leaves missing fields untouched and only rejects clearing a required field.
 */
export function readField(field: FieldConfig, body: Row, mode: "create" | "patch", idTypeOf: IdTypeOf): Input {
  const present = Object.hasOwn(body, field.name) && body[field.name] !== undefined;
  const raw = present ? body[field.name] : undefined;
  const isText = field.type === "string" || field.type === "text";
  const blank = typeof raw === "string" && raw.trim() === "";
  // A blank value means "no value" for typed inputs, except that a required enum must name one of its values.
  const missing = raw === undefined || raw === null || (blank && !isText && !(field.type === "enum" && field.required));
  if (missing) {
    if (mode === "patch" && !present) return SKIP;
    if (mode === "create" && field.default !== null) return value(coerceLiteral(field, field.default, idTypeOf));
    if (field.required) return error(requiredMessage(field));
    return present ? value(null) : SKIP;
  }
  // Text fields treat a non-string value like a blank one (the common `typeof v === "string" ? v.trim() : ""`).
  if (isText && (typeof raw !== "string" || raw.trim() === "")) {
    return field.required ? error(requiredMessage(field)) : value(typeof raw === "string" && field.type === "text" ? raw : "");
  }
  const parsed = parseValue(field, raw, idTypeOf);
  return parsed === INVALID ? error(invalidMessage(field)) : value(parsed);
}

/** Coerce a literal from the spec (a default or an action effect) to the field's type. */
export function coerceLiteral(field: FieldConfig, literal: string, idTypeOf: IdTypeOf): unknown {
  const lower = literal.trim().toLowerCase();
  const isText = field.type === "string" || field.type === "text";
  if (!isText && !field.required && lower === "null") return null;
  if (field.type === "datetime" && lower === "now") return new Date();
  if (field.type === "date" && (lower === "today" || lower === "now")) {
    return new Date(`${new Date().toISOString().slice(0, 10)}T00:00:00.000Z`);
  }
  if (isText) return literal;
  const parsed = parseValue(field, literal, idTypeOf);
  if (parsed === INVALID) throw new Error(`Literal ${JSON.stringify(literal)} is not a valid ${field.type} for ${field.name}`);
  return parsed;
}

function toWire(field: FieldConfig, raw: unknown): unknown {
  if (raw === undefined || raw === null) return null;
  if (raw instanceof Date) return field.type === "date" ? raw.toISOString().slice(0, 10) : raw.toISOString();
  if (typeof raw === "bigint") return Number(raw);
  return raw;
}

/** Shape a database row for the wire: exactly the spec's fields, in spec order. */
export function serialize(entity: EntityConfig, row: Row): Row {
  const out: Row = {};
  for (const field of entity.fields) out[field.name] = toWire(field, row[field.name]);
  return out;
}
