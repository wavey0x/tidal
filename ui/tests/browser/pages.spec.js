import { test, expect } from "@playwright/test";
import { mockPublicApi, contrastRatio, STRATEGY, TOKEN, WANT, AUCTION } from "./fixtures";

const hash = "0x" + "ab".repeat(32);
const now = () => new Date().toISOString();
const balance = (symbol, amount, tokenAddress = TOKEN, extra = {}) => ({
  tokenAddress, tokenSymbol: symbol, normalizedBalance: amount, tokenPriceUsd: "2", tokenDecimals: 18, ...extra,
});
const burner = () => ({
  sourceType: "fee_burner", sourceName: "yCRV Fee Burner", sourceAddress: STRATEGY,
  scannedAt: now(), auctionAddress: AUCTION, auctionVersion: "1.0.4", wantAddress: WANT, wantSymbol: "crvUSD",
  balances: [balance("CRV", "100"), balance("CVX", "20", WANT), balance("DUST", "0.001", AUCTION)],
  kicks: [{ id: 1, createdAt: now(), txHash: hash, operationType: "kick", status: "CONFIRMED", auctionAddress: AUCTION }],
});

const log = (id, extra = {}) => ({
  id, operationType: "kick", status: "CONFIRMED", createdAt: new Date(Date.now() - id * 3600000).toISOString(),
  tokenSymbol: "CRV", tokenAddress: TOKEN, wantSymbol: "crvUSD", wantAddress: WANT,
  sourceAddress: STRATEGY, sourceName: "Curve-BOLDUSDC", auctionAddress: AUCTION, usdValue: "297.08",
  txHash: `0x${id.toString(16).padStart(64, "0")}`, normalizedBalance: "685.02", blockNumber: 23000001,
  gasUsed: "141206", gasPriceGwei: "1.35", runId: `run-${id}`, minimumPrice: "1500000000000000000",
  startingPrice: "2.00", startPriceBufferBps: 1000, minimumQuote: "1.10", minPriceBufferBps: 500,
  quoteAmount: "1.50", stepDecayRateBps: 100, settleToken: TOKEN,
  quoteResponseJson: JSON.stringify({ tokenOutDecimals: 6, providers: { one: { status: "ok", amount_out: "1250000" }, two: { status: "timeout" } },
    summary: { requested_providers: 2, successful_providers: 1, high_amount_out: "1250000", low_amount_out: "1250000", median_amount_out: "1250000" },
    requestUrl: "https://api.example/quote" }), ...extra,
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: logs use six columns, explicit details and compact mobile events`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    const state = await mockPublicApi(page);
    state.logsData = { total: 4, hasMore: false, kicks: [log(1), log(2, { operationType: "settle", status: "ERROR", errorMessage: "Receipt reverted", txHash: null }), log(3, { usdValue: null, status: "DRY_RUN" }), log(4, { usdValue: "0" })] };
    await page.goto("/logs");
    await expect(page.locator(".kick-log-table th")).toHaveText(["Time", "Activity", "Source", "Auction", "Transaction", "USD"]);
    await expect(page.locator(".refresh-status")).toHaveCount(1);
    const first = page.locator('[data-log-id="1"]');
    await first.locator(".kick-time-cell").click();
    await expect(page.locator(".log-detail-content")).toHaveCount(0);
    await expect(page.locator('[data-log-id="3"] .log-usd-cell')).toHaveText("?");
    await expect(page.locator('[data-log-id="4"] .log-usd-cell')).toHaveText("$0.00");
    await expect(first.locator(".transaction-link")).toHaveAttribute("href", `https://etherscan.io/tx/${log(1).txHash}`);
    await expect(first.locator(".log-transaction-cell").getByLabel("View on AuctionScan")).toBeVisible();
    expect(await contrastRatio(first.locator(".status-badge"))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-logs.png`) });
    const expand = first.getByRole("button", { name: "Show details for log 1", exact: true });
    await expand.focus();
    await page.keyboard.press("Enter");
    const detail = page.locator(".log-detail-content");
    await expect(detail.locator("h3")).toHaveText(["Execution", "Pricing", "Diagnostics"]);
    await expect(detail).toContainText("run-1");
    await expect(detail).toContainText("1500000000000000000");
    await expect(detail).toContainText("High 1.25 crvUSD");
    await detail.locator(".provider-details summary").click();
    await expect(detail.locator(".provider-ledger")).toContainText("1.25 crvUSD · ok");
    await expect(detail.locator(".provider-ledger")).toContainText("timeout");
    const copy = detail.getByRole("button", { name: `Copy address ${TOKEN}`, exact: true }).first();
    await copy.click();
    await expect(copy).toHaveClass(/is-copied/);
    await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
    expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-log-details.png`) });
    await first.getByRole("button", { name: "Hide details for log 1", exact: true }).click();
    await page.setViewportSize({ width: 320, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-logs-mobile.png`) });
    await first.getByRole("button", { name: "Show details for log 1", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Activity details" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(first.getByRole("button", { name: "Show details for log 1", exact: true })).toBeFocused();
  });
}

test("log refresh pins the visible page during interaction and preserves open details, filters and deep links", async ({ page }) => {
  const state = await mockPublicApi(page);
  let version = 0;
  state.logsData = url => {
    const selected = Number(url.searchParams.get("kick_id"));
    const offset = Number(url.searchParams.get("offset") || 0);
    return { total: 51 + version, hasMore: !selected && offset < 50, kicks: selected ? [log(selected)] : Array.from({ length: 25 }, (_, index) => log(offset + index + 1 + version)) };
  };
  await page.goto("/logs?q=CRV&status=confirmed&offset=25");
  await expect(page.getByRole("searchbox", { name: "Search logs" })).toHaveValue("CRV");
  await expect(page.getByLabel("Result", { exact: true })).toHaveValue("confirmed");
  const ids = () => page.locator(".kick-log-row").evaluateAll(rows => rows.map(row => row.dataset.logId));
  await expect.poll(ids).toHaveLength(25);
  const before = await ids();
  await page.locator('[data-log-id="26"]').hover();
  version = 1;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => state.logReads.length).toBeGreaterThan(1);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  expect(await ids()).toEqual(before);
  await page.locator('[data-log-id="26"]').getByRole("button", { name: "Show details for log 26", exact: true }).click();
  await expect(page).toHaveURL(/kick_id=26/);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator(".log-detail-content")).toBeVisible();
  expect(await ids()).toEqual(before);
  state.logsFailed = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator(".refresh-status")).toContainText("Data may be stale");
  await expect(page.locator(".log-detail-content")).toBeVisible();
  state.logsFailed = false;
  await page.reload();
  await expect(page.locator(".log-detail-content")).toBeVisible();
  const position = await page.evaluate(() => scrollY);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  expect(await page.evaluate(() => scrollY)).toBe(position);
  await page.getByRole("button", { name: "Show all logs", exact: true }).click();
  await expect.poll(ids).toHaveLength(25);
  await expect(page.getByLabel("Result", { exact: true })).toHaveValue("confirmed");
  await expect(page).toHaveURL(/offset=25/);
  await page.getByRole("button", { name: "Newer", exact: true }).first().click();
  await expect.poll(() => state.logReads.at(-1)).toBe(0);
  await expect(page).toHaveURL(/q=CRV/);
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: fee burner ledger shares compact balances, freshness and responsive layout`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    const state = await mockPublicApi(page);
    state.rows = [burner()];
    await page.goto("/fee-burner");
    await expect(page.locator(".fee-burner-table th")).toHaveText(["Burner", "Auction", "Last activity", "BalancesUSD"]);
    const row = page.locator(".fee-burner-row");
    await expect(row.locator(".token-item")).toHaveCount(2);
    await expect(row.locator(".reward-total")).toHaveText("$240.00");
    await expect(page.locator(".refresh-status")).toHaveCount(1);
    await expect(page.locator(".fee-burner-card")).toHaveCount(0);
    const copy = row.getByRole("button", { name: "Copy token address for CRV", exact: true });
    await copy.click();
    await expect(copy).toHaveClass(/is-copied/);
    await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
    expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-fee-burner.png`) });
    await row.getByRole("button", { name: /Collapse rewards/ }).click();
    state.rows[0].balances[0].normalizedBalance = "110";
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(row.locator(".reward-total")).toHaveText("$260.00");
    await expect(row.locator(".reward-breakdown")).toBeHidden();
    await row.getByRole("button", { name: /Expand rewards/ }).click();
    await page.setViewportSize({ width: 320, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-fee-burner-mobile.png`) });
    state.failed = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.locator(".refresh-status")).toContainText("Data may be stale");
    await expect(row.locator(".reward-total")).toHaveText("$260.00");
  });
}
