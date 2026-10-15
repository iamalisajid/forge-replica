// Prop shapes shared by the generated screens and the library components.

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

export type OperationKind = "list" | "get" | "create" | "update" | "delete" | "action";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type ResponseKind = "object" | "list" | "list_envelope" | "empty";

/** One API operation as the browser calls it (web/src/generated/contract.ts). */
export interface OperationWire {
  id: string;
  kind: OperationKind;
  method: HttpMethod;
  /** OpenAPI-style template, e.g. "/api/projects/{project_id}". */
  path: string;
  entity: string;
  pathParams: string[];
  requestFields: string[];
  response: ResponseKind;
  listKey: string | null;
  successStatus: number;
  actionSets: { field: string; value: string }[];
}

export type Row = Record<string, unknown>;

/** Where a path or route parameter comes from. */
export type ParamSource =
  | { from: "route"; name: string }
  | { from: "record"; field: string }
  | { from: "item"; field: string }
  | { from: "result"; field: string };

export type ParamMap = Record<string, ParamSource>;

/** A screen route (react-router syntax, e.g. "/projects/:project_id") and how to fill its parameters. */
export interface RouteTarget {
  path: string;
  params: ParamMap;
}

export type Control = "text" | "textarea" | "number" | "select" | "checkbox" | "date" | "datetime";

/** Options for a reference select, loaded from a list operation. */
export interface OptionsSource {
  operation: string;
  params: ParamMap;
  valueField: string;
  labelField: string;
}

export interface FieldInput {
  name: string;
  label: string;
  control: Control;
  type: FieldType;
  options: string[];
  optionsFrom: OptionsSource | null;
  required: boolean;
  default: string | null;
}

/** Contents of a create or edit form, in the original's order. */
export type FormItem =
  | { kind: "field"; field: FieldInput }
  | { kind: "alert" }
  | { kind: "status"; text: string }
  | { kind: "text"; text: string }
  | { kind: "submit"; label: string }
  | { kind: "cancel"; label: string }
  | { kind: "delete"; label: string; operation: string; params: ParamMap; navigateTo: RouteTarget | null }
  | { kind: "action"; label: string; operation: string; params: ParamMap }
  | { kind: "link"; label: string; to: RouteTarget };

/** One part of a list row or table row, in the original's order. */
export type RowPart =
  | { kind: "link"; field: string | null; text: string; header: string; to: RouteTarget }
  | { kind: "text"; field: string | null; text: string; header: string; badge: boolean }
  | { kind: "action"; label: string; operation: string; params: ParamMap; hideWhenApplied: boolean }
  | { kind: "delete"; label: string; operation: string; params: ParamMap };

export interface RecordSource {
  operation: string;
  params: ParamMap;
}
