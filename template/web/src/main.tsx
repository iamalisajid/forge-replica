import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { OPERATIONS } from "./generated/contract";
import { APP_NAME, HOME_PATH, SCREENS } from "./generated/routes";
import { App } from "./lib";
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App appName={APP_NAME} homePath={HOME_PATH} screens={SCREENS} operations={OPERATIONS} />
    </BrowserRouter>
  </StrictMode>,
);
