import { test, expect } from "@playwright/test";
import { mockPublicApi, refreshOnFocus } from "./fixtures";

test("all pages retain freshness metadata without manual refresh buttons", async ({ page }) => {
  await mockPublicApi(page);
  for (const path of ["/", "/fee-burner", "/logs", "/alerts"]) {
    await page.goto(path);
    await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(".refresh-status")).toBeVisible();
    await expect(page.getByRole("button", { name: /^Refresh/ })).toHaveCount(0);
  }
});

test("Automatic focus refresh preserves last good dashboard data on failure", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/");
  await expect(page.getByText("$1.25", { exact: true })).toBeVisible();
  state.balance = "2";
  await refreshOnFocus(page);
  await expect(page.getByText("$2.50", { exact: true })).toBeVisible();
  state.failed = true;
  await refreshOnFocus(page);
  await expect(page.getByText("Unable to load dashboard", { exact: true })).toBeVisible();
  await expect(page.getByText(/Data may be stale/)).toBeVisible();
  await expect(page.getByText("$2.50", { exact: true })).toBeVisible();
  state.failed = false;
  state.balance = "3";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("$3.75", { exact: true })).toBeVisible();
  await expect(page.getByText(/Data may be stale/)).toHaveCount(0);
});

test("visible interval refreshes; hidden and inactive views do not poll", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.clock.install();
  await page.goto("/");
  await expect(page.getByText("$1.25", { exact: true })).toBeVisible();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  state.balance = "2";
  await page.clock.runFor(30000);
  await expect(page.getByText("$2.50", { exact: true })).toBeVisible();
  const beforeHidden = state.dashboardReads;
  await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }));
  state.balance = "3";
  await page.clock.runFor(90000);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  expect(state.dashboardReads).toBe(beforeHidden);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByText("$3.75", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Logs", exact: true }).click();
  await expect(page.getByText("KICK REWARD3 -> USDC", { exact: true })).toBeVisible();
  const beforeInactive = state.dashboardReads;
  state.balance = "4";
  await page.clock.runFor(30000);
  await expect(page.getByText("KICK REWARD4 -> USDC", { exact: true })).toBeVisible();
  expect(state.dashboardReads).toBe(beforeInactive);
  await page.getByRole("tab", { name: "Strategies", exact: true }).click();
  await expect(page.getByText("$5.00", { exact: true })).toBeVisible();
});

test("refresh triggers never overlap an in-flight request", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.clock.install();
  await page.goto("/");
  await expect(page.getByText("$1.25", { exact: true })).toBeVisible();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  let release;
  state.holdDashboard = new Promise((resolve) => { release = resolve; });
  const before = state.dashboardReads;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => state.dashboardReads).toBe(before + 1);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(90000);
  expect(state.dashboardReads).toBe(before + 1);
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "true");
  state.balance = "2";
  release();
  await expect(page.getByText("$2.50", { exact: true })).toBeVisible();
});

test("logs revalidate revisited pages and do not prefetch", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/logs");
  await expect(page.getByText("KICK REWARD1 -> USDC", { exact: true })).toBeVisible();
  expect(state.logReads.every((offset) => offset === 0)).toBe(true);
  await page.getByRole("button", { name: "Older", exact: true }).first().click();
  await expect.poll(() => state.logReads.includes(25)).toBe(true);
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
  state.balance = "2";
  await page.getByRole("button", { name: "Newer", exact: true }).first().click();
  await expect(page.getByText("KICK REWARD2 -> USDC", { exact: true })).toBeVisible();
});

test("alerts refresh on entry and repeated focus", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/");
  await expect(page.getByLabel("1 alerts need action")).toBeVisible();
  state.balance = "2";
  await page.getByRole("tab", { name: /Alerts/ }).click();
  await expect(page.getByLabel("2 alerts need action")).toBeVisible();
  state.balance = "3";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByLabel("3 alerts need action")).toBeVisible();
  state.balance = "4";
  await refreshOnFocus(page);
  await expect(page.getByLabel("4 alerts need action")).toBeVisible();
});
