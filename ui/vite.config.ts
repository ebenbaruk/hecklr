import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The build lands inside the Python package so `pip install hecklr` ships it (ADR 0016a).
const OUT_DIR = "../packages/hecklr/src/hecklr/ui_api/static";
// `hecklr ui --dev` serves the API here; `npm run dev` proxies /api to it.
const API = process.env.HECKLR_API ?? "http://127.0.0.1:8484";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: OUT_DIR, emptyOutDir: true },
  server: { proxy: { "/api": API } },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    coverage: {
      include: ["src/**"],
      exclude: ["src/test/**", "src/main.tsx"],
      // The same bar as the Python core packages (SC9).
      thresholds: { lines: 85 },
    },
  },
});
