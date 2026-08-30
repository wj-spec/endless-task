import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createConversation,
  openConversation,
  waitForRunStatus,
} from "../support/api";

type ProviderState = {
  approvalExecutionCount: number;
};

const providerState = async (
  request: Parameters<typeof createConversation>[0],
): Promise<ProviderState> => {
  const response = await request.get(`${apiUrl}/__e2e/provider`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as ProviderState;
};

const requestApproval = async (
  page: Parameters<typeof openConversation>[0],
  title: string,
) => {
  await openConversation(page, title);
  const composer = page.getByRole("textbox", { name: "给 Endless 发送消息" });
  await composer.fill("[e2e:approval] 执行受控操作");
  await page.getByRole("button", { name: "发送消息" }).click();
  const approval = page.getByRole("group", { name: "操作确认" });
  await expect(approval).toBeVisible();
  await expect(
    approval.getByText("允许 Endless 执行“e2e_approval”吗？"),
  ).toBeVisible();
  return approval;
};

test("Runtime v2 工具审批允许后只执行一次并完成原运行", async ({
  page,
  request,
}) => {
  const title = `E2E Approval Approve ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const before = await providerState(request);
  const approval = await requestApproval(page, title);

  await approval.getByRole("button", { name: "允许一次" }).click();

  await expect(page.getByText("E2E 审批通过，受控操作已完成。")).toBeVisible();
  await waitForRunStatus(request, conversation.id, "completed");
  await expect
    .poll(async () => (await providerState(request)).approvalExecutionCount)
    .toBe(before.approvalExecutionCount + 1);
  await expect(approval).toBeHidden();
});

test("Runtime v2 工具审批拒绝后不执行工具并完成原运行", async ({
  page,
  request,
}) => {
  const title = `E2E Approval Deny ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const before = await providerState(request);
  const approval = await requestApproval(page, title);

  await approval.getByRole("button", { name: "不允许" }).click();

  await expect(page.getByText("E2E 操作已拒绝，未执行受控操作。")).toBeVisible();
  await waitForRunStatus(request, conversation.id, "completed");
  expect((await providerState(request)).approvalExecutionCount).toBe(
    before.approvalExecutionCount,
  );
  await expect(approval).toBeHidden();
});