export const STRATEGY = "0x1111111111111111111111111111111111111111";
export const TOKEN = "0x2222222222222222222222222222222222222222";
export const WANT = "0x3333333333333333333333333333333333333333";
export const AUCTION = "0x4444444444444444444444444444444444444444";

// Exercise the real automatic-refresh path without a manual refresh control.
export async function refreshOnFocus(page) {
  const { expect } = await import("@playwright/test");
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
  const path = new URL(page.url()).pathname;
  const endpoint = path === "/logs" ? "/logs/kicks" : path === "/alerts" ? "/alerts" : "/dashboard";
  const response = page.waitForResponse(result => new URL(result.url()).pathname.endsWith(endpoint));
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await response;
  await expect(page.locator(".refresh-status")).toHaveAttribute("aria-busy", "false");
}

export async function contrastRatio(locator) {
  return locator.evaluate((node) => {
    const rgba = (color) => color.match(/[\d.]+/g).map(Number);
    const blend = (front, back) => front.slice(0, 3).map((value, index) =>
      value * (front[3] ?? 1) + back[index] * (1 - (front[3] ?? 1)));
    const luminance = (color) => color
      .map((value) => value / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const layers = [];
    for (let parent = node; parent; parent = parent.parentElement) layers.unshift(rgba(getComputedStyle(parent).backgroundColor));
    const backgroundRgb = layers.reduce((back, front) => blend(front, back), [255, 255, 255]);
    const background = luminance(backgroundRgb);
    const foreground = luminance(blend(rgba(getComputedStyle(node).color), backgroundRgb));
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
}

export async function mockPublicApi(page) {
  const state = { balance: "1", failed: false, dashboardReads: 0, logReads: [], alertReads: 0, holdDashboard: null,
    warnings: [], defaultsWarnings: [], prepareWarnings: [], rows: null, logsData: null, logQueries: [], logsFailed: false,
    alertsData: null, alertsFailed: false, holdAlerts: null, deployDefaults: {}, latestScanAt: "2026-09-04T20:00:00Z" };
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.hostname === "127.0.0.1" ? route.continue() : route.abort();
  });
  await page.route("**/api/v1/tidal/**", async (route) => {
    const request = route.request();
    if (request.headers().authorization) throw new Error("Browser sent an operator credential");
    const url = new URL(request.url());
    let data;
    let warnings = state.warnings;
    if (url.pathname.endsWith("/dashboard")) {
      state.dashboardReads += 1;
      if (state.holdDashboard) await state.holdDashboard;
      if (state.failed) return route.fulfill({ status: 503, json: { detail: "fixture unavailable" } });
      data = {
        latestScanAt: state.latestScanAt,
        rows: state.rows || [{
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
      state.logQueries.push(url.search);
      if (state.logsFailed) return route.fulfill({ status: 503, json: { detail: "fixture unavailable" } });
      data = { total: 26, hasMore: offset === 0, kicks: [{ id: offset + 1, operationType: "kick", status: "CONFIRMED",
        createdAt: "2026-09-04T20:00:00Z", tokenSymbol: `REWARD${state.balance}`, wantSymbol: "USDC",
        sourceAddress: STRATEGY, sourceName: "Fixture Strategy", auctionAddress: AUCTION, usdValue: "1.25" }] };
      if (state.logsData) data = typeof state.logsData === "function" ? state.logsData(url) : state.logsData;
    } else if (url.pathname.endsWith("/alerts")) {
      state.alertReads += 1;
      if (state.holdAlerts) await state.holdAlerts;
      if (state.alertsFailed) return route.fulfill({ status: 503, json: { detail: "fixture unavailable" } });
      data = { items: [], needsActionCount: Number(state.balance), evaluatedAt: "2026-09-04T20:00:00Z" };
      if (state.alertsData) data = state.alertsData;
    } else if (url.pathname.endsWith("/deploy-defaults")) {
      warnings = state.defaultsWarnings;
      data = { strategyAddress: STRATEGY, strategyName: "Fixture Strategy", receiverAddress: STRATEGY,
        wantAddress: WANT, wantSymbol: "USDC", factoryAddress: "0x5555555555555555555555555555555555555555",
        governanceAddress: "0x6666666666666666666666666666666666666666", factoryVersion: "1.0.5",
        startingPrice: "1000000000000000000", startingPriceDisplay: "1", salt: "0x" + "00".repeat(32),
        predictedAuctionAddress: AUCTION, ...state.deployDefaults };
    } else if (url.pathname.endsWith("/deploy/browser-prepare")) {
      warnings = state.prepareWarnings;
      data = { transactions: [{ to: "0x5555555555555555555555555555555555555555", data: "0x00", value: "0x0", chainId: 1 }] };
    } else {
      return route.fulfill({ status: 404, json: { detail: "Unmocked fixture route" } });
    }
    return route.fulfill({ json: { status: "ok", warnings, data } });
  });
  return state;
}

export async function mockWallet(page) {
  await page.addInitScript(() => {
    window.walletFixture = { account: "0x7777777777777777777777777777777777777777", chainId: "0x1",
      receipt: { status: "0x1" }, receiptError: false, sends: 0, receiptReads: 0 };
    window.ethereum = {
      isRabby: true,
      async request({ method, params }) {
        const state = window.walletFixture;
        if (method === "eth_requestAccounts" || method === "eth_accounts") return [state.account];
        if (method === "eth_chainId") return state.chainId;
        if (method === "wallet_switchEthereumChain") { state.chainId = params[0].chainId; return null; }
        if (method === "eth_sendTransaction") { state.sends += 1; return "0x" + "ab".repeat(32); }
        if (method === "eth_getTransactionReceipt") {
          state.receiptReads += 1;
          if (state.receiptError) throw new Error("Receipt temporarily unavailable");
          return state.receipt;
        }
        throw new Error(`Unexpected wallet fixture method: ${method}`);
      },
    };
  });
}
