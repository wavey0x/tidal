import { test, expect } from "@playwright/test";
import { mockPublicApi, contrastRatio, STRATEGY, TOKEN, WANT, AUCTION, refreshOnFocus } from "./fixtures";

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
  sourceAddress: STRATEGY, sourceName: "StrategyCurveBoostedFactory-BOLDUSDC", auctionAddress: AUCTION, usdValue: "297.08",
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
    await expect(first.locator(".row-primary")).toHaveText("Curve-BOLDUSDC");
    await expect(first.locator(".row-primary")).toHaveAttribute("title", "StrategyCurveBoostedFactory-BOLDUSDC");
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
    await expect(detail).toContainText("StrategyCurveBoostedFactory-BOLDUSDC");
    await expect(detail).toContainText("1500000000000000000");
    await expect(detail.locator(".quote-summary-ledger dt")).toHaveText(["High", "Low", "Median"]);
    await expect(detail.locator(".quote-summary-ledger dd")).toHaveText(["1.25 crvUSD", "1.25 crvUSD", "1.25 crvUSD"]);
    const execution = await detail.locator(".log-detail-group").first().boundingBox();
    const support = await detail.locator(".log-detail-support").boundingBox();
    expect(execution.y).toBe(support.y);
    expect(support.x).toBeGreaterThan(execution.x + execution.width);
    expect((await detail.boundingBox()).height).toBeLessThan(410);
    const fields = await detail.locator(".log-detail-group").first().locator(".kick-detail-value").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().x));
    expect(new Set(fields).size).toBe(1);
    expect(await contrastRatio(detail.locator(".kick-detail-label").first())).toBeGreaterThanOrEqual(4.5);
    expect(await contrastRatio(detail.locator(".kick-detail-value").first())).toBeGreaterThanOrEqual(4.5);
    await expect(detail.getByRole("link", { name: "AuctionScan", exact: true })).toHaveAttribute("href", /auctionscan\.info/);
    await detail.locator(".provider-details summary").click();
    await expect(detail.locator(".provider-ledger")).toContainText("1.25 crvUSD · ok");
    await expect(detail.locator(".provider-ledger")).toContainText("timeout");
    for (const glyph of await detail.locator(".kick-external-link .outbound-link-glyph").all()) {
      const box = await glyph.boundingBox();
      expect(box.width).toBe(11);
      expect(box.height).toBe(11);
    }
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
    expect(await page.locator(".kick-modal-body").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-log-details-mobile.png`), animations: "disabled" });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(first.getByRole("button", { name: "Show details for log 1", exact: true })).toBeFocused();
  });
}

test("compact log details preserve failures, raw units and long values on touch screens", async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 1000 }, isMobile: true, hasTouch: true, colorScheme: "dark" });
  try {
    const page = await context.newPage();
    const state = await mockPublicApi(page);
    state.logsData = { total: 2, hasMore: false, kicks: [
      log(1, { operationType: "settle", status: "ERROR", errorMessage: "Receipt reverted", quoteResponseJson: "malformed", txHash: null }),
      log(2, { sourceName: "A-very-long-strategy-name-".repeat(4), normalizedBalance: "12345678901234567890.12", runId: "api-action:" + "a".repeat(80),
        quoteResponseJson: JSON.stringify({ providers: { ["long-provider-name-".repeat(4)]: { amount_out: "123456789012345678901234567890", status: "ok" } },
          summary: { high_amount_out: "123456789012345678901234567890" } }) }),
    ] };
    await page.goto("/logs");
    await page.getByRole("button", { name: "Show details for log 1", exact: true }).click();
    const detail = page.locator(".log-detail-content");
    await expect(detail.locator("h3")).toHaveText(["Execution", "Diagnostics"]);
    await expect(detail.locator(".error-text")).toHaveText("Receipt reverted");
    await expect(detail.locator(".transaction-link")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Show details for log 2", exact: true }).click();
    await expect(detail).toContainText("12,345,678,901,234,567,890.12 CRV");
    await expect(detail).toContainText("123456789012345678901234567890 raw units");
    await expect(detail).toContainText("a".repeat(80));
    await detail.locator(".provider-details summary").click();
    await expect(detail.locator(".provider-ledger")).toContainText("raw units · ok");
    expect(await page.locator(".kick-modal-body").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    for (const button of await detail.locator(".copy-trigger").all()) expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(44);
    await page.screenshot({ path: testInfo.outputPath("log-details-touch-edge-cases.png"), animations: "disabled" });
  } finally { await context.close(); }
});

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
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
  expect(await ids()).toEqual(before);
  await page.locator('[data-log-id="26"]').getByRole("button", { name: "Show details for log 26", exact: true }).click();
  await expect(page).toHaveURL(/kick_id=26/);
  await refreshOnFocus(page);
  await expect(page.locator(".log-detail-content")).toBeVisible();
  expect(await ids()).toEqual(before);
  state.logsFailed = true;
  await refreshOnFocus(page);
  await expect(page.getByText("Unable to load logs", { exact: true })).toBeVisible();
  await expect(page.locator(".refresh-status")).not.toContainText("Data may be stale");
  await expect(page.locator(".log-detail-content")).toBeVisible();
  state.logsFailed = false;
  await page.reload();
  await expect(page.locator(".log-detail-content")).toBeVisible();
  const position = await page.evaluate(() => scrollY);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
  expect(await page.evaluate(() => scrollY)).toBe(position);
  await page.getByRole("button", { name: "Show all logs", exact: true }).click();
  await expect.poll(ids).toHaveLength(25);
  await expect(page.getByLabel("Result", { exact: true })).toHaveValue("confirmed");
  await expect(page).toHaveURL(/offset=25/);
  await page.getByRole("button", { name: "Newer", exact: true }).first().click();
  await expect.poll(() => state.logReads.at(-1)).toBe(0);
  await expect(page).toHaveURL(/q=CRV/);
});

test("log headings stay sticky and refresh preserves scroll; browser Back restores a selected event", async ({ page }) => {
  const state = await mockPublicApi(page);
  state.logsData = url => {
    const selected = Number(url.searchParams.get("kick_id"));
    return { total: selected ? 1 : 25, hasMore: false, kicks: selected ? [log(selected)] : Array.from({ length: 25 }, (_, index) => log(index + 1)) };
  };
  await page.goto("/logs");
  await expect(page.locator(".kick-log-row")).toHaveCount(25);
  await page.evaluate(() => scrollTo(0, 600));
  const scroll = await page.evaluate(() => scrollY);
  expect((await page.locator("#log-time").boundingBox()).y).toBeLessThanOrEqual(1);
  const reads = state.logReads.length;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => state.logReads.length).toBeGreaterThan(reads);
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
  expect(await page.evaluate(() => scrollY)).toBe(scroll);
  await page.getByRole("button", { name: "Show details for log 12", exact: true }).click();
  await expect(page.locator(".log-detail-content")).toBeVisible();
  await page.getByRole("button", { name: "Hide details for log 12", exact: true }).click();
  await page.goBack();
  await expect(page.locator(".log-detail-content")).toContainText("run-12");
  await expect(page.getByRole("button", { name: "Show all logs", exact: true })).toBeVisible();
});

const retryAlert = () => ({
  id: "retry-fixture", kind: "auction_retry", status: "needs_action", severity: "warning", title: "Auction needs a deliberate retry",
  summary: "685.02 CRV → crvUSD · $297.08\n3 no-fills · automation paused", openedAt: new Date(Date.now() - 3 * 86400000).toISOString(),
  updatedAt: now(), retryAt: new Date(Date.now() - 86400000).toISOString(),
  scope: { sourceType: "strategy", sourceAddress: STRATEGY, auctionAddress: AUCTION, tokenAddress: TOKEN, kickId: 42 },
  nextAction: { instruction: "Review the round evidence before a deliberate scoped retry.", command: `tidal kick --auction ${AUCTION} --token ${TOKEN}` },
  links: { logs: "/logs?kick_id=42", etherscan: `https://etherscan.io/tx/${hash}`, auctionScan: `https://auctionscan.info/auction/1/${AUCTION}` },
  evidence: { decision: "MANUAL_REVIEW", reasonCode: "NO_FILL_LIMIT", consecutiveNoFills: 3, retryOrdinal: 3, retryTotal: 3,
    rounds: [42, 40, 38].map(id => ({ kickId: id, closeId: id + 1, outcome: "NO_FILL", reasonCode: "FULL_RECOVERY", kickAt: new Date(Date.now() - id * 3600000).toISOString(), closeAt: now(), kickTxHash: hash, closeTxHash: `0x${"cd".repeat(32)}`,
      placedAmount: "685020000000000000000", requestedAmount: "700000000000000000000", recoveredAmount: "685020000000000000000", quoteAmount: "297.08", minimumQuote: "280.22",
      providers: { spreadPct: "0.25", entries: [{ name: "curve", status: "ok", amountOut: "297080000000000000000" }, { name: "aggregator", status: "timeout", amountOut: null }] },
    })),
  },
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: alerts show next actions with compact evidence, explicit units and copy-only retries`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    const state = await mockPublicApi(page);
    const issue = retryAlert();
    state.alertsData = { needsActionCount: 1, evaluatedAt: now(), latestSuccessfulScanAt: now(), items: [issue, {
      id: "watch-fixture", kind: "scanner_health", status: "watching", severity: "info", title: "Scanner recovery is being watched", summary: "The most recent scan completed.", openedAt: now(),
      nextAction: { instruction: "Wait for the next scheduled scan." }, evidence: { lastScanId: 123, diagnostics: { provider: "fixture", status: "healthy" } },
    }] };
    const writes = [];
    page.on("request", request => { if (request.method() !== "GET") writes.push(request.method()); });
    await page.goto("/alerts");
    await expect(page.locator(".alert-counts")).toContainText("1 Needs action");
    await expect(page.locator(".alert-counts")).toContainText("1 Watching");
    await expect(page.locator(".refresh-status")).toHaveCount(1);
    await expect(page.locator(".refresh-status")).toContainText("Evaluated just now");
    const alert = page.getByRole("article", { name: issue.title, exact: true });
    const toggle = alert.getByRole("button", { name: `Show details for ${issue.title}`, exact: true });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(alert.locator(".alert-expanded-content")).toBeHidden();
    await expect(alert.getByRole("button", { name: "Copy retry command", exact: true })).toHaveCount(0);
    await expect(alert.locator(".alert-summary")).toContainText("3 no-fills · automation paused");
    expect((await alert.boundingBox()).height).toBeLessThan(85);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-alerts-collapsed.png`) });
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(alert.locator(".alert-expand")).toHaveAttribute("aria-expanded", "true");
    await expect(alert.locator(".alert-expanded-content")).toBeVisible();
    await expect(alert.locator(".alert-expanded-content")).toHaveCSS("border-top-width", "0px");
    await expect(alert.locator(".alert-expanded-content")).toHaveCSS("padding-top", "0px");
    await expect(alert.locator(".alert-addresses")).toHaveCSS("border-left-width", "0px");
    await expect(alert).toHaveCSS("border-bottom-width", "1px");
    const headerBox = await alert.locator(".alert-card-header").boundingBox();
    const expandedBox = await alert.locator(".alert-expanded-content").boundingBox();
    expect(expandedBox.y - headerBox.y - headerBox.height).toBe(8);
    await expect(alert.locator(".alert-next-action")).toContainText(issue.nextAction.instruction);
    await expect(alert.locator(".alert-age dt")).toHaveText("Opened");
    await expect(alert.locator(".alert-age time")).toHaveText("3 days ago");
    await expect(alert).not.toContainText("Observed");
    expect((await page.locator(".alerts-page").boundingBox()).width).toBe(960);
    const bodyBox = await alert.locator(".alert-body").boundingBox();
    const contextBox = await alert.locator(".alert-addresses").boundingBox();
    expect(contextBox.y).toBe(bodyBox.y);
    expect(contextBox.x).toBeGreaterThan(bodyBox.x + bodyBox.width);
    const addressColumns = await alert.locator(".alert-addresses dd").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().x));
    expect(new Set(addressColumns).size).toBe(1);
    await expect(alert.getByRole("link", { name: "Logs", exact: true })).toHaveAttribute("href", "/logs?kick_id=42");
    const copy = alert.getByRole("button", { name: "Copy retry command", exact: true });
    await copy.click();
    await expect(copy).toHaveClass(/is-copied/);
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(issue.nextAction.command);
    await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
    expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
    expect(await contrastRatio(alert.locator(".alert-kicker"))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-alerts.png`) });
    await alert.locator(".alert-details > summary").click();
    await expect(alert.locator(".alert-round-row")).toHaveCount(3);
    await expect(alert.locator(".alert-round-row").first().locator(".evidence-amount")).toHaveText(["6.85e+20", "6.85e+20", "297.08", "280.22"]);
    await expect(alert.locator(".evidence-note")).toContainText("token decimals are unavailable");
    await page.screenshot({ path: testInfo.outputPath(`${theme}-alert-evidence.png`) });
    await alert.getByRole("button", { name: "Show round details for log 42", exact: true }).click();
    const round = alert.locator(".alert-round-detail");
    await expect(round).toContainText("685020000000000000000");
    await expect(round).toContainText("Requested · raw sell units");
    await expect(round.locator(".transaction-link")).toHaveCount(2);
    await round.locator(".provider-details > summary").click();
    await expect(round).toContainText("297080000000000000000 raw buy units");
    await expect(round).toContainText("Agreement does not identify the cause");
    state.alertsData.evaluatedAt = now();
    state.alertsData.items[0].evidence.rounds[0].closeId = 99;
    await refreshOnFocus(page);
    await expect(round).toContainText("Close · log 99");
    await expect(round.locator(".provider-details")).toHaveAttribute("open", "");
    await alert.locator(".alert-expand").click();
    await expect(round).toBeHidden();
    await refreshOnFocus(page);
    await expect(alert.locator(".alert-expand")).toHaveAttribute("aria-expanded", "false");
    await alert.locator(".alert-expand").focus();
    await page.keyboard.press("Space");
    await expect(round).toBeVisible();
    await expect(round.locator(".provider-details")).toHaveAttribute("open", "");
    await page.setViewportSize({ width: 320, height: 1200 });
    await page.screenshot({ path: testInfo.outputPath(`${theme}-alert-mobile.png`) });
    const overflowing = await page.locator(".alerts-page *").evaluateAll(nodes => nodes.filter(node => !node.closest("thead") && node.getBoundingClientRect().right > innerWidth + .5).map(node => `${node.tagName}.${node.className}`));
    expect(overflowing).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    state.alertsFailed = true;
    await refreshOnFocus(page);
    await expect(page.getByText("Unable to load alerts", { exact: true })).toBeVisible();
    await expect(page.locator(".alert-health-warning")).toHaveCount(0);
    await expect(alert).toBeVisible();
    await expect(page.getByText("No operator action needed", { exact: true })).toHaveCount(0);
    expect(writes).toEqual([]);
  });
}

test("alerts describe snapshots without freshness banners or unverified health claims", async ({ page }) => {
  const state = await mockPublicApi(page);
  state.alertsData = { needsActionCount: 0, items: [], evaluatedAt: now(), latestSuccessfulScanAt: now() };
  await page.goto("/alerts");
  await expect(page.getByText("No alerts in this snapshot", { exact: true })).toBeVisible();
  state.alertsData.evaluatedAt = new Date(Date.now() + 20000).toISOString();
  await refreshOnFocus(page);
  await expect(page.locator(".refresh-status")).toContainText("Evaluated just now");
  await expect(page.locator(".refresh-updated")).toHaveAttribute("title", /UTC$/);
  for (const invalid of [
    { evaluatedAt: "2020-01-01T00:00:00Z" }, { latestSuccessfulScanAt: "2020-01-01T00:00:00Z" },
    { latestSuccessfulScanAt: null }, { evaluatedAt: "invalid" }, { items: null }, { needsActionCount: 1 },
  ]) {
    state.alertsData = { needsActionCount: 0, items: [], evaluatedAt: now(), latestSuccessfulScanAt: now(), ...invalid };
    await refreshOnFocus(page);
    await expect(page.locator(".alert-health-warning")).toHaveCount(0);
    const invalidResults = Object.hasOwn(invalid, "items") || Object.hasOwn(invalid, "needsActionCount");
    await expect(page.locator(".alerts-empty strong")).toHaveText(invalidResults ? "Alert results unavailable" : "No alerts in this snapshot");
    await expect(page.locator(".alert-counts strong")).toHaveText(invalidResults ? ["—", "—"] : ["0", "0"]);
    await expect(page.getByText("No operator action needed", { exact: true })).toHaveCount(0);
  }
  state.alertsFailed = true;
  await page.reload();
  await expect(page.getByText("Unable to load alerts", { exact: true })).toBeVisible();
  await expect(page.locator(".alerts-empty strong")).toHaveText("Alert results unavailable");
  await expect(page.locator(".alert-health-warning")).toHaveCount(0);
  await expect(page.getByText("No operator action needed", { exact: true })).toHaveCount(0);
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: fee burner keeps values close in a bounded left-aligned inventory`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    const state = await mockPublicApi(page);
    state.rows = [burner()];
    state.rows[0].kicks = Array.from({ length: 5 }, (_, index) => ({
      ...state.rows[0].kicks[0], txHash: `0x${String(index + 1).repeat(64)}`,
      createdAt: new Date(Date.now() - (index + 1) * 86400000).toISOString(),
    }));
    await page.goto("/fee-burner");
    const inventory = page.locator(".fee-burner-inventory");
    const table = inventory.locator(".fee-token-table");
    await expect(table.locator("thead th")).toHaveText(["Token", "Amount", "USD"]);
    await expect(table.locator(".fee-token-row")).toHaveCount(2);
    await expect(table.locator(".fee-token-amount")).toHaveText(["100.00", "20.00"]);
    await expect(table.locator(".fee-token-usd")).toHaveText(["$200.00", "$40.00"]);
    await expect(table.locator(".fee-inventory-total")).toHaveText("$240.00");
    await expect(page.locator(".refresh-status")).toHaveCount(1);
    await expect(page.getByRole("button", { name: /Collapse rewards|Expand rewards/ })).toHaveCount(0);
    await expect(inventory.locator(".fee-burner-context .entity-cell")).toContainText("yCRV Fee Burner");
    await expect(inventory.locator(".fee-burner-auction a")).toHaveAttribute("href", `https://etherscan.io/address/${AUCTION}`);
    const boxes = await Promise.all([table, inventory.locator(".fee-burner-context"), inventory.locator(".fee-burner-activity")].map(node => node.boundingBox()));
    expect(boxes[0].width).toBe(700);
    expect(boxes[0].x).toBe(boxes[1].x);
    expect(boxes[2].x).toBe(boxes[0].x);
    expect(boxes[2].width).toBe(boxes[0].width);
    expect(boxes[1].width).toBe(boxes[0].width);
    const refresh = await page.locator(".refresh-status").boundingBox();
    expect(refresh.x).toBe(boxes[1].x);
    const identity = await inventory.locator(".fee-burner-identity").boundingBox();
    const auction = await inventory.locator(".fee-burner-auction").boundingBox();
    expect(identity.y).toBe(auction.y);
    expect(auction.x - identity.x).toBeCloseTo(320, 0);
    await expect(page.getByRole("button", { name: "Refresh", exact: true })).toHaveCount(0);
    const columns = await table.locator("thead th").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().width));
    for (const [index, width] of [320, 190, 190].entries()) expect(columns[index]).toBeCloseTo(width, 0);
    expect(boxes[1].y + boxes[1].height).toBeLessThanOrEqual(boxes[0].y);
    expect(boxes[2].y).toBeGreaterThanOrEqual(boxes[0].y + boxes[0].height);
    expect((await table.locator(".fee-token-name").first().boundingBox()).x).toBe(boxes[0].x);
    expect((await table.locator(".fee-inventory-total").boundingBox()).x).toBe((await table.locator(".fee-token-usd").first().boundingBox()).x);
    const copy = table.getByRole("button", { name: "Copy token address for CRV", exact: true });
    await copy.click();
    await expect(copy).toHaveClass(/is-copied/);
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(TOKEN);
    await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
    expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
    expect(await contrastRatio(table.locator(".fee-token-amount").first())).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-fee-inventory.png`) });
    for (const width of [900, 700]) {
      await page.setViewportSize({ width, height: 1000 });
      const bounds = await table.boundingBox();
      const region = await inventory.boundingBox();
      expect(bounds.x).toBe(region.x);
      expect(bounds.width).toBe(Math.min(700, region.width));
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    const activity = inventory.getByRole("button", { name: "Show recent activity for yCRV Fee Burner", exact: true });
    await activity.focus();
    await page.keyboard.press("Enter");
    await expect(inventory.locator(".kick-row")).toHaveCount(5);
    await expect(inventory.locator(".fee-activity-list .transaction-link").first()).toHaveAttribute("href", `https://etherscan.io/tx/0x${"1".repeat(64)}`);
    await expect(inventory.getByRole("link", { name: "View on AuctionScan" })).toHaveCount(5);
    state.rows[0].balances[0].normalizedBalance = "110";
    await refreshOnFocus(page);
    await expect(table.locator(".fee-inventory-total")).toHaveText("$260.00");
    await expect(table.locator(".fee-token-amount").first()).toHaveText("110.00");
    await expect(inventory.locator(".kick-row")).toHaveCount(5);
    await page.getByRole("tab", { name: "Strategies", exact: true }).click();
    await page.getByRole("tab", { name: "Fee Burner", exact: true }).click();
    await expect(inventory.locator(".kick-row")).toHaveCount(5);
    await page.setViewportSize({ width: 320, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(table.locator(".fee-token-amount").first()).toBeVisible();
    await expect(table.locator(".fee-token-usd").first()).toBeVisible();
    const usdHeading = await table.locator("thead th").last().boundingBox();
    const usdCell = await table.locator(".fee-token-usd").first().boundingBox();
    expect(usdHeading.x + usdHeading.width).toBe(usdCell.x + usdCell.width);
    expect(await table.locator(".fee-token-amount").first().evaluate(node => getComputedStyle(node).textAlign)).toBe("left");
    await page.screenshot({ path: testInfo.outputPath(`${theme}-fee-inventory-mobile.png`) });
    state.failed = true;
    await refreshOnFocus(page);
    await expect(page.getByText("Unable to load dashboard", { exact: true })).toBeVisible();
    await expect(page.locator(".refresh-status")).not.toContainText("Data may be stale");
    await expect(table.locator(".fee-inventory-total")).toHaveText("$260.00");
    await expect(inventory.locator(".kick-row")).toHaveCount(5);
  });
}

test("fee inventory keeps unknown values and warnings honest without hiding large balances on touch screens", async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 1100 }, isMobile: true, hasTouch: true, colorScheme: "dark" });
  try {
    const page = await context.newPage();
    const state = await mockPublicApi(page);
    state.rows = [burner()];
    state.rows[0].balances = [
      balance("VERY-LONG-TOKEN-SYMBOL", "12345678901234567890.12", TOKEN, { tokenPriceUsd: null, auctionSellTokenStatus: "unknown" }),
      balance("PAUSED", "100", WANT, { kickPrepareStatus: "PAUSED", kickPrepareReason: "AUCTION_PRICE_GRANULARITY" }),
      balance("DISABLED", "50", AUCTION, { auctionSellTokenStatus: "disabled" }),
      balance("UNKNOWN-AMOUNT", null, STRATEGY),
    ];
    await page.goto("/fee-burner");
    await expect(page.locator(".fee-token-row")).toHaveCount(4);
    await expect(page.locator(".fee-inventory-total")).toHaveText("?");
    await expect(page.locator(".fee-token-amount").first()).toHaveText("12,345,678,901,234,567,890.12");
    await expect(page.locator(".fee-token-usd").first()).toHaveText("?");
    await expect(page.locator(".fee-token-amount").last()).toHaveText("?");
    await expect(page.locator(".fee-token-warnings")).toHaveText(["Auction status unknown", " Paused", "Not enabled in auction"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const button of await page.locator(".fee-token-table .copy-trigger, .fee-activity-toggle").all()) {
      expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(44);
    }
    await page.screenshot({ path: testInfo.outputPath("fee-inventory-touch-edge-cases.png") });
  } finally { await context.close(); }
});

test("fee inventories separate multiple burners and preserve loading, empty and unavailable states", async ({ page }) => {
  const state = await mockPublicApi(page);
  let release;
  state.holdDashboard = new Promise(resolve => { release = resolve; });
  state.rows = [burner(), { ...burner(), sourceAddress: AUCTION, sourceName: "Second burner", auctionAddress: null, kicks: [],
    balances: [balance("ONE", "1")] }];
  await page.goto("/fee-burner");
  await expect(page.getByRole("status", { name: "Loading fee burner inventory" })).toBeVisible();
  release();
  await expect(page.locator(".fee-burner-inventory")).toHaveCount(2);
  await expect(page.locator(".refresh-status")).toHaveCount(1);
  const second = page.getByRole("region", { name: "Second burner token inventory", exact: true });
  await expect(second.locator(".fee-burner-auction")).toContainText("No auction");
  await expect(second.locator(".fee-burner-activity")).toContainText("None recorded");
  await expect(second.locator(".fee-inventory-total")).toHaveCount(0);
  state.rows[1].balances = [balance("DUST", "0.001"), balance("ZERO", "0", WANT)];
  await refreshOnFocus(page);
  await expect(second.locator(".fee-token-row")).toHaveCount(0);
  await expect(second).toContainText("No balances above the visibility threshold.");
  state.rows = [];
  await refreshOnFocus(page);
  await expect(page.getByText("No fee burners are available.", { exact: true })).toBeVisible();
  state.failed = true;
  await page.reload();
  await expect(page.getByText("Fee burner inventory unavailable.", { exact: true })).toBeVisible();
  await expect(page.getByText("No fee burners are available.", { exact: true })).toHaveCount(0);
});
