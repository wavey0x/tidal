import { test, expect } from "@playwright/test";
import { contrastRatio, mockPublicApi, WANT, AUCTION, refreshOnFocus } from "./fixtures";

const address = (index) => `0x${index.toString(16).padStart(40, "0")}`;
const tx = `0x${"ab".repeat(32)}`;
const token = (symbol, amount, index = 101, extra = {}) => ({
  tokenAddress: address(index),
  tokenSymbol: symbol,
  normalizedBalance: amount,
  tokenPriceUsd: "1",
  tokenDecimals: 18,
  ...extra,
});
const makeRow = (index, name, balances, extra = {}) => ({
  sourceType: "strategy",
  sourceAddress: address(index),
  sourceName: name,
  contextAddress: address(index + 1000),
  contextSymbol: "yvUSDC",
  contextName: "Yearn USDC Vault",
  active: true,
  depositLimit: "1",
  wantAddress: WANT,
  wantSymbol: "crvDOLA",
  auctionAddress: AUCTION,
  auctionVersion: "1.0.4",
  scannedAt: new Date().toISOString(),
  kicks: [
    {
      txHash: tx,
      createdAt: new Date(Date.now() - 86400000).toISOString(),
      operationType: "kick",
      status: "CONFIRMED",
      auctionAddress: AUCTION,
    },
  ],
  balances,
  ...extra,
});

async function fixture(page) {
  const state = await mockPublicApi(page);
  state.latestScanAt = new Date().toISOString();
  state.rows = [
    makeRow(1, "Curve-crvDOLA", [token("CRV", "685.02")]),
    makeRow(2, "Curve-BOLDUSDC", [token("BOLD", "183.84", 102), token("CRV", "113.24")], {
      wantSymbol: "BOLDUSDC",
    }),
    makeRow(3, "Curve-ETH MATIC-f", [token("CRV", "283.12")], { auctionAddress: null, kicks: [] }),
    makeRow(4, "Curve-OETHAstETH", [
      token("CRV", "264.90", 101, {
        kickPrepareStatus: "PAUSED",
        kickPrepareReason: "AUCTION_PRICE_GRANULARITY",
      }),
    ]),
    makeRow(5, "Unknown price strategy with a very long name that must remain usable", [
      token("UNPRICED-LONG-SYMBOL", "123", 103, { tokenPriceUsd: null }),
    ]),
    makeRow(6, "Dust only", [token("DUST", "0.001", 104)]),
    makeRow(7, "Retired strategy", [token("CRV", "9999")], { depositLimit: "0" }),
    makeRow(8, "Fee burner", [token("CRV", "123")], { sourceType: "fee_burner" }),
  ];
  return state;
}

for (const theme of ["light", "dark"]) {
  test(`${theme}: expanded rewards replace the summary without repeating tokens or values`, async ({
    page,
    context,
  }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    const state = await fixture(page);
    // A non-unit price makes USD/token switching distinguishable.
    state.rows[0].balances[0].tokenPriceUsd = "2";
    await page.goto("/");
    const single = page.locator(`[data-strategy="${address(1)}"]`);
    const multi = page.locator(`[data-strategy="${address(2)}"]`);
    const caretX = await single
      .locator(".chevron-toggle")
      .first()
      .evaluate((node) => {
        const bounds = node.getBoundingClientRect();
        return bounds.x + bounds.width / 2;
      });
    const logoX = await single.locator(".reward-logos").evaluate((node) => node.getBoundingClientRect().x);
    const valueRight = await single
      .locator(".reward-total")
      .evaluate((node) => node.getBoundingClientRect().right);
    await single.getByRole("button", { name: /Expand rewards/ }).click();
    await expect(single.getByRole("button", { name: /Collapse rewards/ })).toBeFocused();
    await expect(single.locator(".reward-logos, .reward-caption, .reward-breakdown-total")).toHaveCount(0);
    await expect(single.locator(".token-item")).toHaveCount(1);
    await expect(single.getByText("$1,370.04", { exact: true })).toHaveCount(1);
    const dimensions = await single.evaluate((node) => ({
      height: node.getBoundingClientRect().height,
      caretX: (() => {
        const bounds = node.querySelector(".reward-summary-button .chevron-toggle").getBoundingClientRect();
        return bounds.x + bounds.width / 2;
      })(),
      logoX: node.querySelector(".token-logo, .token-logo-placeholder").getBoundingClientRect().x,
      valueRight: node.querySelector(".token-balance").getBoundingClientRect().right,
    }));
    expect(dimensions.height).toBeLessThanOrEqual(56);
    expect(dimensions.caretX).toBeCloseTo(caretX, 3);
    expect(dimensions.logoX).toBe(logoX);
    expect(dimensions.valueRight).toBe(valueRight);
    const copy = single.getByRole("button", { name: "Copy token address for CRV", exact: true });
    await copy.click();
    await expect(copy).toHaveClass(/is-copied/);
    expect((await page.evaluate(() => navigator.clipboard.readText())).toLowerCase()).toBe(address(101));
    await multi.getByRole("button", { name: /Expand rewards/ }).click();
    await expect(multi.locator(".token-item")).toHaveCount(2);
    await expect(multi.locator(".reward-logos, .reward-caption")).toHaveCount(0);
    await expect(multi.getByText("$297.08", { exact: true })).toHaveCount(1);
    await expect(multi.locator(".reward-total-label")).toHaveText("Total");
    expect(await contrastRatio(multi.locator(".reward-total-label"))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`${theme}-reward-breakdowns.png`) });
    await single.locator(".token-balance-button").click();
    await expect(single.locator(".token-balance")).toHaveText("685.02");
    await expect(multi.locator(".reward-total-label")).toHaveText("Total USD");
    await single.locator(".token-balance-button").click();
    for (const width of [780, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(single.getByText("$1,370.04", { exact: true })).toHaveCount(1);
    }
    await page.screenshot({ path: testInfo.outputPath(`${theme}-reward-breakdowns-mobile.png`) });
    await single.getByRole("button", { name: /Collapse rewards/ }).focus();
    await page.keyboard.press("Enter");
    await expect(single.getByRole("button", { name: /Expand rewards/ })).toBeFocused();
    await expect(single.locator(".reward-total")).toHaveText("$1,370.04");
  });

  test(`${theme}: compact ledger, true explorer links, aligned rewards, and responsive layout`, async ({
    page,
  }, testInfo) => {
    await page.emulateMedia({ colorScheme: theme });
    await fixture(page);
    await page.goto("/");
    const rows = page.locator(".strategy-row");
    await expect(rows).toHaveCount(5);
    await expect(page.locator(".result-count")).toHaveText("5 of 7 strategies");
    await expect(page.getByRole("columnheader", { name: "Last Scan", exact: true })).toHaveCount(0);
    const first = rows.first();
    await expect(first.locator(".history-cell .transaction-link")).toHaveAttribute(
      "href",
      `https://etherscan.io/tx/${tx}`
    );
    await expect(first.locator(".history-cell .transaction-link")).toHaveAttribute("target", "_blank");
    await expect(first.locator(".transaction-link svg")).toHaveCount(1);
    await expect(first.locator(".auction-address-row a")).toHaveAttribute(
      "href",
      `https://etherscan.io/address/${AUCTION}`
    );
    const measurements = await rows.evaluateAll((nodes) =>
      nodes.slice(0, 4).map((node) => ({
        height: node.getBoundingClientRect().height,
        caret: node.querySelector(".reward-summary-button .chevron-toggle").getBoundingClientRect().x,
        tokens: node.querySelector(".reward-logos").getBoundingClientRect().x,
        value: node.querySelector(".reward-total").getBoundingClientRect().right,
      }))
    );
    expect(measurements[0].height).toBeLessThanOrEqual(56);
    for (const key of ["caret", "tokens", "value"])
      expect(new Set(measurements.map((entry) => entry[key])).size).toBe(1);
    await expect(rows.nth(3).locator(".reward-caption")).toContainText("paused");
    await expect(rows.last().locator(".reward-total")).toHaveText("?");
    await expect(page.locator(".deploy-cta")).toHaveCSS(
      "color",
      theme === "light" ? "rgb(148, 98, 19)" : "rgb(229, 184, 109)"
    );
    for (const selector of [
      ".deploy-cta",
      ".auction-version-badge",
      ".reward-paused",
      ".transaction-link",
      ".reward-symbols",
      "#activity-heading",
    ]) {
      expect(await contrastRatio(page.locator(selector).first()), selector).toBeGreaterThanOrEqual(4.5);
    }
    await page.screenshot({ path: testInfo.outputPath(`${theme}-ledger-desktop.png`), fullPage: true });
    for (const width of [1024, 780, 736, 480, 360, 320]) {
      await page.setViewportSize({ width, height: 1200 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const bounds = await rows.evaluateAll((nodes) =>
        nodes.map((node) => node.querySelector(".reward-total").getBoundingClientRect().right)
      );
      expect(Math.max(...bounds)).toBeLessThanOrEqual(width);
    }
    await page.screenshot({ path: testInfo.outputPath(`${theme}-ledger-mobile.png`), fullPage: true });
  });
}

test("sorting, expansion, filters, and scroll survive refresh without moving the row being used", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/");
  const first = page.locator(`[data-strategy="${address(1)}"]`);
  await first.getByRole("button", { name: /Expand rewards/ }).click();
  await expect(first.locator(".reward-breakdown")).toBeVisible();
  await page.getByRole("button", { name: "Sort rewards ascending" }).click();
  await expect(page.locator("#rewards-heading")).toHaveAttribute("aria-sort", "ascending");
  await expect(first.locator(".reward-breakdown")).toBeVisible();
  await page.getByRole("button", { name: "Sort rewards descending" }).click();
  await first.hover();
  state.rows[1].balances[0].normalizedBalance = "9999";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.locator(`[data-strategy="${address(2)}"] .reward-total`)).toHaveText("$10,112.24");
  await expect(page.locator(".strategy-row").first()).toHaveAttribute("data-strategy", address(1));
  await first.getByRole("button", { name: /Collapse rewards/ }).focus();
  await page.getByRole("searchbox").fill("BOLDUSDC");
  await expect(page.locator(".strategy-row")).toHaveCount(1);
  await refreshOnFocus(page);
  await expect(page.getByRole("searchbox")).toHaveValue("BOLDUSDC");
  await expect(page.locator(".result-count")).toHaveText("1 of 7 strategies");
  await page.getByRole("searchbox").fill("");
  await expect(first.locator(".reward-breakdown")).toBeVisible();
});

test("unknown and dust values, additive filters, stale exceptions, and explicit details", async ({
  page,
}) => {
  const state = await fixture(page);
  state.rows[0].scannedAt = new Date(Date.now() - 3600000).toISOString();
  state.rows[0].kickGuardDisabled = true;
  await page.goto("/");
  await expect(page.getByText("Older scan", { exact: true })).toBeVisible();
  await expect(page.getByText("Kicks disabled", { exact: true })).toBeVisible();
  await page.getByLabel("Include zero rewards").check();
  await expect(page.locator(`[data-strategy="${address(6)}"] .reward-empty`)).toContainText("$0.00");
  await page.getByLabel("Include retired").check();
  await expect(page.locator(".strategy-row")).toHaveCount(7);
  await page.getByRole("button", { name: "Show details for Curve-crvDOLA", exact: true }).click();
  await expect(page.locator(".strategy-detail-grid")).toBeVisible();
  await expect(page.locator(".strategy-detail-entity").first()).toContainText("yvUSDC");
  await page.getByRole("button", { name: "Hide details for Curve-crvDOLA", exact: true }).click();
  await expect(page.locator(".strategy-detail-grid")).toHaveCount(0);
  await page.getByRole("combobox", { name: "Filter by reward token" }).selectOption(address(102));
  await expect(page.locator(".strategy-row")).toHaveCount(1);
  state.rows[1].balances = [];
  await refreshOnFocus(page);
  await expect(page.getByRole("combobox", { name: "Filter by reward token" })).toHaveValue(address(102));
  await expect(page.locator(".empty")).toBeVisible();
});

test("sticky column headers and scroll position hold on a long live table", async ({ page }) => {
  const state = await fixture(page);
  state.rows = Array.from({ length: 138 }, (_, index) =>
    makeRow(index + 1, `Strategy ${index}`, [token("CRV", String(1000 - index))])
  );
  await page.goto("/");
  await expect(page.locator(".strategy-row")).toHaveCount(138);
  await page.evaluate(() => window.scrollTo(0, 1200));
  await expect
    .poll(() =>
      page.locator("#strategy-heading").evaluate((node) => Math.round(node.getBoundingClientRect().top))
    )
    .toBe(0);
  const scroll = await page.evaluate(() => window.scrollY);
  state.rows[0].balances[0].normalizedBalance = "1001";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.locator(".reward-total").first()).toHaveText("$1,001.00");
  expect(await page.evaluate(() => window.scrollY)).toBe(scroll);
});

test("large totals and long tokens remain accessible at phone width", async ({ page }) => {
  const state = await fixture(page);
  state.rows = [
    makeRow(1, "VeryLongStrategyNameWithoutNaturalBreaksForTheLayout", [
      token("LONG-TOKEN-SYMBOL", "12345678.90"),
      token("SECOND", "1", 102),
    ]),
  ];
  await page.setViewportSize({ width: 320, height: 1100 });
  await page.goto("/");
  await expect(page.locator(".reward-total")).toHaveText("$12,345,679.90");
  await page.getByRole("button", { name: /Expand rewards/ }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator(".token-balance").first()).toHaveText("$12,345,678.90");
});

test("history expansion and token image fallback remain independent of strategy details", async ({
  page,
}) => {
  const state = await fixture(page);
  state.rows[0].kicks.push({ ...state.rows[0].kicks[0], txHash: `0x${"cd".repeat(32)}` });
  state.rows[0].balances[0].tokenLogoUrl = "/auctionscan-favicon.svg"; // Local image fixture, no external fetch.
  state.rows[1].balances[0].tokenLogoUrl = "https://invalid.example/missing.png";
  await page.goto("/");
  const first = page.locator(".strategy-row").first();
  await expect(first.locator(".reward-logos img")).toHaveAttribute("src", "/auctionscan-favicon.svg");
  await expect
    .poll(() => first.locator(".reward-logos img").evaluate((node) => node.naturalWidth))
    .toBeGreaterThan(0);
  await expect(
    page.locator(".strategy-row").nth(1).locator(".reward-logos .token-logo-placeholder")
  ).toHaveCount(2);
  await first.getByRole("button", { name: "Expand kick history", exact: true }).click();
  await expect(first.locator(".kick-row")).toHaveCount(2);
  await expect(page.locator(".strategy-detail-grid")).toHaveCount(0);
  await refreshOnFocus(page);
  await expect(first.locator(".kick-row")).toHaveCount(2);
  await first.getByRole("button", { name: "Collapse kick history", exact: true }).click();
  await expect(first.locator(".kick-row")).toHaveCount(1);
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: expanded activity is an aligned single-line ledger with working explorer links`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: theme });
    const state = await fixture(page);
    state.rows[0].kicks = Array.from({ length: 5 }, (_, index) => ({
      ...state.rows[0].kicks[0],
      createdAt: new Date(Date.now() - (index ? index * 86400000 : 43200000)).toISOString(),
      txHash: `0x${String(index + 1).repeat(64)}`,
    }));
    await page.goto("/");
    const first = page.locator(".strategy-row").first();
    await first.getByRole("button", { name: "Expand kick history", exact: true }).click();
    const history = first.locator(".kick-history");
    await expect(history.locator(".kick-row")).toHaveCount(5);
    await expect(history.locator(".transaction-prefix")).toHaveCount(0);
    await expect(history.getByText("less", { exact: true })).toHaveCount(0);
    await expect(history.locator(".transaction-link").first()).toHaveAttribute("href", `https://etherscan.io/tx/0x${"1".repeat(64)}`);
    await expect(history.getByRole("link", { name: "View on AuctionScan" })).toHaveCount(5);
    await expect(history.locator("time").first()).toHaveAttribute("aria-label", "12 hours ago");
    for (const width of [1440, 800, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      const bounds = await history.locator(".kick-row-inner").evaluateAll(rows => rows.map(row => {
        const time = row.querySelector("time").getBoundingClientRect();
        const link = row.querySelector(".transaction-link").getBoundingClientRect();
        return { x: link.x, timeY: time.y + time.height / 2, linkY: link.y + link.height / 2 };
      }));
      expect(new Set(bounds.map(b => Math.round(b.x))).size).toBe(1);
      for (const bound of bounds) expect(Math.abs(bound.timeY - bound.linkY)).toBeLessThan(2);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await history.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${theme}-history-${width}.png`) });
    }
    expect(await contrastRatio(history.locator("time").first())).toBeGreaterThanOrEqual(4.5);
    await expect(page.locator(".brand-logo")).toHaveAttribute("src", `/tidal-logo-${theme}.svg`);
    await expect.poll(() => page.locator(".brand-logo").evaluate(node => node.naturalWidth)).toBeGreaterThan(0);
  });
}

test("touch targets and mobile detail focus stay usable without accidental navigation", async ({
  browser,
}, testInfo) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    permissions: ["clipboard-read", "clipboard-write"],
    colorScheme: "dark",
  });
  const page = await context.newPage();
  await fixture(page);
  await page.goto("http://127.0.0.1:5182/");
  const first = page.locator(".strategy-row").first();
  const copy = first.locator(".strategy-identity-cell .copy-trigger");
  const box = await copy.boundingBox();
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(44);
  await copy.tap();
  await expect(copy).toHaveClass(/is-copied/);
  await expect(page.locator(".strategy-detail-grid")).toHaveCount(0);
  await first.getByRole("button", { name: /Expand rewards/ }).tap();
  const rewardCopy = first.getByRole("button", { name: "Copy token address for CRV", exact: true });
  await rewardCopy.tap();
  await expect(rewardCopy).toHaveClass(/is-copied/);
  const collapse = first.getByRole("button", { name: /Collapse rewards/ });
  const collapseBox = await collapse.boundingBox();
  expect(collapseBox.width).toBeGreaterThanOrEqual(24);
  expect(collapseBox.height).toBeGreaterThanOrEqual(44);
  await collapse.tap();
  await expect(first.locator(".reward-breakdown")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("dark-ledger-touch.png"), fullPage: true });
  await first.getByRole("button", { name: "Show details for Curve-crvDOLA", exact: true }).tap();
  const dialog = page.getByRole("dialog", { name: "Strategy details", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Close details" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("dark-mobile-details.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(
    first.getByRole("button", { name: "Show details for Curve-crvDOLA", exact: true })
  ).toBeFocused();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
  await context.close();
});
