// The app shell: header, one route per generated screen, and a not-found page.
import type { ComponentType } from "react";
import { Link, Route, Routes, useLocation } from "react-router";
import { ContractProvider } from "./contract";
import type { OperationWire } from "./types";

export interface ScreenRoute {
  id: string;
  /** react-router path, e.g. "/projects/:project_id". */
  path: string;
  Component: ComponentType;
}

export interface AppProps {
  appName: string;
  /** Where the brand link goes; null when no screen lives at a parameterless path. */
  homePath: string | null;
  screens: ScreenRoute[];
  operations: Record<string, OperationWire>;
}

/** Remount the screen on every URL change so each page starts from fresh data and closed dialogs. */
function Keyed({ Component }: { Component: ComponentType }) {
  const location = useLocation();
  return <Component key={location.pathname} />;
}

function NotFound({ homePath }: { homePath: string | null }) {
  return (
    <div className="screen">
      <h1 className="page-title">Not found</h1>
      {homePath ? (
        <Link className="nav-link" to={homePath}>
          Go to the start page
        </Link>
      ) : null}
    </div>
  );
}

export function App({ appName, homePath, screens, operations }: AppProps) {
  return (
    <ContractProvider appName={appName} operations={operations}>
      <div className="app">
        <header className="app-header">
          <div className="app-header-inner">
            {homePath ? (
              <Link className="brand" to={homePath}>
                {appName}
              </Link>
            ) : (
              <span className="brand">{appName}</span>
            )}
          </div>
        </header>
        <main className="app-main">
          <Routes>
            {screens.map((screen) => (
              <Route key={screen.id} path={screen.path} element={<Keyed Component={screen.Component} />} />
            ))}
            <Route path="*" element={<NotFound homePath={homePath} />} />
          </Routes>
        </main>
      </div>
    </ContractProvider>
  );
}
