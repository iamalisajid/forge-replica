// Database access. Every write in the app goes through ``write()`` so the negative control can turn
// writes off: with FORGE_MUTANT=rollback-writes each write runs in a transaction that is rolled back
// after its result is computed, so the API still answers exactly as if the write had happened.
import { Prisma, PrismaClient } from "@prisma/client";
import type { EntityConfig, Row } from "./types.js";

export const MUTANT_ENV = "FORGE_MUTANT";
export const ROLLBACK_WRITES = "rollback-writes";

export const prisma = new PrismaClient();

export type Tx = Prisma.TransactionClient;
export type Client = PrismaClient | Tx;

/** The subset of a Prisma model delegate the generic handlers use. */
export interface Delegate {
  findMany(args?: object): Promise<Row[]>;
  findFirst(args?: object): Promise<Row | null>;
  findUnique(args: object): Promise<Row | null>;
  create(args: object): Promise<Row>;
  update(args: object): Promise<Row>;
  delete(args: object): Promise<Row>;
}

export function delegate(client: Client, entity: EntityConfig): Delegate {
  const model = (client as unknown as Record<string, Delegate | undefined>)[entity.delegate];
  if (!model) throw new Error(`Prisma client has no model delegate "${entity.delegate}"`);
  return model;
}

export function mutant(): string | null {
  const value = process.env[MUTANT_ENV];
  return value ? value : null;
}

class Rollback<T> {
  constructor(readonly result: T) {}
}

/**
 * Run ``fn`` as one write transaction and return its result.
 * Under FORGE_MUTANT=rollback-writes the transaction is rolled back after ``fn`` resolves, and the
 * would-be result is still returned, so callers answer with their normal status code and body.
 */
export async function write<T>(fn: (tx: Tx) => Promise<T>): Promise<T> {
  if (process.env[MUTANT_ENV] !== ROLLBACK_WRITES) {
    return prisma.$transaction((tx) => fn(tx));
  }
  try {
    await prisma.$transaction(async (tx) => {
      throw new Rollback(await fn(tx));
    });
  } catch (err) {
    if (err instanceof Rollback) return err.result as T;
    throw err;
  }
  throw new Error("rollback-writes: the transaction committed unexpectedly");
}

export const db = { prisma, write, delegate, mutant };
