// Generic handlers for list/get/create/update/delete/action operations, driven by literal configs.
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { type Client, type Delegate, type Tx, delegate, prisma, write } from "./db.js";
import { HttpError, notFound, registerEntityLabels, unprocessable } from "./errors.js";
import type { EntityConfig, FieldConfig, OperationConfig, OperationResult, Row } from "./types.js";
import { type IdType, type IdTypeOf, coerceLiteral, invalidMessage, parseId, readField, requiredMessage, serialize } from "./values.js";

/** Everything a hand-written action (api/src/stubs/<operationId>.ts) needs. */
export interface ActionContext {
  operation: OperationConfig;
  entity: EntityConfig;
  entities: Record<string, EntityConfig>;
  /** Raw path parameters, e.g. {"issue_id": "12"}. */
  params: Record<string, string>;
  /** Parsed JSON body ({} when there is none). */
  body: Row;
  /** Parsed id of the row named by the path, or null when the path names no row of this entity. */
  id: number | string | null;
  /** That row in wire shape (null when ``id`` is null). */
  record: Row | null;
  /** Read-only use. Every write must go through ``write`` so FORGE_MUTANT=rollback-writes can roll it back. */
  prisma: typeof prisma;
  write: <T>(fn: (tx: Tx) => Promise<T>) => Promise<T>;
  /** Prisma delegate for an entity by spec name, e.g. ``ctx.model(tx, "issue").update(...)``. */
  model: (client: Client, entity: string) => Delegate;
  /** Wire shape of a row of the given entity. */
  serialize: (entity: string, row: Row) => Row;
  /** A success response; the status defaults to the operation's success status. */
  ok: (body?: unknown, status?: number) => OperationResult;
  /** Abort with {"error": message}. */
  fail: (status: number, message: string) => never;
  notImplemented: () => OperationResult;
}

export type StubHandler = (ctx: ActionContext) => OperationResult | Promise<OperationResult>;

interface Call {
  params: Record<string, string>;
  body: unknown;
  /** Query-string values (only list operations with a search parameter read them). */
  query?: Record<string, unknown>;
}

interface Scope {
  /** Foreign-key filters implied by parent path parameters. */
  where: Row;
  /** Parsed id of the operation's own row ("self" path parameter). */
  selfId: number | string | null;
}

function bodyObject(body: unknown): Row {
  return body !== null && typeof body === "object" && !Array.isArray(body) ? (body as Row) : {};
}

function fieldOf(entity: EntityConfig, name: string): FieldConfig | undefined {
  return entity.fields.find((f) => f.name === name);
}

export function fastifyPath(template: string): string {
  return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, ":$1");
}

function wantsHtml(request: FastifyRequest): boolean {
  return (request.headers.accept ?? "").includes("text/html");
}

class Operation {
  private readonly entity: EntityConfig;
  private readonly idTypeOf: IdTypeOf;

  constructor(
    private readonly entities: Record<string, EntityConfig>,
    private readonly op: OperationConfig,
    private readonly stub: StubHandler | undefined,
  ) {
    const entity = entities[op.entity];
    if (!entity) throw new Error(`operation ${op.id}: unknown entity ${op.entity}`);
    this.entity = entity;
    this.idTypeOf = (name) => (name && entities[name] ? (entities[name].idType as IdType) : null);
    for (const p of op.pathParams) {
      if (!entities[p.entity]) throw new Error(`operation ${op.id}: unknown entity ${p.entity} for {${p.name}}`);
    }
    for (const set of op.actionSets) {
      const field = fieldOf(entity, set.field);
      if (!field) throw new Error(`operation ${op.id}: unknown field ${set.field}`);
      coerceLiteral(field, set.value, this.idTypeOf); // fail at startup, not on the first request
    }
  }

  async run(call: Call): Promise<OperationResult> {
    switch (this.op.kind) {
      case "list":
        return this.list(call);
      case "get":
        return this.get(call);
      case "create":
        return this.create(call);
      case "update":
        return this.update(call);
      case "delete":
        return this.remove(call);
      case "action":
        return this.action(call);
    }
  }

  // ---- shared steps ----
  private async scope(params: Record<string, string>): Promise<Scope> {
    const where: Row = {};
    let selfId: number | string | null = null;
    for (const p of this.op.pathParams) {
      const target = this.entities[p.entity];
      const id = parseId(target.idType, params[p.name]);
      if (id === null) throw notFound();
      if (p.role === "self") {
        selfId = id;
        continue;
      }
      const parent = await delegate(prisma, target).findUnique({ where: { [target.idField]: id } });
      if (!parent) throw notFound();
      if (p.field) where[p.field] = id;
    }
    return { where, selfId };
  }

  private async loadSelf(scope: Scope): Promise<Row> {
    if (scope.selfId === null) throw notFound();
    const row = await delegate(prisma, this.entity).findFirst({
      where: { ...scope.where, [this.entity.idField]: scope.selfId },
    });
    if (!row) throw notFound();
    return row;
  }

  private readInputs(body: Row, mode: "create" | "patch", fromPath: Set<string>): Row {
    const data: Row = {};
    for (const name of this.op.requestFields) {
      const field = fieldOf(this.entity, name);
      if (!field || field.generated !== "none" || fromPath.has(name)) continue;
      const input = readField(field, body, mode, this.idTypeOf);
      if (input.kind === "error") throw unprocessable(input.message);
      if (input.kind === "value") data[name] = input.value;
    }
    return data;
  }

  private async checkReferences(data: Row, fromPath: Set<string>): Promise<void> {
    for (const field of this.entity.fields) {
      if (field.type !== "reference" || !field.references || fromPath.has(field.name)) continue;
      const value = data[field.name];
      if (value === undefined || value === null) continue;
      const target = this.entities[field.references];
      const found = await delegate(prisma, target).findUnique({ where: { [target.idField]: value } });
      if (!found) throw unprocessable(invalidMessage(field));
    }
  }

  private respond(row: Row): OperationResult {
    const { response, successStatus, listKey } = this.op;
    if (response === "empty" || successStatus === 204) return { status: successStatus };
    const obj = serialize(this.entity, row);
    if (response === "list") return { status: successStatus, body: [obj] };
    if (response === "list_envelope") return { status: successStatus, body: { [listKey ?? "items"]: [obj] } };
    return { status: successStatus, body: obj };
  }

  private idWhere(row: Row): Row {
    return { [this.entity.idField]: row[this.entity.idField] };
  }

  // ---- kinds ----
  private async list(call: Call): Promise<OperationResult> {
    const scope = await this.scope(call.params);
    const search = this.op.search;
    const raw = search ? call.query?.[search.param] : undefined;
    const term = typeof raw === "string" ? raw.trim() : "";
    const where =
      search && term && search.fields.length
        ? { AND: [scope.where, { OR: search.fields.map((f) => ({ [f]: { contains: term, mode: "insensitive" } })) }] }
        : scope.where;
    const rows = await delegate(prisma, this.entity).findMany({
      where,
      orderBy: [{ [this.entity.orderBy]: "desc" }],
    });
    const items = rows.map((row) => serialize(this.entity, row));
    const { response, successStatus, listKey } = this.op;
    if (response === "list_envelope") return { status: successStatus, body: { [listKey ?? "items"]: items } };
    return { status: successStatus, body: items };
  }

  private async get(call: Call): Promise<OperationResult> {
    const row = await this.loadSelf(await this.scope(call.params));
    return { status: this.op.successStatus, body: serialize(this.entity, row) };
  }

  private async create(call: Call): Promise<OperationResult> {
    const scope = await this.scope(call.params);
    const fromPath = new Set(Object.keys(scope.where));
    const data: Row = { ...this.readInputs(bodyObject(call.body), "create", fromPath), ...scope.where };
    for (const field of this.entity.fields) {
      if (field.generated !== "none" || data[field.name] !== undefined) continue;
      if (field.default !== null) data[field.name] = coerceLiteral(field, field.default, this.idTypeOf);
      else if (field.required) throw unprocessable(requiredMessage(field));
    }
    await this.checkReferences(data, fromPath);
    const row = await write((tx) => delegate(tx, this.entity).create({ data }));
    return this.respond(row);
  }

  private async update(call: Call): Promise<OperationResult> {
    const scope = await this.scope(call.params);
    const row = await this.loadSelf(scope);
    const fromPath = new Set(Object.keys(scope.where));
    const data = this.readInputs(bodyObject(call.body), "patch", fromPath);
    await this.checkReferences(data, fromPath);
    const updated = await write((tx) => delegate(tx, this.entity).update({ where: this.idWhere(row), data }));
    return this.respond(updated);
  }

  private async remove(call: Call): Promise<OperationResult> {
    const row = await this.loadSelf(await this.scope(call.params));
    const deleted = await write((tx) => delegate(tx, this.entity).delete({ where: this.idWhere(row) }));
    return this.respond(deleted);
  }

  private async action(call: Call): Promise<OperationResult> {
    const scope = await this.scope(call.params);
    const row = scope.selfId === null ? null : await this.loadSelf(scope);
    if (this.op.stub) {
      const ctx = this.context(call, scope, row);
      return this.stub ? this.stub(ctx) : ctx.notImplemented();
    }
    if (!row) throw new HttpError(501, `Not implemented: ${this.op.id}`);
    const fromPath = new Set(Object.keys(scope.where));
    const data = this.readInputs(bodyObject(call.body), "patch", fromPath);
    for (const set of this.op.actionSets) {
      data[set.field] = coerceLiteral(fieldOf(this.entity, set.field)!, set.value, this.idTypeOf);
    }
    await this.checkReferences(data, fromPath);
    const updated = Object.keys(data).length
      ? await write((tx) => delegate(tx, this.entity).update({ where: this.idWhere(row), data }))
      : row;
    return this.respond(updated);
  }

  private context(call: Call, scope: Scope, row: Row | null): ActionContext {
    const op = this.op;
    const entityNamed = (name: string): EntityConfig => {
      const entity = this.entities[name];
      if (!entity) throw new Error(`unknown entity ${name}`);
      return entity;
    };
    return {
      operation: op,
      entity: this.entity,
      entities: this.entities,
      params: call.params,
      body: bodyObject(call.body),
      id: scope.selfId,
      record: row ? serialize(this.entity, row) : null,
      prisma,
      write,
      model: (client, name) => delegate(client, entityNamed(name)),
      serialize: (name, r) => serialize(entityNamed(name), r),
      ok: (body, status = op.successStatus) => ({ status, body }),
      fail: (status, message) => {
        throw new HttpError(status, message);
      },
      notImplemented: () => ({ status: 501, body: { error: `Not implemented: ${op.id}` } }),
    };
  }
}

async function send(reply: FastifyReply, result: OperationResult): Promise<FastifyReply> {
  reply.code(result.status);
  if (result.body === undefined || result.status === 204 || result.status === 304) return reply.send();
  return reply.send(result.body);
}

/** Register one operation as a Fastify route. */
export function registerOperation(
  app: FastifyInstance,
  entities: Record<string, EntityConfig>,
  op: OperationConfig,
  stub?: StubHandler,
): void {
  registerEntityLabels(entities);
  const operation = new Operation(entities, op, stub);
  app.route({
    method: op.method,
    url: fastifyPath(op.path),
    handler: async (request, reply) => {
      if (op.htmlFallback && request.method === "GET" && wantsHtml(request) && typeof reply.sendFile === "function") {
        return reply.header("cache-control", "no-cache").type("text/html; charset=utf-8").sendFile("index.html");
      }
      const result = await operation.run({
        params: request.params as Record<string, string>,
        body: request.body,
        query: (request.query ?? {}) as Record<string, unknown>,
      });
      return send(reply, result);
    },
  });
}
