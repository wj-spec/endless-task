import { expect, test } from "@playwright/test";
import {
  createCompletedConversation,
  openConversation,
} from "../support/api";

const MARKDOWN_MARKER = "[e2e:markdown]";

test("消息内容渲染：链接/标题/表格/代码块/引用角标（P0-1）", async ({
  page,
  request,
}) => {
  const title = `E2E Content Rendering ${Date.now()}`;
  await createCompletedConversation(request, title, MARKDOWN_MARKER);
  await openConversation(page, title);

  const copy = page.locator(".assistant-content .message-copy");

  // 标题与段落
  await expect(copy.locator("h1")).toHaveText("E2E 渲染标题");

  // 外链：新窗口 + noopener
  const link = copy.getByRole("link", { name: "OpenAI" });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", "https://openai.com");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", /noopener/);

  // GFM 表格
  const table = copy.locator("table");
  await expect(table).toBeVisible();
  await expect(table).toContainText("甲");
  await expect(table).toContainText("乙");

  // 引用角标（[K1] -> 可点按钮）
  const citation = copy.getByRole("button", { name: "查看引用 K1 的来源" });
  await expect(citation).toBeVisible();

  // 代码块：语言标签 + 复制按钮 + 高亮
  await expect(copy.locator(".code-block-lang")).toHaveText("python");
  const copyButton = copy.locator(".code-block-copy");
  await expect(copyButton).toHaveAttribute("aria-label", "复制代码（python）");
  await expect(copy.locator(".code-block pre code.hljs")).toBeVisible();

  // 用户输入按纯文本显示（标记不变成 markdown）
  await expect(page.locator(".user-copy")).toContainText(MARKDOWN_MARKER);
});
