// Headings, text and status messages: fixed text or bound to a field of the screen's record.
import { display } from "./params";
import { useScreen } from "./screen";

type Level = 1 | 2 | 3;

function H({ level, children }: { level: Level; children: string }) {
  if (level === 1) return <h1 className="page-title">{children}</h1>;
  if (level === 2) return <h2 className="section-title">{children}</h2>;
  return <h3 className="subsection-title">{children}</h3>;
}

/** A heading with fixed text, e.g. "Projects". */
export function Heading({ text, level = 1 }: { text: string; level?: Level }) {
  return <H level={level}>{text}</H>;
}

/** The detail page heading: one field of the screen's record, e.g. the project's name. */
export function DetailHeading({ field, level = 1 }: { field: string; level?: Level }) {
  const { record } = useScreen();
  return <H level={level}>{display(record?.[field])}</H>;
}

/** A paragraph with fixed text. */
export function Text({ text }: { text: string }) {
  return <p className="muted">{text}</p>;
}

/** A paragraph showing one field of the screen's record. */
export function DetailText({ field }: { field: string }) {
  const { record } = useScreen();
  return <p>{display(record?.[field])}</p>;
}

/** Live region for success messages (e.g. "Saved" after an edit form is submitted). */
export function StatusMessage() {
  const { notice } = useScreen();
  return (
    <p role="status" className="notice">
      {notice}
    </p>
  );
}

/** Alert for errors from row actions and deletes on this screen. */
export function AlertMessage() {
  const { error } = useScreen();
  return (
    <p role="alert" className="form-error">
      {error}
    </p>
  );
}

/** A labelled control that is not part of any form in the spec (kept so its role and name exist). */
export function StandaloneField({ label, multiline = false }: { label: string; multiline?: boolean }) {
  return (
    <label className="field">
      {label}
      {multiline ? <textarea className="input textarea" rows={3} /> : <input type="text" className="input" autoComplete="off" />}
    </label>
  );
}

/** A button with no behaviour the spec could describe (kept so its role and name exist). */
export function InertButton({ label }: { label: string }) {
  return (
    <button type="button" className="btn">
      {label}
    </button>
  );
}
