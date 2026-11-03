import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./styles/global.css";
import "./styles/landing.css";
import "./styles/workbench.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("缺少 #root 节点");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
