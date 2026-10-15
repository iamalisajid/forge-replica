// Labelled form controls. The <label> wraps its control, so the accessible name is exactly the label text.
import type { ChangeEvent } from "react";
import { listItems } from "./api";
import { useContract } from "./contract";
import { useOperationData } from "./data";
import { display, resolveParams } from "./params";
import { useScreen } from "./screen";
import type { FieldInput, Row } from "./types";

export type FormValue = string | boolean;
export type FormValues = Record<string, FormValue>;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** ISO timestamp -> value for <input type="datetime-local"> (local time, minutes). */
function toLocalDateTime(value: unknown): string {
  if (typeof value !== "string" || !value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The value a control starts with: the record's value when editing, else the spec default. */
export function initialValue(field: FieldInput, record: Row | null): FormValue {
  if (record) {
    const value = record[field.name];
    switch (field.control) {
      case "checkbox":
        return value === true;
      case "datetime":
        return toLocalDateTime(value);
      case "date":
        return typeof value === "string" ? value.slice(0, 10) : "";
      default:
        return display(value);
    }
  }
  if (field.control === "checkbox") return field.default === "true";
  if (field.default !== null) return field.default;
  if (field.control === "select" && field.required && !field.optionsFrom && field.options.length) return field.options[0];
  return "";
}

export function initialValues(fields: FieldInput[], record: Row | null): FormValues {
  const values: FormValues = {};
  for (const field of fields) values[field.name] = initialValue(field, record);
  return values;
}

/** Control value -> JSON value for the request body. */
export function toPayload(field: FieldInput, value: FormValue): unknown {
  if (field.control === "checkbox") return value === true;
  const raw = typeof value === "string" ? value : "";
  switch (field.control) {
    case "select":
    case "date":
      return raw === "" ? null : raw;
    case "number": {
      if (raw.trim() === "") return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : raw;
    }
    case "datetime": {
      if (raw === "") return null;
      const d = new Date(raw);
      return Number.isNaN(d.getTime()) ? raw : d.toISOString();
    }
    default:
      return raw;
  }
}

interface ControlProps {
  field: FieldInput;
  value: FormValue;
  onChange: (value: FormValue) => void;
}

function ReferenceOptions({ field }: { field: FieldInput }) {
  const screen = useScreen();
  const { operation } = useContract();
  const source = field.optionsFrom!;
  const op = operation(source.operation);
  const params = resolveParams(source.params, { route: screen.route, record: screen.record });
  const state = useOperationData(op, params, screen.version);
  const rows = state.data !== undefined ? listItems(op, state.data) : [];
  return (
    <>
      {rows.map((row) => {
        const value = display(row[source.valueField]);
        return (
          <option key={value} value={value}>
            {display(row[source.labelField]) || value}
          </option>
        );
      })}
    </>
  );
}

export function FieldControl({ field, value, onChange }: ControlProps) {
  const text = typeof value === "string" ? value : "";
  const onText = (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => onChange(e.target.value);

  if (field.control === "checkbox") {
    return (
      <label className="field field-check">
        <input type="checkbox" name={field.name} className="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
        {field.label}
      </label>
    );
  }

  let control;
  switch (field.control) {
    case "textarea":
      control = <textarea name={field.name} className="input textarea" rows={4} value={text} onChange={onText} />;
      break;
    case "select":
      control = (
        <select name={field.name} className="input select" value={text} onChange={onText}>
          {!field.required || field.optionsFrom ? <option value="">—</option> : null}
          {field.optionsFrom ? (
            <ReferenceOptions field={field} />
          ) : (
            field.options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))
          )}
        </select>
      );
      break;
    case "number":
      control = <input type="number" name={field.name} className="input" step="any" value={text} onChange={onText} />;
      break;
    case "date":
      control = <input type="date" name={field.name} className="input" value={text} onChange={onText} />;
      break;
    case "datetime":
      control = <input type="datetime-local" name={field.name} className="input" value={text} onChange={onText} />;
      break;
    default:
      control = (
        <input
          type="text"
          name={field.name}
          className="input"
          autoComplete="off"
          inputMode={field.type === "integer" || field.type === "number" ? "decimal" : undefined}
          value={text}
          onChange={onText}
        />
      );
  }
  return (
    <label className="field">
      {field.label}
      {control}
    </label>
  );
}
