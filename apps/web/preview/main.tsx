import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { HolmApplication } from "../src/app/application";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/noto-sans-sc/400.css";
import "@fontsource/noto-sans-sc/500.css";
import "@fontsource/noto-sans-sc/600.css";
import "@fontsource/noto-sans-sc/700.css";
import "../src/styles/tokens.css";
import "../src/styles/icons.css";
import "../src/styles/components.css";
import "../src/styles/app.css";

const root = document.getElementById("holm-root");
if (root === null) throw new Error("preview mount root is missing");

createRoot(root).render(
  <StrictMode>
    <HolmApplication />
  </StrictMode>,
);
