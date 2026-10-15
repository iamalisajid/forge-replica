// Lists and tables of rows from a list operation. When there are no rows, the empty-state text replaces
// the list (as the original does); without one, the empty list stays.
import type { ReactNode } from "react";
import { listItems } from "./api";
import { ActionButton, DeleteButton, NavLink } from "./buttons";
import { useContract } from "./contract";
import { useOperationData } from "./data";
import { display, resolveParams } from "./params";
import { useScreen } from "./screen";
import type { ParamMap, Row, RowPart } from "./types";

export interface ListProps {
  /** Accessible name of the list or table, e.g. "Issues". */
  label: string;
  operation: string;
  params: ParamMap;
  parts: RowPart[];
  /** Text shown instead of the list when it has no rows, e.g. "No issues yet.". */
  empty: string | null;
  /** Field that identifies a row (React key). */
  rowKey: string;
}

function useRows(operation: string, params: ParamMap): { rows: Row[] | null; error: string } {
  const screen = useScreen();
  const { operation: lookup } = useContract();
  const op = lookup(operation);
  const values = resolveParams(params, { route: screen.route, record: screen.record });
  const state = useOperationData(op, values, screen.version);
  if (state.data === undefined) return { rows: null, error: state.error ? state.error.message : "" };
  return { rows: listItems(op, state.data), error: "" };
}

const isButton = (part: RowPart) => part.kind === "action" || part.kind === "delete";

/** Neighbouring buttons share one cell / one group; every other part stands alone. */
function groupParts(parts: RowPart[]): RowPart[][] {
  const groups: RowPart[][] = [];
  for (const part of parts) {
    const last = groups[groups.length - 1];
    if (last && isButton(part) && isButton(last[0])) last.push(part);
    else groups.push([part]);
  }
  return groups;
}

function PartView({ part, item }: { part: RowPart; item: Row }): ReactNode {
  switch (part.kind) {
    case "link":
      return <NavLink label={part.text} field={part.field} to={part.to} item={item} />;
    case "text": {
      const text = part.field ? display(item[part.field]) : part.text;
      return part.badge ? <span className="badge">{text}</span> : <span>{text}</span>;
    }
    case "action":
      return <ActionButton label={part.label} operation={part.operation} params={part.params} item={item} hideWhenApplied={part.hideWhenApplied} />;
    case "delete":
      return <DeleteButton label={part.label} operation={part.operation} params={part.params} item={item} />;
  }
}

function keyOf(item: Row, rowKey: string, index: number): string {
  const value = item[rowKey];
  return value === undefined || value === null ? `row-${index}` : String(value);
}

function Problem({ text }: { text: string }) {
  return text ? <p className="muted">{text}</p> : null;
}

export function EntityTable({ label, operation, params, parts, empty, rowKey, role = "table" }: ListProps & { role?: string }) {
  const { rows, error } = useRows(operation, params);
  if (rows === null) return <Problem text={error} />;
  if (rows.length === 0 && empty !== null) return <p className="empty-state">{empty}</p>;
  const groups = groupParts(parts);
  return (
    <div className="table-wrap">
      <table aria-label={label} role={role === "table" ? undefined : role} className="table">
        <thead>
          <tr>
            {groups.map((group, i) => (
              <th key={i} scope="col" className={isButton(group[0]) ? "th-actions" : undefined}>
                {isButton(group[0]) ? "Actions" : group[0].kind === "link" || group[0].kind === "text" ? group[0].header : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr key={keyOf(item, rowKey, index)}>
              {groups.map((group, i) => (
                <td key={i} className={isButton(group[0]) ? "td-actions" : undefined}>
                  {isButton(group[0]) ? (
                    <div className="row-actions">
                      {group.map((part, j) => (
                        <PartView key={j} part={part} item={item} />
                      ))}
                    </div>
                  ) : (
                    <PartView part={group[0]} item={item} />
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EntityList({ label, operation, params, parts, empty, rowKey }: ListProps) {
  const { rows, error } = useRows(operation, params);
  if (rows === null) return <Problem text={error} />;
  if (rows.length === 0 && empty !== null) return <p className="empty-state">{empty}</p>;
  const groups = groupParts(parts);
  return (
    <ul aria-label={label} className="entity-list">
      {rows.map((item, index) => (
        <li key={keyOf(item, rowKey, index)} className="entity-list-item">
          {groups.map((group, i) =>
            isButton(group[0]) ? (
              <div key={i} className="row-actions">
                {group.map((part, j) => (
                  <PartView key={j} part={part} item={item} />
                ))}
              </div>
            ) : (
              <PartView key={i} part={group[0]} item={item} />
            ),
          )}
        </li>
      ))}
    </ul>
  );
}
