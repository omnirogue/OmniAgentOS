import { test, expect } from "@playwright/test";
import { E2E_BASE_URL } from "./rail.authority";

test.describe("@visual scaffold against existing product UI", () => {
  test("home page (status) renders", async ({ page }) => {
    // /chats was deleted in the new 10-page IA (P0 shell rebuild,
    // 2026-08-14) — / is Status, the new home page.
    await page.goto(`${E2E_BASE_URL}/`);
    await expect(page.locator("h1:has-text('Status')")).toBeVisible({ timeout: 15000 });
    // Scaffold for visual regression
    expect(await page.screenshot()).toBeDefined();
  });
});
