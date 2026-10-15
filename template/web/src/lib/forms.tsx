// Create dialogs, inline create forms and edit forms. Every submit goes to the API (no client-side
// validation), shows the API's {"error"} message in the form's alert, and refetches the screen on success.
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { callOperation, messageOf } from "./api";
import { ActionButton, DeleteButton, NavLink } from "./buttons";
import { useContract } from "./contract";
import { FieldControl, initialValues, toPayload, type FormValue, type FormValues } from "./fields";
import { buildRoute, resolveParams } from "./params";
import { useScreen } from "./screen";
import type { FieldInput, FormItem, ParamMap, RouteTarget, Row } from "./types";

type Mode = "create" | "update" | "action";

interface FormBodyProps {
  /** Accessible name of the <form>; null leaves it unnamed (the dialog carries the name). */
  label: string | null;
  mode: Mode;
  operation: string;
  params: ParamMap;
  items: FormItem[];
  /** Screen-level status text to show after a successful submit (e.g. "Saved"). */
  successNotice: string | null;
  /** Where to go after a successful submit; parameters may come from the response ("result"). */
  navigateTo: RouteTarget | null;
  onDone?: () => void;
  onCancel?: () => void;
  id?: string;
}

const BUTTON_KINDS = new Set(["submit", "cancel", "delete", "action"]);

/** Split items into runs so neighbouring buttons share one toolbar row. */
function groupItems(items: FormItem[]): FormItem[][] {
  const groups: FormItem[][] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && BUTTON_KINDS.has(item.kind) && BUTTON_KINDS.has(last[0].kind)) last.push(item);
    else groups.push([item]);
  }
  return groups;
}

function FormBody({ label, mode, operation, params, items, successNotice, navigateTo, onDone, onCancel, id }: FormBodyProps) {
  const screen = useScreen();
  const { operation: lookup } = useContract();
  const navigate = useNavigate();
  const op = lookup(operation);
  const fields = items.flatMap((item) => (item.kind === "field" ? [item.field] : []));
  const record = mode === "update" ? screen.record : null;

  const [values, setValues] = useState<FormValues>(() => initialValues(fields, record));
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState(false);

  // Edit forms follow the record: after every refetch they show what the server has.
  useEffect(() => {
    if (mode === "update") setValues(initialValues(fields, record));
    // fields come from literal props and never change for a mounted form
  }, [record, mode]);

  const setValue = (name: string, value: FormValue) => setValues((prev) => ({ ...prev, [name]: value }));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setError("");
    setStatus("");
    if (successNotice) screen.setNotice("");
    const ctx = { route: screen.route, record: screen.record };
    const pathValues = resolveParams(params, ctx);
    if (!pathValues) {
      setError("This page is still loading");
      return;
    }
    const body: Row = {};
    for (const field of fields) body[field.name] = toPayload(field, values[field.name]);
    setPending(true);
    try {
      const result = await callOperation(op, pathValues, body);
      const resultRow = result && typeof result === "object" && !Array.isArray(result) ? (result as Row) : null;
      const statusItem = items.find((item) => item.kind === "status");
      if (statusItem && statusItem.kind === "status") setStatus(statusItem.text);
      if (successNotice) screen.setNotice(successNotice);
      const href = navigateTo ? buildRoute(navigateTo, { ...ctx, result: resultRow }) : null;
      onDone?.();
      screen.refresh();
      if (href) navigate(href);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setPending(false);
    }
  }

  function renderItem(item: FormItem, key: number): ReactNode {
    switch (item.kind) {
      case "field":
        return <FieldControl key={key} field={item.field} value={values[item.field.name]} onChange={(v) => setValue(item.field.name, v)} />;
      case "alert":
        return (
          <p key={key} role="alert" className="form-error">
            {error}
          </p>
        );
      case "status":
        return (
          <p key={key} role="status" className="form-status">
            {status}
          </p>
        );
      case "text":
        return (
          <p key={key} className="muted">
            {item.text}
          </p>
        );
      case "submit":
        return (
          <button key={key} type="submit" className="btn btn-primary" disabled={pending}>
            {item.label}
          </button>
        );
      case "cancel":
        return (
          <button key={key} type="button" className="btn" onClick={onCancel}>
            {item.label}
          </button>
        );
      case "delete":
        return <DeleteButton key={key} label={item.label} operation={item.operation} params={item.params} navigateTo={item.navigateTo} />;
      case "action":
        return <ActionButton key={key} label={item.label} operation={item.operation} params={item.params} />;
      case "link":
        return <NavLink key={key} label={item.label} field={null} to={item.to} />;
    }
  }

  // Without an alert element in the spec, errors still show, as plain text (no extra alert role).
  const hasAlert = items.some((item) => item.kind === "alert");
  return (
    <form id={id} aria-label={label ?? undefined} className="form" onSubmit={submit} noValidate>
      {groupItems(items).map((group, g) =>
        BUTTON_KINDS.has(group[0].kind) ? (
          <div key={g} className="form-actions">
            {group.map((item, i) => renderItem(item, i))}
          </div>
        ) : (
          renderItem(group[0], g)
        ),
      )}
      {!hasAlert && error ? <p className="form-error">{error}</p> : null}
    </form>
  );
}

export interface CreateDialogProps {
  /** Accessible name of the button that opens the dialog, e.g. "New project". */
  trigger: string;
  /** Dialog heading and accessible name, e.g. "New project". */
  title: string;
  operation: string;
  params: ParamMap;
  items: FormItem[];
  successNotice?: string | null;
  navigateTo?: RouteTarget | null;
}

/** A button that opens a modal <dialog> with a create form. The dialog is removed from the page when closed. */
export function CreateDialog({ trigger, title, operation, params, items, successNotice = null, navigateTo = null }: CreateDialogProps) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  return (
    <>
      <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
        {trigger}
      </button>
      {open ? (
        <ModalDialog title={title} onClosed={close}>
          {(dismiss) => (
            <FormBody
              label={null}
              mode="create"
              operation={operation}
              params={params}
              items={items}
              successNotice={successNotice}
              navigateTo={navigateTo}
              onDone={dismiss}
              onCancel={dismiss}
            />
          )}
        </ModalDialog>
      ) : null}
    </>
  );
}

function ModalDialog({ title, onClosed, children }: { title: string; onClosed: () => void; children: (dismiss: () => void) => ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    dialog.addEventListener("close", onClosed);
    if (!dialog.open) dialog.showModal();
    return () => dialog.removeEventListener("close", onClosed);
  }, [onClosed]);
  const dismiss = useCallback(() => ref.current?.close(), []);
  return (
    <dialog ref={ref} aria-labelledby={titleId} className="dialog">
      <h2 id={titleId} className="dialog-title">
        {title}
      </h2>
      {children(dismiss)}
    </dialog>
  );
}

export interface FormProps {
  /** Accessible name of the form, e.g. "Edit issue". */
  label: string | null;
  operation: string;
  params: ParamMap;
  items: FormItem[];
  successNotice?: string | null;
  navigateTo?: RouteTarget | null;
  /** DOM id, so a submit button outside the form can target it. */
  id?: string;
}

/** A form prefilled from the screen's record; submits the update operation. */
export function EditForm({ label, operation, params, items, successNotice = null, navigateTo = null, id }: FormProps) {
  return (
    <FormBody
      id={id}
      label={label}
      mode="update"
      operation={operation}
      params={params}
      items={items}
      successNotice={successNotice}
      navigateTo={navigateTo}
    />
  );
}

/** An always-visible create form (not in a dialog). */
export function CreateForm({ label, operation, params, items, successNotice = null, navigateTo = null, id }: FormProps) {
  const [generation, setGeneration] = useState(0);
  return (
    <FormBody
      key={generation}
      id={id}
      label={label}
      mode="create"
      operation={operation}
      params={params}
      items={items}
      successNotice={successNotice}
      navigateTo={navigateTo}
      onDone={() => setGeneration((g) => g + 1)}
      onCancel={() => setGeneration((g) => g + 1)}
    />
  );
}

/** A form that runs an action operation with the fields it contains. */
export function ActionForm({ label, operation, params, items, successNotice = null, navigateTo = null, id }: FormProps) {
  return (
    <FormBody
      id={id}
      label={label}
      mode="action"
      operation={operation}
      params={params}
      items={items}
      successNotice={successNotice}
      navigateTo={navigateTo}
    />
  );
}

/** A submit button placed outside its form (linked with the form attribute). */
export function SubmitButton({ label, form }: { label: string; form: string }) {
  return (
    <button type="submit" form={form} className="btn btn-primary">
      {label}
    </button>
  );
}

export type { FieldInput };
