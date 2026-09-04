import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: true,
  workers: 2,
  use: {
    baseURL: "http://127.0.0.1:5182",
    channel: process.env.TIDAL_TEST_BROWSER_CHANNEL || undefined,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5182 --strictPort",
    url: "http://127.0.0.1:5182",
    reuseExistingServer: false,
    env: {
      VITE_TIDAL_API_BASE_URL: "/api/v1/tidal",
      VITE_FACTORY_DASHBOARD_API_BASE_URL: "",
      TIDAL_API_PROXY_TARGET: "http://127.0.0.1:9",
    },
  },
});
