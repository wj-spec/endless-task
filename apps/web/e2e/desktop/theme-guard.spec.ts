import { expect, test, type Page } from "@playwright/test";
import { createCompletedConversation, openConversation } from "../support/api";

const seedTheme = async (page: Page, preference: "light" | "dark") => {
  await page.addInitScript((theme) => {
    try {
      window.localStorage.setItem("endless-task-theme", theme);
    } catch {
      /* 忽略存储异常 */
    }
  }, preference);
};

test("存储主题偏好经首帧防护脚本生效（P3-2a）", async ({ page, request }) => {
  const title = `E2E Theme ${Date.now()}`;
  await createCompletedConversation(request, title);

  await seedTheme(page, "dark");
  await openConversation(page, title);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const darkBackground = await page.evaluate(
    () => getComputedStyle(document.body).backgroundColor,
  );
  expect(darkBackground).toBe("rgb(23, 25, 23)");

  await seedTheme(page, "light");
  await openConversation(page, title);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  const lightBackground = await page.evaluate(
    () => getComputedStyle(document.body).backgroundColor,
  );
  expect(lightBackground).toBe("rgb(251, 251, 249)");
});
