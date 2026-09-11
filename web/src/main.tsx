import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import { AppProviders } from "./app/providers";
import { prepareTelegramTheme } from "./shared/telegram/sdk";
import "./styles.css";

prepareTelegramTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);
