import { test, expect } from "@playwright/test";
import { mockPublicApi } from "./fixtures";

for (const theme of ["light", "dark"]) {
  test(`${theme}: favicon uses black waves without a theme-dependent override`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: theme });
    await mockPublicApi(page);
    await page.goto("/");
    const icon = page.locator('link[rel="icon"]');
    await expect(icon).toHaveAttribute("href", "/tidal-favicon.svg?v=2");
    const logoPath = await page.evaluate(async () => {
      const source = await fetch(document.querySelector(".brand-logo").src).then(response => response.text());
      return new DOMParser().parseFromString(source, "image/svg+xml").querySelector("path").getAttribute("d");
    });
    await page.goto(await icon.getAttribute("href"));
    await expect(page.locator("svg > path")).toHaveAttribute("d", logoPath);
    await expect(page.locator("svg")).toHaveCSS("stroke", "rgb(0, 0, 0)");
    await expect(page.locator("svg style")).toHaveCount(0);
    await page.setViewportSize({ width: 32, height: 32 });
    // Preview the transparent mark on the user's pink browser chrome.
    await page.locator("svg").evaluate(node => { node.style.background = "#f3b4bc"; });
    await page.screenshot({ path: testInfo.outputPath(`${theme}-favicon.png`) });
  });
}
