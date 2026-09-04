import { test, expect } from "@playwright/test";
import { mockPublicApi } from "./fixtures";

for (const theme of ["light", "dark"]) {
  test(`${theme} theme keeps readable refresh feedback and animated address/token copying`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.emulateMedia({ colorScheme: theme });
    await mockPublicApi(page);
    await page.goto("/");
    await expect(page.getByText("$1.25", { exact: true })).toBeVisible();
    expect(await page.locator("html").getAttribute("data-theme")).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem("tidal_theme_preference"))).toBeNull();
    for (const button of [page.locator(".copy-trigger").first(), page.locator(".copy-trigger").last()]) {
      await button.click();
      await expect(button).toHaveClass(/is-copied/);
      await expect.poll(() => button.locator(".check-glyph").evaluate((node) => getComputedStyle(node).opacity)).toBe("1");
      const contrast = await button.evaluate((node) => {
        const luminance = (color) => color.match(/[\d.]+/g).slice(0, 3).map(Number)
          .map((value) => value / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
          .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
        const bg = luminance(getComputedStyle(document.body).backgroundColor);
        const color = luminance(getComputedStyle(node.querySelector(".copy-icon")).color);
        const text = luminance(getComputedStyle(document.querySelector(".refresh-status")).color);
        return [color, text].map((value) => (Math.max(value, bg) + 0.05) / (Math.min(value, bg) + 0.05));
      });
      expect(contrast[0]).toBeGreaterThanOrEqual(4.5);
      expect(contrast[1]).toBeGreaterThanOrEqual(4.5);
    }
    await page.screenshot({ path: testInfo.outputPath(`${theme}-refresh-copy.png`) });
    const chosen = theme === "light" ? "dark" : "light";
    await page.locator(".theme-switch").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", chosen);
    expect(await page.evaluate(() => localStorage.getItem("tidal_theme_preference"))).toBe(chosen);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", chosen);
  });
}
