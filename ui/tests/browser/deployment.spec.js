import { test, expect } from "@playwright/test";
import { AUCTION, TOKEN, contrastRatio, mockPublicApi, mockWallet, refreshOnFocus } from "./fixtures";

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
    await refreshOnFocus(page);
    await expect(page.getByText("confirmed", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`${theme}-deployment-confirmed.png`), animations: "disabled" });
    api.auctionAddress = AUCTION;
    await refreshOnFocus(page);
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

for (const theme of ["light", "dark"]) {
  test(`${theme}: mobile deployment separates exact pricing from raw fields without hiding warnings`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, colorScheme: theme,
      permissions: ["clipboard-read", "clipboard-write"] });
    try {
      const page = await context.newPage();
      const api = await mockPublicApi(page);
      const name = "StrategyCurveBoostedFactory-ETH MATIC-f";
      api.deployDefaults = { strategyName: name, wantSymbol: "ETH MATIC-f", startingPriceDisplay: "10.162816661626094681",
        startingPrice: "10162816661626096000", inference: { sellTokenAddress: TOKEN, sellTokenSymbol: "CRV" }, startPriceBufferBps: 1000 };
      api.defaultsWarnings = ["Curve quote unavailable for deploy inference (status: no_route)"];
      await mockWallet(page);
      await page.goto("/");
      await page.getByRole("button", { name: "Show details for Fixture Strategy", exact: true }).tap();
      await prepare(page);
      const modal = page.locator(".deploy-modal");
      await expect(modal.getByRole("heading")).toHaveText("Deploy auction");
      await expect(modal.locator(".deploy-modal-entity .row-primary")).toHaveText("Curve-ETH MATIC-f");
      await expect(modal.locator(".deploy-price-amount")).toHaveText("10.162816661626094681");
      await expect(modal.locator(".deploy-price-token")).toHaveText("ETH MATIC-f");
      await expect(modal.locator(".deploy-technical dl")).toBeHidden();
      await expect(modal.locator(".deploy-modal-warning")).toBeVisible();
      expect(await contrastRatio(modal.locator(".deploy-modal-warning"))).toBeGreaterThanOrEqual(4.5);
      const copy = modal.locator(".deploy-modal-entity .copy-trigger");
      await copy.tap();
      await expect(copy).toHaveClass(/is-copied/);
      await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
      expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
      await page.screenshot({ path: testInfo.outputPath(`${theme}-compact-deploy-mobile.png`), animations: "disabled" });
      await modal.locator(".deploy-technical summary").tap();
      await expect(modal.locator(".deploy-technical")).toContainText("10162816661626096000");
      await expect(modal.locator(".deploy-technical")).toContainText(name);
      expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
      for (const width of [320, 390, 768]) {
        await page.setViewportSize({ width, height: 700 });
        expect(await modal.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
        const box = await modal.boundingBox();
        expect(box.x).toBeGreaterThanOrEqual(12);
        expect(box.y).toBeGreaterThanOrEqual(12);
        expect(box.y + box.height).toBeLessThanOrEqual(688);
      }
      await modal.getByRole("button", { name: "Cancel", exact: true }).tap();
      await expect(modal).toHaveCount(0);
      await expect(page.getByRole("dialog", { name: "Strategy details" })).toBeVisible();
      expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
    } finally { await context.close(); }
  });
}
