// Browser smoke test against the real backend: `bunx playwright test` (from admin-ui/).
// Builds the UI, then starts `uv run writer-demo-server` (in-memory stores, FakeLLM).
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 8765);
export const ADMIN_PASSWORD = "alice-e2e-password";
export const REVIEWER_PASSWORD = "rita-e2e-password";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bun run build && cd .. && uv run writer-demo-server",
    url: `http://127.0.0.1:${PORT}/admin/`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: {
      WRITER_DEMO_PORT: String(PORT),
      WRITER_DEMO_ADMIN_PASSWORD: ADMIN_PASSWORD,
      WRITER_DEMO_REVIEWER_PASSWORD: REVIEWER_PASSWORD,
    },
  },
});
