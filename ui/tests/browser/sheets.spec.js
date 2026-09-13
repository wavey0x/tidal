import { test, expect, webkit } from "@playwright/test";
import { mockPublicApi, mockWallet, contrastRatio, STRATEGY, TOKEN, WANT, AUCTION } from "./fixtures";

async function sheetFixture(page) {
  const state = await mockPublicApi(page);
  state.rows = [{ sourceType: "strategy", sourceAddress: STRATEGY, sourceName: "Curve-crvDOLA",
    contextAddress: TOKEN, contextName: "Curve DOLA-FRAXPYUSD Factory yVault", contextSymbol: "yvCurve-DOLA-FRAXPYUSD-f",
    active: true, depositLimit: "1", scannedAt: new Date().toISOString(), wantAddress: WANT, wantSymbol: "crvDOLA",
    auctionAddress: AUCTION, auctionVersion: "1.0.4",
    kicks: Array.from({ length: 5 }, (_, i) => ({ txHash: `0x${String(i + 1).repeat(64)}`,
      operationType: "kick", status: "CONFIRMED", createdAt: new Date(Date.now() - (i + 1) * 86400000).toISOString(), auctionAddress: AUCTION })),
    balances: Array.from({ length: 12 }, (_, i) => ({ tokenAddress: `0x${(i + 100).toString(16).padStart(40, "0")}`,
      tokenSymbol: `TOKEN${i}`, normalizedBalance: String(100 - i), tokenPriceUsd: "1", tokenDecimals: 18 })),
  }];
  state.logsData = { total: 1, hasMore: false, kicks: [{ id: 1, operationType: "kick", status: "CONFIRMED",
    createdAt: new Date().toISOString(), sourceAddress: STRATEGY, sourceName: "Curve-crvDOLA", tokenAddress: TOKEN,
    tokenSymbol: "CRV", wantAddress: WANT, wantSymbol: "crvDOLA", auctionAddress: AUCTION, usdValue: "771.87",
    txHash: `0x${"ab".repeat(32)}`, normalizedBalance: "1933.60", blockNumber: 25910991, gasUsed: "160324",
    quoteAmount: "1232.904850164963824509", minPrice: "321966315368532138", startPrice: "1357", minQuote: "1171" }] };
  return state;
}

async function inVisibleViewport(page, locator, inset = 0) {
  const viewport = await page.evaluate(() => ({ x: visualViewport.offsetLeft, y: visualViewport.offsetTop,
    width: visualViewport.width, height: visualViewport.height }));
  const rect = await locator.boundingBox();
  expect(rect.x).toBeGreaterThanOrEqual(viewport.x + inset - 1);
  expect(rect.y).toBeGreaterThanOrEqual(viewport.y + inset - 1);
  expect(rect.x + rect.width).toBeLessThanOrEqual(viewport.x + viewport.width - inset + 1);
  expect(rect.y + rect.height).toBeLessThanOrEqual(viewport.y + viewport.height - inset + 1);
}

async function swipe(page, session, x, y, dy) {
  await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
  for (let i = 1; i <= 10; i++) {
    await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + dy * i / 10 }] });
    await page.waitForTimeout(16);
  }
  await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

for (const theme of ["light", "dark"]) {
  for (const path of ["/", "/logs"]) {
    test(`${theme}: ${path} sheet fits zoomed and resized visible viewports with a reachable end`, async ({ browser }, testInfo) => {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, colorScheme: theme });
      try {
        const page = await context.newPage();
        await sheetFixture(page);
        await page.goto(path);
        await page.getByRole("button", { name: /^Show details for / }).first().tap();
        const dialog = page.getByRole("dialog");
        await expect(dialog).toHaveCSS("transform", "none");
        const close = dialog.getByRole("button", { name: "Close details" });
        const body = dialog.locator(".kick-modal-body");
        const session = await context.newCDPSession(page);
        if (path === "/") {
          await dialog.getByRole("button", { name: "Show 4 earlier transactions" }).tap();
          await dialog.locator(".strategy-sheet-reference > summary").tap();
        }
        for (const [width, height, scale] of [[390, 844, 1], [390, 844, 1.15], [390, 844, 2], [320, 640, 1], [780, 390, 1], [390, 550, 1]]) {
          await page.setViewportSize({ width, height });
          await session.send("Emulation.setPageScaleFactor", { pageScaleFactor: scale });
          await expect.poll(() => dialog.evaluate(node => Math.abs(node.getBoundingClientRect().width - Math.min(640, visualViewport.width)) < 1)).toBe(true);
          await inVisibleViewport(page, dialog);
          await inVisibleViewport(page, close, 8);
          expect((await close.boundingBox()).width).toBe(44);
          expect(await contrastRatio(close)).toBeGreaterThanOrEqual(4.5);
          if (path === "/") {
            const lastBalance = body.locator(".token-item").last();
            await lastBalance.scrollIntoViewIfNeeded();
            await inVisibleViewport(page, lastBalance, 16);
          }
          await body.evaluate(node => { node.scrollTop = node.scrollHeight; });
          const end = body.locator(path === "/" ? ".strategy-sheet-reference-content > .kick-detail-item" : ".log-detail-links").last();
          await inVisibleViewport(page, end, 16);
          expect(await body.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
          await page.screenshot({ path: testInfo.outputPath(`${theme}-${path === "/" ? "strategy" : "logs"}-${width}-${height}-${scale}.png`), animations: "disabled" });
        }
        await close.tap();
        await expect(dialog).toHaveCount(0);
        await expect(page.getByRole("button", { name: /^Show details for / }).first()).toBeFocused();
      } finally { await context.close(); }
    });
  }
}

test("content scrolls normally; a downward pull at the top follows the finger and dismisses", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    await sheetFixture(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Show details for Curve-crvDOLA" }).tap();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveCSS("transform", "none");
    const body = dialog.locator(".kick-modal-body");
    const session = await context.newCDPSession(page);
    await swipe(page, session, 230, 500, -260);
    await expect.poll(() => body.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    await expect(dialog).toHaveCSS("transform", "none");
    // A scroll that started below the top never turns into a dismissal.
    await swipe(page, session, 230, 250, 180);
    await expect(dialog).toHaveCSS("transform", "none");
    await body.evaluate(node => { node.scrollTop = 0; });
    const start = await dialog.boundingBox();
    const content = await body.boundingBox();
    const x = 230, y = content.y + 20;
    await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + 140 }] });
    await expect.poll(async () => (await dialog.boundingBox()).y - start.y).toBeGreaterThan(120);
    expect(await body.evaluate(node => node.scrollTop)).toBe(0);
    await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect(dialog).toHaveCount(0);
  } finally { await context.close(); }
});

for (const path of ["/", "/logs"]) {
  test(`${path}: mouse drags settle, reverse, dismiss, and reopen without stale state`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await sheetFixture(page);
    await page.goto(path);
    const opener = page.getByRole("button", { name: /^Show details for / }).first();
    const dialog = page.getByRole("dialog");
    const startDrag = async () => {
      await expect(dialog).toHaveCSS("transform", "none");
      const box = await dialog.boundingBox();
      await page.mouse.move(195, box.y + 9);
      await page.mouse.down();
      return box.y + 9;
    };
    await opener.click();
    let y = await startDrag();
    await page.mouse.move(195, y + 30, { steps: 5 });
    await page.waitForTimeout(140); // Holding a short pull is not a flick.
    await page.mouse.up();
    await expect(dialog).toHaveCSS("transform", "none");
    y = await startDrag();
    await page.mouse.move(195, y + 180, { steps: 5 });
    await page.mouse.move(195, y + 140, { steps: 4 });
    await page.mouse.up();
    await expect(dialog).toHaveCSS("transform", "none");
    y = await startDrag();
    await page.mouse.move(195, y + 180, { steps: 8 });
    await page.mouse.up();
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
    await opener.click();
    await expect(dialog).toHaveCSS("transform", "none");
    await dialog.getByRole("button", { name: "Close details" }).click();
    await expect(dialog).toHaveCount(0);
  });

  test(`${path}: cancelled touch pulls recover and header swipes dismiss`, async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    try {
      const page = await context.newPage();
      await sheetFixture(page);
      await page.goto(path);
      const opener = page.getByRole("button", { name: /^Show details for / }).first();
      await opener.tap();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toHaveCSS("transform", "none");
      const session = await context.newCDPSession(page);
      const header = await dialog.locator(".kick-modal-header").boundingBox();
      const x = 195, y = header.y + 9;
      await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
      await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + 150 }] });
      await expect(dialog).toHaveAttribute("data-dragging", "true");
      await session.send("Input.dispatchTouchEvent", { type: "touchCancel", touchPoints: [] });
      await expect(dialog).toHaveCSS("transform", "none");
      await dialog.getByRole("button", { name: "Close details" }).tap();
      await expect(dialog).toHaveCount(0);
      await opener.tap();
      await expect(dialog).toHaveCSS("transform", "none");
      await swipe(page, session, x, y, 150);
      await expect(dialog).toHaveCount(0);
    } finally { await context.close(); }
  });
}

test("sheets preserve page position and lock the background until the last nested dialog closes", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    const state = await mockPublicApi(page);
    state.rows = Array.from({ length: 20 }, (_, i) => ({ sourceType: "strategy", sourceAddress: `0x${String(i + 100).padStart(40, "0")}`,
      sourceName: `Fixture ${i}`, active: true, depositLimit: "1", wantAddress: WANT, wantSymbol: "USDC",
      balances: [{ tokenAddress: TOKEN, tokenSymbol: "REWARD", normalizedBalance: "1", tokenPriceUsd: "1", tokenDecimals: 18 }],
      scannedAt: new Date().toISOString() }));
    await mockWallet(page);
    await page.goto("/");
    const opener = page.getByRole("button", { name: "Show details for Fixture 15", exact: true });
    await opener.scrollIntoViewIfNeeded();
    const scrollY = await page.evaluate(() => window.scrollY);
    expect(scrollY).toBeGreaterThan(0);
    await opener.tap();
    await expect(page.locator("#root")).toHaveAttribute("inert", "");
    await expect(page.locator("body")).toHaveCSS("position", "fixed");
    await page.getByRole("button", { name: /Deploy auction/i }).tap();
    await expect(page.locator(".kick-modal-backdrop")).toHaveAttribute("inert", "");
    await page.keyboard.press("Escape");
    await expect(page.locator(".deploy-modal")).toHaveCount(0);
    await expect(page.locator(".kick-modal-backdrop")).not.toHaveAttribute("inert");
    await expect(page.locator("body")).toHaveCSS("position", "fixed");
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator("#root")).not.toHaveAttribute("inert");
    await expect(page.locator("body")).toHaveCSS("position", "static");
    expect(await page.evaluate(() => window.scrollY)).toBe(scrollY);
    await expect(opener).toBeFocused();
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
  } finally { await context.close(); }
});

test("short sheets fit content and backdrop/close/Escape respect reduced motion", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockPublicApi(page);
  await page.goto("/");
  const opener = page.getByRole("button", { name: /^Show details for / }).first();
  for (const method of ["backdrop", "close", "escape"]) {
    await opener.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveCSS("transform", "none");
    expect((await dialog.boundingBox()).height).toBeLessThan(600);
    expect(await dialog.evaluate(node => node.getAnimations().length)).toBe(0);
    if (method === "backdrop") await page.mouse.click(10, 10);
    else if (method === "close") await dialog.getByRole("button", { name: "Close details" }).click();
    else await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
  }
});

test("resizing away from a mobile sheet under confirmation restores the page after cancellation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await mockWallet(page);
  await page.goto("/");
  await page.getByRole("button", { name: /^Show details for / }).first().click();
  await page.getByRole("button", { name: /Deploy auction/i }).click();
  await expect(page.locator(".deploy-modal")).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.locator(".kick-modal")).toHaveCount(0);
  await expect(page.locator("body")).toHaveCSS("position", "fixed");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.locator("body")).toHaveCSS("position", "static");
  await expect(page.locator("html")).toHaveCSS("overflow-y", "scroll");
  await expect(page.locator("#root")).not.toHaveAttribute("inert");
  await page.getByRole("tab", { name: "Logs", exact: true }).click();
  await expect(page).toHaveURL(/\/logs/);
  expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
});

test("horizontal swipes, interrupted drags and copy controls leave the sheet usable", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
    permissions: ["clipboard-read", "clipboard-write"] });
  try {
    const page = await context.newPage();
    await sheetFixture(page);
    await page.goto("/");
    await page.getByRole("button", { name: /^Show details for / }).first().tap();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveCSS("transform", "none");
    const session = await context.newCDPSession(page);
    const header = await dialog.locator(".kick-modal-header").boundingBox();
    const x = 195, y = header.y + 9;
    await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x + 70, y: y + 10 }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x + 70, y: y + 150 }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect(dialog).toHaveCSS("transform", "none");
    await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y + 150 }] });
    await expect(dialog).toHaveAttribute("data-dragging", "true");
    // A second finger must cancel a pull, never dismiss the sheet.
    await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ id: 0, x, y: y + 150 }, { id: 1, x: x + 80, y: y + 150 }] });
    await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect(dialog).toHaveCSS("transform", "none");
    const copy = dialog.locator(".strategy-sheet-heading .copy-trigger");
    await copy.tap();
    await expect(copy).toHaveClass(/is-copied/);
    await dialog.getByRole("button", { name: "Close details" }).tap();
    await expect(dialog).toHaveCount(0);
  } finally { await context.close(); }
});

for (const type of ["sheet", "confirmation"]) {
  test(`${type}: crossing the backdrop boundary does not close; clicking outside does`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockPublicApi(page);
    await mockWallet(page);
    await page.goto("/");
    await page.getByRole("button", { name: /^Show details for / }).first().click();
    if (type === "confirmation") await page.getByRole("button", { name: /Deploy auction/i }).click();
    const dialog = page.getByRole("dialog", { name: type === "sheet" ? "Strategy details" : "Deploy auction" });
    await expect(dialog).toHaveCSS("transform", "none");
    const box = await dialog.boundingBox();
    const inside = { x: box.x + 20, y: box.y + 25 };
    for (const [start, end] of [[inside, { x: 10, y: 10 }], [{ x: 10, y: 10 }, inside]]) {
      await page.mouse.move(start.x, start.y);
      await page.mouse.down();
      await page.mouse.move(end.x, end.y, { steps: 6 });
      await page.mouse.up();
      await expect(dialog).toBeVisible();
    }
    await page.mouse.move(10, 10);
    await page.mouse.down();
    await expect(dialog).toBeVisible();
    await page.mouse.up();
    await expect(page.locator(type === "sheet" ? ".kick-modal" : ".deploy-modal")).toHaveCount(0);
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
  });
}

for (const theme of ["light", "dark"]) {
  test(`${theme}: strategy sheets prioritize balances consistently and keep context accessible`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
      colorScheme: theme, permissions: ["clipboard-read", "clipboard-write"] });
    try {
      const page = await context.newPage();
      const state = await sheetFixture(page);
      state.rows[0].balances = state.rows[0].balances.slice(0, 1);
      await page.goto("/");
      const dialog = page.getByRole("dialog", { name: "Strategy details" });
      const body = dialog.locator(".kick-modal-body");
      for (const entry of [/^Show details for /, /^Expand rewards for /]) {
        await page.getByRole("button", { name: entry }).first().tap();
        await expect(dialog).toHaveCSS("transform", "none");
        await expect(dialog.locator(".kick-modal-header .row-primary")).toHaveText("Curve-crvDOLA");
        await expect(dialog.locator(".strategy-sheet-heading .address-copy a")).toHaveAttribute("href", `https://etherscan.io/address/${STRATEGY}`);
        await expect(dialog.locator(".strategy-sheet-scan")).toContainText("Scanned");
        expect(await body.locator(":scope > .strategy-sheet-content > *").evaluateAll(nodes => nodes.map(node => node.className)))
          .toEqual(["kick-detail-item strategy-detail-balances", "kick-detail-item strategy-detail-auction", "kick-detail-item strategy-detail-history", "strategy-sheet-reference"]);
        await expect(body.getByText("Want Token", { exact: true })).toHaveCount(0);
        await expect(body.getByText("Strategy", { exact: true })).toHaveCount(0);
        const reference = dialog.locator(".strategy-sheet-reference");
        await expect(reference).not.toHaveAttribute("open");
        await expect(reference.locator(".strategy-sheet-reference-content")).toBeHidden();
        await inVisibleViewport(page, dialog.locator(".strategy-detail-history"), 16);
        const copy = dialog.locator(".strategy-sheet-heading .copy-trigger");
        await copy.tap();
        await expect(copy).toHaveClass(/is-copied/);
        await expect.poll(() => copy.locator(".check-glyph").evaluate(node => getComputedStyle(node).opacity)).toBe("1");
        expect(await contrastRatio(copy.locator(".copy-icon"))).toBeGreaterThanOrEqual(4.5);
        await page.screenshot({ path: testInfo.outputPath(`${theme}-strategy-overview-${entry.source.includes("rewards") ? "rewards" : "identity"}.png`), animations: "disabled" });
        // The native disclosure must participate in the dialog's keyboard loop.
        await reference.locator("summary").focus();
        await page.keyboard.press("Enter");
        await expect(reference).toHaveAttribute("open");
        await expect(reference).toContainText("Curve DOLA-FRAXPYUSD Factory yVault");
        await expect(reference.locator("time")).toHaveAttribute("dateTime", state.rows[0].scannedAt);
        await reference.locator("summary").press("Enter");
        await page.keyboard.press("Tab");
        await expect(dialog.locator(".strategy-sheet-heading a")).toBeFocused();
        await dialog.getByRole("button", { name: "Close details" }).tap();
        await expect(page.getByRole("button", { name: entry }).first()).toBeFocused();
      }
    } finally { await context.close(); }
  });
}

test("zoomed nested deployment keeps cancellation reachable without sending a transaction", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    await mockPublicApi(page);
    await mockWallet(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Show details for Fixture Strategy" }).tap();
    await page.getByRole("button", { name: /Deploy auction/i }).tap();
    const modal = page.locator(".deploy-modal");
    await expect(modal).toBeVisible();
    await expect(modal).toHaveCSS("transform", "none");
    const session = await context.newCDPSession(page);
    await session.send("Emulation.setPageScaleFactor", { pageScaleFactor: 1.5 });
    await expect.poll(() => modal.evaluate(node => node.getBoundingClientRect().width <= visualViewport.width - 23)).toBe(true);
    await modal.evaluate(node => { node.scrollTop = node.scrollHeight; });
    await inVisibleViewport(page, modal.getByRole("button", { name: "Cancel", exact: true }), 12);
    await modal.getByRole("button", { name: "Cancel", exact: true }).tap();
    await expect(modal).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: "Strategy details" })).toBeVisible();
    expect(await page.evaluate(() => window.walletFixture.sends)).toBe(0);
  } finally { await context.close(); }
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: WebKit sheets retain close access, bottom content and disclosure focus`, async ({}, testInfo) => {
    const browser = await webkit.launch({ channel: undefined });
    try {
      const context = await browser.newContext({ baseURL: "http://127.0.0.1:5182", viewport: { width: 390, height: 844 },
        isMobile: true, hasTouch: true, colorScheme: theme });
      const page = await context.newPage();
      await sheetFixture(page);
      for (const path of ["/", "/logs"]) {
        await page.goto(path);
        await page.getByRole("button", { name: /^Show details for / }).first().tap();
        const dialog = page.getByRole("dialog");
        await expect(dialog).toHaveCSS("transform", "none");
        if (path === "/") {
          await dialog.getByRole("button", { name: "Show 4 earlier transactions" }).tap();
          await dialog.locator(".strategy-sheet-reference > summary").tap();
        }
        for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 550 }, { width: 780, height: 390 }]) {
          await page.setViewportSize(viewport);
          await expect.poll(() => dialog.evaluate(node => node.getBoundingClientRect().width)).toBe(Math.min(viewport.width, 640));
          await inVisibleViewport(page, dialog);
          await inVisibleViewport(page, dialog.getByRole("button", { name: "Close details" }), 8);
          const body = dialog.locator(".kick-modal-body");
          await body.evaluate(node => { node.scrollTop = node.scrollHeight; });
          await inVisibleViewport(page, body.locator(path === "/" ? ".strategy-sheet-reference-content > .kick-detail-item" : ".log-detail-links").last(), 16);
          expect(await body.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
          await page.screenshot({ path: testInfo.outputPath(`${theme}-webkit-${path === "/" ? "strategy" : "logs"}-${viewport.width}.png`), animations: "disabled" });
        }
        if (path === "/") {
          const summary = dialog.locator(".strategy-sheet-reference > summary");
          await summary.press("Enter");
          await page.keyboard.press("Tab");
          await expect(dialog.locator(".strategy-sheet-heading a")).toBeFocused();
        }
        // WebKit also supports dragging the same handle with a mouse/trackpad.
        const header = await dialog.locator(".kick-modal-header").boundingBox();
        await page.mouse.move(195, header.y + 9);
        await page.mouse.down();
        await page.mouse.move(195, header.y + 159, { steps: 8 });
        await page.mouse.up();
        await expect(dialog).toHaveCount(0);
        await page.setViewportSize({ width: 390, height: 844 });
      }
    } finally { await browser.close(); }
  });
}
