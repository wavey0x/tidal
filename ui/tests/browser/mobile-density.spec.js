import { test, expect } from "@playwright/test";
import { mockPublicApi, contrastRatio, refreshOnFocus, TOKEN, WANT, AUCTION } from "./fixtures";

const address = index => `0x${index.toString(16).padStart(40, "0")}`;
const tx = `0x${"ab".repeat(32)}`;
const strategy = (index, extra = {}) => ({
  sourceType: "strategy", sourceAddress: address(index), sourceName: `Curve-pool${index}`,
  wantAddress: WANT, wantSymbol: "crvUSD", auctionAddress: AUCTION, auctionVersion: "1.0.4",
  scannedAt: new Date().toISOString(), depositLimit: "1", active: true,
  balances: [{ tokenAddress: TOKEN, tokenSymbol: "CRV", tokenLogoUrl: "/auctionscan-favicon.svg",
    normalizedBalance: String(1000 - index), tokenPriceUsd: "1", tokenDecimals: 18 }],
  kicks: [{ id: index, createdAt: new Date(Date.now() - 3600000).toISOString(), txHash: tx, operationType: "kick", status: "CONFIRMED", auctionAddress: AUCTION }],
  ...extra,
});

async function density(rows, maxHeight) {
  const bounds = await rows.evaluateAll(nodes => nodes.map(node => {
    const r = node.getBoundingClientRect();
    return { height: r.height, visible: r.top >= 0 && r.bottom <= innerHeight };
  }));
  expect(bounds.filter(row => row.visible).length).toBeGreaterThanOrEqual(6);
  expect(Math.max(...bounds.map(row => row.height))).toBeLessThanOrEqual(maxHeight);
}

for (const theme of ["light", "dark"]) {
  test(`${theme}: touch strategy summaries show six rows, retain warnings and use one detail sheet`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, colorScheme: theme,
      permissions: ["clipboard-read", "clipboard-write"] });
    try {
      const page = await context.newPage();
      const state = await mockPublicApi(page);
      state.latestScanAt = new Date().toISOString();
      state.rows = Array.from({ length: 12 }, (_, i) => strategy(i + 1));
      state.rows[1].auctionAddress = null;
      state.rows[2].balances[0].kickPrepareStatus = "PAUSED";
      state.rows[2].balances[0].kickPrepareReason = "AUCTION_PRICE_GRANULARITY";
      await page.goto("/");
      const rows = page.locator(".strategy-row");
      await expect(rows).toHaveCount(12);
      await density(rows, 88);
      await expect(rows.nth(1)).toContainText("Needs auction");
      await expect(rows.nth(2)).toContainText("Paused");
      await expect(rows.locator(".copy-trigger, .auction-version-badge, .transaction-link, .reward-caption")).toHaveCount(0);
      for (const button of await rows.first().getByRole("button").all()) expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(44);
      expect(await contrastRatio(rows.nth(2).locator(".mobile-row-warning"))).toBeGreaterThanOrEqual(4.5);
      await page.screenshot({ path: testInfo.outputPath(`${theme}-strategies-compact-touch.png`) });
      const filters = page.getByRole("button", { name: "Filters", exact: true });
      await expect(page.getByLabel("Filter by reward token")).toBeHidden();
      await filters.tap();
      await page.getByLabel("Include retired").check();
      await page.getByLabel("Filter by reward token").selectOption(TOKEN);
      await page.getByRole("button", { name: "Filters, 2 active", exact: true }).tap();
      await expect(page.getByLabel("Filter by reward token")).toBeHidden();
      await refreshOnFocus(page);
      await expect(page.getByRole("button", { name: "Filters, 2 active", exact: true })).toBeVisible();
      const first = rows.first();
      await first.getByRole("button", { name: /Expand rewards/ }).tap();
      const dialog = page.getByRole("dialog", { name: "Strategy details" });
      await expect(dialog.locator(".strategy-detail-grid > div").first()).toHaveClass(/strategy-detail-balances/);
      const copy = dialog.getByRole("button", { name: "Copy token address for CRV", exact: true });
      await copy.tap();
      await expect(copy).toHaveClass(/is-copied/);
      await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
      expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
      state.rows[0].balances[0].normalizedBalance = "2";
      await refreshOnFocus(page);
      await expect(rows.first()).toHaveAttribute("data-strategy", address(1));
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(first.getByRole("button", { name: /Expand rewards/ })).toBeFocused();
      await first.getByRole("button", { name: /Show details/ }).tap();
      await expect(dialog.locator(".strategy-detail-grid > div").first()).not.toHaveClass(/strategy-detail-balances/);
      await expect(dialog.locator(".transaction-link")).toHaveAttribute("href", `https://etherscan.io/tx/${tx}`);
      await page.keyboard.press("Escape");
      for (const width of [320, 430, 780]) {
        await page.setViewportSize({ width, height: 844 });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      }
    } finally { await context.close(); }
  });
}

test("mobile summaries retain duplicate identities, exact large totals and text zoom access", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    const state = await mockPublicApi(page);
    state.latestScanAt = new Date().toISOString();
    state.rows = [strategy(1, { sourceName: "Same strategy" }), strategy(2, { sourceName: "Same strategy" }),
      strategy(3, { sourceName: "VeryLongStrategyNameWithoutNaturalBreaksForTheLayout" })];
    state.rows[0].balances[0].normalizedBalance = "123456789012345.67";
    state.rows[2].balances[0].tokenPriceUsd = null;
    await page.goto("/");
    await expect(page.locator(".mobile-identity-hint")).toHaveCount(2);
    await expect(page.locator(".reward-total").first()).toHaveText("$123,456,789,012,345.67");
    await expect(page.locator(".mobile-row-meta").last()).toContainText("Unpriced rewards");
    // Text-only zoom stresses content wrapping without shrinking tap targets or hiding values.
    await page.addStyleTag({ content: "body, button { font-size: 24px !important; } .mobile-row-meta { font-size: 22px !important; line-height: 1.5 !important; }" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: /Expand rewards/ }).first().tap();
    await expect(page.getByRole("dialog").locator(".token-balance")).toHaveText("$123,456,789,012,345.67");
    expect(await page.locator(".kick-modal-body").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  } finally { await context.close(); }
});

const event = (id, extra = {}) => ({
  id, operationType: "kick", status: "CONFIRMED", createdAt: new Date(Date.now() - id * 60000).toISOString(),
  tokenSymbol: "CRV", tokenAddress: TOKEN, wantSymbol: "crvUSD", wantAddress: WANT,
  sourceAddress: address(id), sourceName: `Curve-pool${id}`, auctionAddress: AUCTION,
  usdValue: "761.75", txHash: tx, normalizedBalance: "1000", ...extra,
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: mobile logs are compact events with explicit outcomes and explorer access in details`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, colorScheme: theme,
      permissions: ["clipboard-read", "clipboard-write"] });
    try {
      const page = await context.newPage();
      const state = await mockPublicApi(page);
      state.logsData = url => ({ total: 40, hasMore: Number(url.searchParams.get("offset") || 0) === 0, kicks: Array.from({ length: 12 }, (_, i) => event(i + 1,
        i === 1 ? { status: "ERROR", txHash: null, errorMessage: "Receipt reverted" } : i === 2 ? { status: "SUBMITTED", usdValue: null } : {})) });
      await page.goto("/logs");
      const rows = page.locator(".kick-log-row");
      await expect(rows).toHaveCount(12);
      await density(rows, 96);
      await expect(rows.nth(1).locator(".status-badge")).toHaveText("ERROR");
      await expect(rows.nth(2).locator(".status-badge")).toHaveText("SUBMITTED");
      await expect(rows.nth(2).locator(".log-usd-cell")).toHaveText("?");
      await expect(rows.locator(".copy-trigger, .transaction-link")).toHaveCount(0);
      expect(await contrastRatio(rows.first().locator(".status-badge"))).toBeGreaterThanOrEqual(4.5);
      expect((await rows.first().getByRole("button").boundingBox()).height).toBeGreaterThanOrEqual(44);
      await page.screenshot({ path: testInfo.outputPath(`${theme}-logs-compact-touch.png`) });
      await page.getByRole("button", { name: "Filters", exact: true }).tap();
      await page.getByLabel("Result", { exact: true }).selectOption("failed");
      await expect.poll(() => state.logQueries.at(-1)).toContain("status=failed");
      await page.getByRole("button", { name: "Filters, 1 active" }).tap();
      await expect(page.getByLabel("Result", { exact: true })).toBeHidden();
      await page.getByRole("button", { name: "Older", exact: true }).first().tap();
      await expect.poll(() => state.logQueries.at(-1)).toContain("offset=25");
      await page.getByRole("button", { name: "Newer", exact: true }).first().tap();
      await rows.first().getByRole("button").tap();
      const dialog = page.getByRole("dialog", { name: "Activity details" });
      await expect(dialog.locator(".mobile-log-detail-links .transaction-link")).toHaveAttribute("href", `https://etherscan.io/tx/${tx}`);
      await expect(dialog.locator(".mobile-log-detail-links a")).toHaveCount(2);
      await expect(dialog.locator(".mobile-log-detail-auction a")).toHaveAttribute("href", `https://etherscan.io/address/${AUCTION}`);
      const copy = dialog.locator(".mobile-log-detail-auction .copy-trigger");
      await copy.tap();
      await expect(copy).toHaveClass(/is-copied/);
      await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
      expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
      await refreshOnFocus(page);
      await expect(dialog).toBeVisible();
      await expect(page).toHaveURL(/kick_id=1/);
      await page.keyboard.press("Escape");
      await expect(rows.first().getByRole("button")).toBeFocused();
      await rows.nth(1).getByRole("button").tap();
      await expect(dialog.locator(".error-text")).toHaveText("Receipt reverted");
      await expect(dialog.locator(".mobile-log-detail-links")).toContainText("No transaction");
      await page.keyboard.press("Escape");
      for (const width of [320, 430, 780, 960]) {
        await page.setViewportSize({ width, height: 844 });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await expect(page.getByRole("button", { name: "Filters, 1 active" })).toBeVisible();
      }
    } finally { await context.close(); }
  });
}

test("mobile log deep links retain large values, source identity and focus at text zoom", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    const state = await mockPublicApi(page);
    state.logsData = { total: 1, hasMore: false, kicks: [event(1, { sourceName: "LongUnbrokenSourceName".repeat(5), usdValue: "123456789012345.67" })] };
    await page.goto("/logs?kick_id=1");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await page.addStyleTag({ content: "body, button { font-size: 24px !important; } .mobile-log-open { line-height: 1.5 !important; }" });
    await expect(page.locator(".log-usd-cell")).toHaveText("$123,456,789,012,345.67");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "Show details for log 1", exact: true }).tap();
    await expect(page.getByRole("dialog")).toContainText("LongUnbrokenSourceName".repeat(5));
    expect(await page.locator(".kick-modal-body").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  } finally { await context.close(); }
});
