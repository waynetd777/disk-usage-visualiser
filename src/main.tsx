import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

/// How long the splash stays up at minimum, so a fast start reads as a deliberate opening
/// rather than a blink.
const MIN_SPLASH_MS = 900;
const splashShownAt = Date.now();

/// Fade out the splash that index.html painted, once there is a real screen to replace it.
export function hideSplash() {
  const el = document.getElementById("splash");
  if (!el || el.classList.contains("gone") || el.dataset.going) return;
  const wait = Math.max(0, MIN_SPLASH_MS - (Date.now() - splashShownAt));
  el.dataset.going = "1";
  setTimeout(() => {
    el.classList.add("gone");
    setTimeout(() => el.remove(), 250);
  }, wait);
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
