import { defineConfig, devices } from "@playwright/test";

const apiUrl = "http://127.0.0.1:8000";
const webUrl = "http://127.0.0.1:4173";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: webUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: [
    {
      command:
        'ENDLESS_TASK_E2E=1 ENDLESS_TASK_E2E_DB="${TMPDIR:-/tmp}/endless-task-playwright.db" uv run uvicorn tests.fixtures.e2e_server:app --host 127.0.0.1 --port 8000',
      cwd: "../api",
      url: `${apiUrl}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
      cwd: ".",
      url: webUrl,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
  projects: [
    {
      name: "desktop-chromium",
      testMatch: /desktop\/.*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-chromium",
      testMatch: /mobile\/.*\.spec\.ts/,
      use: { ...devices["Pixel 5"] },
    },
  ],
});