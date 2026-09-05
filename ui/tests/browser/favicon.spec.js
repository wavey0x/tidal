import { test, expect } from "@playwright/test";
import { mockPublicApi } from "./fixtures";

for (const theme of ["light", "dark"]) {
  test(`${theme}: favicon matches the wave mark and contrasts with browser chrome`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: theme });
    await mockPublicApi(page);
    await page.goto("/");
    const icon = page.locator('link[rel="icon"]');
    await expect(icon).toHaveAttribute("href", "/tidal-favicon.svg");
    const logoPath = await page.evaluate(async () => {
      const source = await fetch(document.querySelector(".brand-logo").src).then(response => response.text());
      return new DOMParser().parseFromString(source, "image/svg+xml").querySelector("path").getAttribute("d");
    });
    await page.goto(await icon.getAttribute("href"));
    await expect(page.locator("svg > path")).toHaveAttribute("d", logoPath);
    await expect(page.locator("svg")).toHaveCSS("stroke", theme === "dark" ? "rgb(232, 236, 233)" : "rgb(34, 39, 34)");
    await page.setViewportSize({ width: 32, height: 32 });
    await page.screenshot({ path: testInfo.outputPath(`${theme}-favicon.png`), omitBackground: true });
  });
}
