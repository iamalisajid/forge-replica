// Links and buttons that navigate, delete or run an action. Writes refetch the screen afterwards.
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { callOperation, messageOf } from "./api";
import { useContract } from "./contract";
import { buildRoute, display, resolveParams, type ParamContext } from "./params";
import { useScreen } from "./screen";
import type { OperationWire, ParamMap, RouteTarget, Row } from "./types";

function useParamContext(item: Row | null): ParamContext {
  const screen = useScreen();
  return { route: screen.route, record: screen.record, item };
}

export interface NavLinkProps {
  /** Static link text (used when ``field`` is null). */
  label: string;
  /** Show this field of the row (or of the screen's record) as the link text. */
  field: string | null;
  to: RouteTarget;
  item?: Row | null;
}

/** A link to another screen. Its text is either fixed or bound to a field. */
export function NavLink({ label, field, to, item = null }: NavLinkProps) {
  const ctx = useParamContext(item);
  const source = item ?? ctx.record ?? null;
  const text = field ? display(source?.[field]) : label;
  const href = buildRoute(to, ctx);
  if (href === null) return <span className="nav-link">{text}</span>;
  return (
    <Link className="nav-link" to={href}>
      {text}
    </Link>
  );
}

/** A button that opens another screen. */
export function NavButton({ label, to, item = null, primary = false }: { label: string; to: RouteTarget; item?: Row | null; primary?: boolean }) {
  const ctx = useParamContext(item);
  const navigate = useNavigate();
  const href = buildRoute(to, ctx);
  return (
    <button type="button" className={primary ? "btn btn-primary" : "btn"} disabled={href === null} onClick={() => href && navigate(href)}>
      {label}
    </button>
  );
}

/** Run a write and then either navigate or refetch the screen; errors go to the screen's error message. */
function useWrite(item: Row | null) {
  const screen = useScreen();
  const ctx = useParamContext(item);
  const navigate = useNavigate();
  const { operation } = useContract();
  const [pending, setPending] = useState(false);

  async function run(opId: string, params: ParamMap, navigateTo: RouteTarget | null) {
    if (pending) return;
    const op = operation(opId);
    const values = resolveParams(params, ctx);
    if (!values) return;
    // Resolve the destination first: after a delete the record it comes from is gone.
    const href = navigateTo ? buildRoute(navigateTo, ctx) : null;
    screen.setError("");
    screen.setNotice("");
    setPending(true);
    try {
      await callOperation(op, values);
      if (href) navigate(href);
      else screen.refresh();
    } catch (err) {
      screen.setError(messageOf(err));
    } finally {
      setPending(false);
    }
  }

  return { run, pending, operation };
}

export interface WriteButtonProps {
  label: string;
  operation: string;
  params: ParamMap;
  item?: Row | null;
}

/** Deletes the row (or the screen's record), then navigates to ``navigateTo`` or refetches. */
export function DeleteButton({ label, operation, params, item = null, navigateTo = null }: WriteButtonProps & { navigateTo?: RouteTarget | null }) {
  const { run, pending } = useWrite(item);
  return (
    <button type="button" className="btn btn-danger" disabled={pending} onClick={() => void run(operation, params, navigateTo)}>
      {label}
    </button>
  );
}

function applied(op: OperationWire, item: Row | null): boolean {
  if (!item || op.actionSets.length === 0) return false;
  return op.actionSets.every((set) => display(item[set.field]) === set.value);
}

/**
 * Runs an action on the row (or the screen's record), then refetches.
 * With ``hideWhenApplied`` the button disappears once the action's effect already holds (e.g. "Close" on a closed issue).
 */
export function ActionButton({ label, operation, params, item = null, hideWhenApplied = false }: WriteButtonProps & { hideWhenApplied?: boolean }) {
  const { run, pending, operation: lookup } = useWrite(item);
  const screen = useScreen();
  if (hideWhenApplied && applied(lookup(operation), item ?? screen.record)) return null;
  return (
    <button type="button" className="btn" disabled={pending} onClick={() => void run(operation, params, null)}>
      {label}
    </button>
  );
}
