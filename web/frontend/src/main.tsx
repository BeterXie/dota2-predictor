import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FluentProvider
      theme={webDarkTheme}
      className="fluent-root"
      applyStylesToPortals={false}
    >
      <App />
    </FluentProvider>
  </React.StrictMode>,
);
