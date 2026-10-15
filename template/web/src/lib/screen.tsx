// A screen: loads its record (detail screens), shares it with every element and refetches after writes.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";
import { ApiError, messageOf } from "./api";
import { useContract } from "./contract";
import { useOperationData } from "./data";
import { resolveParams } from "./params";
import type { RecordSource, Row } from "./types";

export interface ScreenValue {
  route: Readonly<Record<string, string | undefined>>;
  record: Row | null;
  /** Bumped after every successful write; every query on the screen refetches. */
  version: number;
  refresh: () => void;
  /** Success message for the screen's status element, e.g. "Saved". */
  notice: string;
  setNotice: (text: string) => void;
  /** Error message from row actions and deletes. */
  error: string;
  setError: (text: string) => void;
}

const ScreenContext = createContext<ScreenValue | null>(null);

export function useScreen(): ScreenValue {
  const value = useContext(ScreenContext);
  if (!value) throw new Error("useScreen must be used inside <Screen>");
  return value;
}

export interface ScreenProps {
  title: string;
  /** Record field that names the page in the document title, e.g. "name". */
  titleField?: string | null;
  /** Detail screens load one record (a get operation) before rendering their elements. */
  record?: RecordSource | null;
  /** Show errors from row actions when the original has no alert element for them. */
  errorOutlet?: boolean;
  children: ReactNode;
}

export function Screen({ title, titleField = null, record = null, errorOutlet = true, children }: ScreenProps) {
  const route = useParams();
  const { appName, operation } = useContract();
  const [version, setVersion] = useState(0);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  const recordOp = record ? operation(record.operation) : null;
  const recordParams = record ? resolveParams(record.params, { route }) : null;
  const state = useOperationData(recordOp, recordParams, version);
  const row = record && state.data && typeof state.data === "object" ? (state.data as Row) : null;

  const pageTitle = titleField && row && row[titleField] != null ? String(row[titleField]) : title;
  useEffect(() => {
    document.title = pageTitle ? `${pageTitle} · ${appName}` : appName;
  }, [pageTitle, appName]);

  const value = useMemo<ScreenValue>(
    () => ({ route, record: row, version, refresh, notice, setNotice, error, setError }),
    [route, row, version, refresh, notice, error],
  );

  if (record) {
    if (!row && state.error) {
      const missing = state.error instanceof ApiError && state.error.status === 404;
      return <ScreenProblem title={missing ? "Not found" : "Something went wrong"} detail={messageOf(state.error)} />;
    }
    if (!row) return <div className="screen-loading" aria-busy="true" />;
  }

  return (
    <ScreenContext.Provider value={value}>
      <div className="screen">
        {children}
        {errorOutlet && error ? <p className="screen-error">{error}</p> : null}
      </div>
    </ScreenContext.Provider>
  );
}

function ScreenProblem({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="screen">
      <h1 className="page-title">{title}</h1>
      <p className="muted">{detail}</p>
      <Link className="nav-link" to="/">
        Go to the start page
      </Link>
    </div>
  );
}
