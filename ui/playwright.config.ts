import { defineConfig, devices } from "@playwright/test";

// End-to-end check against a running `hecklr ui` on a real results store (see e2e/README.md).
export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.HECKLR_UI_URL ?? "http://127.0.0.1:8484",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
