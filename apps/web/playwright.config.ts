import { defineConfig, devices } from "@playwright/test";

const apiPort = process.env.ENDLESS_TASK_E2E_API_PORT ?? "18000";
const webPort = process.env.ENDLESS_TASK_E2E_WEB_PORT ?? "4173";
const apiUrl = `http://127.0.0.1:${apiPort}`;
const webUrl = `http://127.0.0.1:${webPort}`;
const fixtureNoProxy = Array.from(
  new Set(
    [process.env.NO_PROXY ?? "", process.env.no_proxy ?? "", "127.0.0.1,localhost,::1"]
      .flatMap((value) => value.split(","))
      .map((value) => value.trim())
      .filter(Boolean),
  ),
).join(",");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  globalTeardown: "./e2e/globalTeardown.ts",
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
      // The API calls a loopback model fixture; macOS system proxies must not route it externally.
      env: { ...process.env, NO_PROXY: fixtureNoProxy, no_proxy: fixtureNoProxy },
      command:
        `ENDLESS_TASK_E2E=1 ENDLESS_TASK_E2E_DB="\${TMPDIR:-/tmp}/endless-task-playwright-${apiPort}.db" .venv/bin/python -m uvicorn tests.fixtures.e2e_server:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: "../api",
      url: `${apiUrl}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `ENDLESS_TASK_API_URL=${apiUrl} npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
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
