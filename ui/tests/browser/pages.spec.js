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
