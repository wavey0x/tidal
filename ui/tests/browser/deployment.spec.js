import { test, expect } from "@playwright/test";
import { AUCTION, contrastRatio, mockPublicApi, mockWallet } from "./fixtures";

async function prepare(page) {
  await page.getByRole("button", { name: "Deploy auction", exact: true }).click();
  await expect(page.locator(".deploy-modal")).toBeVisible();
}

for (const theme of ["light", "dark"]) {
  test(`${theme}: both API envelopes show warnings before sending; confirmed persists until mapping`, async ({ page }, testInfo) => {
    const api = await mockPublicApi(page);
    api.defaultsWarnings = ["Fixture quote warning"];
    api.prepareWarnings = ["Fixture gas estimate warning"];
    await mockWallet(page);
    await page.emulateMedia({ colorScheme: theme });
    await page.goto("/");
    await prepare(page);
    await expect(page.getByText("Fixture quote warning", { exact: true })).toBeVisible();
    await expect(page.getByText("Fixture gas estimate warning", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    expect(await contrastRatio(page.locator(".deploy-modal-warning").first())).toBeGreaterThanOrEqual(4.5);
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-deployment-warnings.png`), animations: "disabled" });
    const reads = api.dashboardReads;
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
    await expect(page.locator(".deployment-confirmed")).toHaveCSS("color", theme === "light" ? "rgb(33, 127, 70)" : "rgb(91, 196, 125)");
    await expect.poll(() => api.dashboardReads).toBeGreaterThan(reads);
    await expect(page.getByText("Waiting for scanner mapping.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`${theme}-deployment-confirmed.png`), animations: "disabled" });
    api.auctionAddress = AUCTION;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.locator(".auction-cell .auction-address-row .address-value")).toHaveText("0x4444...4444");
    await expect(page.getByText("Waiting for scanner mapping.", { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(1);
  });
}

test("known revert is failed, not confirmed or pending", async ({ page }) => {
  await mockPublicApi(page);
  await mockWallet(page);
  await page.goto("/");
  await page.evaluate(() => { window.walletFixture.receipt = { status: "0x0" }; });
  await prepare(page);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("failed", { exact: true })).toBeVisible();
  await expect(page.getByText("Deployment transaction reverted", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Check again", exact: true })).toHaveCount(0);
});

for (const status of [null, "0x2", "1invalid"]) {
  test(`unknown receipt status ${status} stays pending and check-again does not resend`, async ({ page }) => {
    await mockPublicApi(page);
    await mockWallet(page);
    await page.goto("/");
    await page.evaluate((value) => { window.walletFixture.receipt = { status: value }; }, status);
    await prepare(page);
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByText("pending", { exact: true })).toBeVisible();
    await page.evaluate(() => { window.walletFixture.receipt = { status: "0x1" }; });
    await page.getByRole("button", { name: "Check again", exact: true }).click();
    await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(1);
  });
}

test("receipt timeout remains pending and recovers after checking again", async ({ page }) => {
  await mockPublicApi(page);
  await mockWallet(page);
  await page.clock.install();
  await page.goto("/");
  await page.evaluate(() => { window.walletFixture.receipt = null; });
  await prepare(page);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("checking…", { exact: true })).toBeVisible();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  await page.clock.runFor(120000);
  await expect(page.getByText("pending", { exact: true })).toBeVisible();
  await page.clock.resume();
  await page.evaluate(() => { window.walletFixture.receipt = { status: 1 }; });
  await page.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(1);
});

test("receipt RPC failure and wrong-network checks stay pending", async ({ page }) => {
  await mockPublicApi(page);
  await mockWallet(page);
  await page.goto("/");
  await page.evaluate(() => { window.walletFixture.receiptError = true; });
  await prepare(page);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("pending", { exact: true })).toBeVisible();
  await expect(page.getByText("Receipt temporarily unavailable", { exact: true })).toBeVisible();
  await page.evaluate(() => { window.walletFixture.receiptError = false; window.walletFixture.chainId = "0x2"; });
  await page.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(page.getByText("Switch your wallet to chain 1, then check again.", { exact: true })).toBeVisible();
  await page.evaluate(() => { window.walletFixture.chainId = "0x1"; });
  await page.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(1);
});

test("account changes and cancelled confirmation do not send", async ({ page }) => {
  await mockPublicApi(page);
  await mockWallet(page);
  await page.goto("/");
  await prepare(page);
  await page.getByRole("button", { name: "Confirm", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(page.locator(".deploy-modal a").first()).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Confirm", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
  await prepare(page);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
  await prepare(page);
  await page.evaluate(() => { window.walletFixture.account = "0x8888888888888888888888888888888888888888"; });
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("Wallet account changed; prepare the deployment again.", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
});

test("confirmation queues a fresh read when a previous refresh is in flight", async ({ page }) => {
  const api = await mockPublicApi(page);
  await mockWallet(page);
  await page.goto("/");
  await prepare(page);
  let release;
  api.holdDashboard = new Promise((resolve) => { release = resolve; });
  const before = api.dashboardReads;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => api.dashboardReads).toBe(before + 1);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
  expect(api.dashboardReads).toBe(before + 1);
  api.holdDashboard = null;
  release();
  await expect.poll(() => api.dashboardReads).toBe(before + 2);
});
