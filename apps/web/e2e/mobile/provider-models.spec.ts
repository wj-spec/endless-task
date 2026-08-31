import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createCompletedConversation,
  openConversation,
  expectConversationHeading,
} from "../support/api";

test("移动端只提供目录模型选择并以全屏页面管理模型服务", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const title = `E2E Mobile Model ${Date.now()}`;
  const serviceName = `E2E Mobile Provider ${Date.now()}`;
  const createProviderResponse = await request.post(`${apiUrl}/providers`, {
    data: {
      name: serviceName,
      baseUrl: `${apiUrl}/__e2e/openai`,
      apiKey: "e2e-model-key-one",
      defaultModel: "e2e-mobile",
    },
  });
  expect(
    createProviderResponse.ok(),
    await createProviderResponse.text(),
  ).toBeTruthy();
  const profile = (await createProviderResponse.json()) as {
    profile: { id: string };
  };

  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title, { mobile: true });

  const composer = page.locator(".composer");
  const modelSelect = composer.getByLabel("当前对话模型", { exact: true });
  await expect(modelSelect).toBeVisible();
  await expect(composer.getByLabel("当前对话模型覆盖")).toHaveCount(0);
  await expect(composer.locator('input[placeholder*="模型"]')).toHaveCount(0);

  const selection = JSON.stringify([profile.profile.id, "e2e-mobile"]);
  const patchPromise = page.waitForRequest(
    (pending) =>
      new URL(pending.url()).pathname ===
        `/conversations/${conversation.id}` && pending.method() === "PATCH",
  );
  await modelSelect.selectOption(selection);
  expect((await patchPromise).postDataJSON()).toEqual({
    providerProfileId: profile.profile.id,
    modelOverride: "e2e-mobile",
  });

  await modelSelect.selectOption("__manage__");
  const panel = page.getByRole("dialog", { name: "系统能力面板" });
  await expect(panel).toBeVisible();
  await expect(page.locator('[role="dialog"][aria-modal="true"]')).toHaveCount(1);
  await expect(panel.getByText(serviceName, { exact: true })).toBeVisible();

  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  await expect
    .poll(async () => {
      const box = await panel.boundingBox();
      return Boolean(
        box &&
          box.x <= 1 &&
          box.y <= 1 &&
          box.width >= viewport!.width - 1 &&
          box.height >= viewport!.height - 1,
      );
    })
    .toBe(true);

  const width = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(width.scroll).toBeLessThanOrEqual(width.client);

  await panel.getByRole("button", { name: "关闭系统能力面板" }).click();
  await expect(panel).toBeHidden();
  await expectConversationHeading(page, title);

  const deleteResponse = await request.delete(
    `${apiUrl}/providers/${profile.profile.id}`,
  );
  expect(deleteResponse.ok(), await deleteResponse.text()).toBeTruthy();
});