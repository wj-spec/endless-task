import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  apiUrl,
  createCompletedConversation,
  openConversation,
  expectConversationHeading,
} from "../support/api";

type ProviderProfile = {
  id: string;
  name: string;
  apiKeyConfigured: boolean;
  enabled: boolean;
  defaultModel: string;
  models: Array<{ modelId: string; enabled: boolean }>;
};

type ProviderResponse = { profile: ProviderProfile };
type ProviderListResponse = { items: ProviderProfile[] };

type ConversationSnapshot = {
  conversation: {
    providerProfileId: string | null;
    modelOverride: string | null;
  };
};

const openModelServices = async (page: Page) => {
  const modelSelect = page.getByLabel("当前对话模型", { exact: true });
  await modelSelect.selectOption("__manage__");
  const panel = page.getByRole("complementary", { name: "系统能力面板" });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("tab", { name: "模型服务" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  return panel;
};

const providerByName = async (request: APIRequestContext, name: string) => {
  const response = await request.get(`${apiUrl}/providers`);
  expect(response.ok(), await response.text()).toBeTruthy();
  const payload = (await response.json()) as ProviderListResponse;
  return payload.items.find((item) => item.name === name) ?? null;
};

const createCatalogProvider = async (
  request: APIRequestContext,
  name: string,
): Promise<ProviderProfile> => {
  const response = await request.post(`${apiUrl}/providers`, {
    data: {
      name,
      baseUrl: `${apiUrl}/__e2e/openai`,
      apiKey: "e2e-model-key-one",
      defaultModel: "e2e-beta",
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const { profile } = (await response.json()) as ProviderResponse;
  const modelResponse = await request.post(
    `${apiUrl}/providers/${profile.id}/models`,
    { data: { modelId: "e2e-alpha" } },
  );
  expect(modelResponse.ok(), await modelResponse.text()).toBeTruthy();
  return profile;
};

test("模型服务设置覆盖凭据、同步、手工目录和停用生命周期", async ({
  page,
  request,
}) => {
  const title = `E2E Provider Settings ${Date.now()}`;
  const serviceName = `E2E 模型服务 ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);
  const panel = await openModelServices(page);

  await panel.getByRole("button", { name: "添加模型服务" }).click();
  await panel.getByLabel("服务名称").fill(serviceName);
  await panel.getByLabel("Base URL", { exact: true }).fill(`${apiUrl}/__e2e/openai`);
  await panel.getByLabel("API Key", { exact: true }).fill("e2e-model-key-one");

  const createResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/providers" &&
      response.request().method() === "POST",
  );
  await panel.getByRole("button", { name: "保存服务" }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok(), await createResponse.text()).toBeTruthy();
  const createPayload = (await createResponse.json()) as ProviderResponse;
  expect(await createResponse.text()).not.toContain("e2e-model-key-one");

  const card = panel.locator("article.provider-card").filter({
    hasText: serviceName,
  });
  await expect(card).toBeVisible();
  await expect(card.getByText("已保存在本地后端")).toBeVisible();

  await card.getByRole("button", { name: "测试并刷新模型" }).click();
  await expect(card.getByText("e2e-alpha", { exact: true })).toBeVisible();
  await expect(card.getByText("e2e-beta", { exact: true })).toBeVisible();
  await expect(card.getByText("连接正常", { exact: true })).toBeVisible();

  await card.getByRole("button", { name: "编辑服务" }).click();
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.getByText("已保存在本地后端")).toBeVisible();
  expect((await providerByName(request, serviceName))?.apiKeyConfigured).toBe(true);

  await card.getByRole("button", { name: "编辑服务" }).click();
  await card.getByLabel("替换 API Key").fill("e2e-model-key-two");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await card.getByRole("button", { name: "测试并刷新模型" }).click();
  await expect
    .poll(async () => {
      const response = await request.get(`${apiUrl}/__e2e/model-service`);
      return ((await response.json()) as { lastCredentialVersion: string | null })
        .lastCredentialVersion;
    })
    .toBe("key-two");

  await card.getByRole("button", { name: "手工添加" }).click();
  await card.getByLabel(`${serviceName} 模型 ID`).fill("e2e-manual");
  await card.getByRole("button", { name: "添加", exact: true }).click();
  const manualModel = card.locator("li").filter({ hasText: "e2e-manual" });
  await expect(manualModel.getByText("手工添加", { exact: true })).toBeVisible();
  await manualModel.getByRole("button", { name: "移除" }).click();
  await expect(card.getByText("e2e-manual", { exact: true })).toBeHidden();

  await card.getByRole("button", { name: "删除 API Key" }).click();
  const keyDialog = page.getByRole("dialog", { name: "删除已保存的 API Key？" });
  await expect(keyDialog).toBeVisible();
  await keyDialog.getByRole("button", { name: "删除 API Key" }).click();
  await expect(card.getByText("未设置", { exact: true })).toBeVisible();
  expect((await providerByName(request, serviceName))?.apiKeyConfigured).toBe(false);

  await card.getByRole("button", { name: "测试并刷新模型" }).click();
  await expect(panel.getByRole("alert")).toContainText("模型服务密钥无效");
  await expect(panel).not.toContainText("invalid credential");

  await card.getByRole("button", { name: "停用服务" }).click();
  await expect(card.getByText("已停用", { exact: true })).toBeVisible();
  expect((await providerByName(request, serviceName))?.enabled).toBe(false);

  await card.getByRole("button", { name: "删除服务" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "删除模型服务" });
  await deleteDialog.getByRole("button", { name: "删除服务" }).click();
  await expect(card).toBeHidden();
  expect(await providerByName(request, serviceName)).toBeNull();

  expect(createPayload.profile.id).toBeTruthy();
});

test("会话只允许从目录原子选择服务与模型，并明确显示失效选择", async ({
  page,
  request,
}) => {
  const title = `E2E Conversation Model ${Date.now()}`;
  const serviceName = `E2E Conversation Provider ${Date.now()}`;
  const profile = await createCatalogProvider(request, serviceName);
  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title);

  const composer = page.locator(".composer");
  const modelSelect = composer.getByLabel("当前对话模型", { exact: true });
  await expect(modelSelect).toBeVisible();
  await expect(composer.getByLabel("当前对话模型覆盖")).toHaveCount(0);
  await expect(composer.locator('input[placeholder*="模型"]')).toHaveCount(0);

  const selection = JSON.stringify([profile.id, "e2e-alpha"]);
  const patchRequestPromise = page.waitForRequest(
    (pending) =>
      new URL(pending.url()).pathname ===
        `/conversations/${conversation.id}` && pending.method() === "PATCH",
  );
  const patchResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/conversations/${conversation.id}` &&
      response.request().method() === "PATCH",
  );
  await modelSelect.selectOption(selection);
  const patchRequest = await patchRequestPromise;
  expect(patchRequest.postDataJSON()).toEqual({
    providerProfileId: profile.id,
    modelOverride: "e2e-alpha",
  });
  expect((await patchResponsePromise).ok()).toBe(true);

  const snapshotResponse = await request.get(
    `${apiUrl}/conversations/${conversation.id}`,
  );
  const snapshot = (await snapshotResponse.json()) as ConversationSnapshot;
  expect(snapshot.conversation.providerProfileId).toBe(profile.id);
  expect(snapshot.conversation.modelOverride).toBe("e2e-alpha");

  const disableModelResponse = await request.patch(
    `${apiUrl}/providers/${profile.id}/models/e2e-alpha`,
    { data: { enabled: false } },
  );
  expect(disableModelResponse.ok(), await disableModelResponse.text()).toBeTruthy();
  await page.reload();
  await expectConversationHeading(page, title);
  await expect(modelSelect).toHaveValue("__unavailable__");
  await expect(composer.getByText("当前模型不可用", { exact: true })).toBeVisible();
  await expect(page.getByLabel("给 Endless 发送消息")).toBeDisabled();

  await modelSelect.selectOption(JSON.stringify([profile.id, "e2e-beta"]));
  await expect(modelSelect).toHaveValue(JSON.stringify([profile.id, "e2e-beta"]));
  await expect(page.getByLabel("给 Endless 发送消息")).toBeEnabled();

  const disableProviderResponse = await request.patch(
    `${apiUrl}/providers/${profile.id}`,
    { data: { enabled: false } },
  );
  expect(
    disableProviderResponse.ok(),
    await disableProviderResponse.text(),
  ).toBeTruthy();
  await page.reload();
  await expectConversationHeading(page, title);
  await expect(composer.getByText("模型服务已停用", { exact: true })).toBeVisible();
  await expect(page.getByLabel("给 Endless 发送消息")).toBeDisabled();
});