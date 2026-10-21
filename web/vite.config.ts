import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  base:
    "/" +
    (process.env.PAGES_BASE_PATH || "").split("/").filter(Boolean).join("/") +
    ((process.env.PAGES_BASE_PATH || "").split("/").filter(Boolean).length
      ? "/"
      : ""),
  plugins: [react()],
  build: { outDir: "dist/client", emptyOutDir: true },
});
