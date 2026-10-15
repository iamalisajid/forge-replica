// Config shapes for the generated route table (api/src/generated/routes.ts).
// Codegen writes these as literal objects; the library turns them into Fastify handlers.

export type FieldType =
  | "string"
  | "text"
  | "integer"
  | "number"
  | "boolean"
  | "datetime"
  | "date"
  | "enum"
  | "reference";

export type Generated = "none" | "id" | "created_at" | "updated_at";

export interface FieldConfig {
  /** Wire name, reused as the Prisma field name. */
  name: string;
  /** Used in error messages: "<label> is required". */
  label: string;
  type: FieldType;
  required: boolean;
  unique: boolean;
  enumValues: string[];
  /** Entity name when type is "reference". */
  references: string | null;
  /** Literal default from the spec, coerced to the field type. */
  default: string | null;
  generated: Generated;
}

export interface EntityConfig {
  name: string;
  label: string;
  /** Prisma client delegate, e.g. "issue" for model Issue. */
  delegate: string;
  idField: string;
  idType: "integer" | "uuid";
  /** Lists are returned newest first: ordered by this field, descending. */
  orderBy: string;
  fields: FieldConfig[];
}

export type OperationKind = "list" | "get" | "create" | "update" | "delete" | "action";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type ResponseKind = "object" | "list" | "list_envelope" | "empty";

export interface PathParamConfig {
  /** Name in the path template, e.g. "project_id". */
  name: string;
  /** Entity whose id fills it. */
  entity: string;
  /** "self" identifies the operation's own row; "parent" scopes it under another entity. */
  role: "self" | "parent";
  /** For "parent": the foreign-key field on the operation's entity that points at the parent. */
  field: string | null;
}

export interface ActionSet {
  field: string;
  value: string;
}

export interface OperationConfig {
  id: string;
  kind: OperationKind;
  method: HttpMethod;
  /** OpenAPI-style template, e.g. "/api/projects/{project_id}/issues". */
  path: string;
  entity: string;
  pathParams: PathParamConfig[];
  requestFields: string[];
  response: ResponseKind;
  listKey: string | null;
  successStatus: number;
  actionSets: ActionSet[];
  /** True when the behaviour lives in api/src/stubs/<id>.ts (written by the builder). */
  stub: boolean;
  /** True when a browser navigation to this GET path must get the SPA shell (the path is also a screen route). */
  htmlFallback: boolean;
  /** List operations: the query parameter that filters by text, and the text fields it searches. */
  search: { param: string; fields: string[] } | null;
}

export type Row = Record<string, unknown>;

/** What a handler (generic or stub) produces. ``body`` undefined means an empty response. */
export interface OperationResult {
  status: number;
  body?: unknown;
}
