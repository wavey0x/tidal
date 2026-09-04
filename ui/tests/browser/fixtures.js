export const STRATEGY = "0x1111111111111111111111111111111111111111";
export const TOKEN = "0x2222222222222222222222222222222222222222";
export const WANT = "0x3333333333333333333333333333333333333333";
export const AUCTION = "0x4444444444444444444444444444444444444444";

export async function mockPublicApi(page) {
  const state = { balance: "1", failed: false, dashboardReads: 0, logReads: [], alertReads: 0, holdDashboard: null, warnings: [] };
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.hostname === "127.0.0.1" ? route.continue() : route.abort();
  });
  await page.route("**/api/v1/tidal/**", async (route) => {
    const request = route.request();
    if (request.headers().authorization) throw new Error("Browser sent an operator credential");
    const url = new URL(request.url());
    let data;
    if (url.pathname.endsWith("/dashboard")) {
      state.dashboardReads += 1;
      if (state.holdDashboard) await state.holdDashboard;
      if (state.failed) return route.fulfill({ status: 503, json: { detail: "fixture unavailable" } });
      data = {
        latestScanAt: "2026-09-04T20:00:00Z",
        rows: [{
          sourceType: "strategy", sourceAddress: STRATEGY, sourceName: "Fixture Strategy", active: true,
          wantAddress: WANT, wantSymbol: "USDC", auctionAddress: state.auctionAddress || null,
          scannedAt: "2026-09-04T20:00:00Z", depositLimit: "1", kicks: [],
          balances: [{ tokenAddress: TOKEN, tokenSymbol: "REWARD", normalizedBalance: state.balance,
            tokenPriceUsd: "1.25", tokenDecimals: 18 }],
        }],
      };
    } else if (url.pathname.endsWith("/logs/kicks")) {
      const offset = Number(url.searchParams.get("offset") || "0");
      state.logReads.push(offset);
      data = { total: 26, hasMore: offset === 0, kicks: [{ id: offset + 1, operationType: "kick", status: "CONFIRMED",
        createdAt: "2026-09-04T20:00:00Z", tokenSymbol: `REWARD${state.balance}`, wantSymbol: "USDC",
        sourceAddress: STRATEGY, sourceName: "Fixture Strategy", auctionAddress: AUCTION, usdValue: "1.25" }] };
    } else if (url.pathname.endsWith("/alerts")) {
      state.alertReads += 1;
      data = { items: [], needsActionCount: Number(state.balance), evaluatedAt: "2026-09-04T20:00:00Z" };
    } else {
      return route.fulfill({ status: 404, json: { detail: "Unmocked fixture route" } });
    }
    return route.fulfill({ json: { status: "ok", warnings: state.warnings, data } });
  });
  return state;
}
