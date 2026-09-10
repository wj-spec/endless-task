// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const skill = {
  name: "review-notes",
  description: "评审笔记",
  scope: "user" as const,
  filePath: "/tmp/skills/review-notes/SKILL.md",
  disabled: false,
  disableModelInvocation: false,
  diagnostics: [],
};

const listSkills = vi.fn(async () => ({
  userSkillsDirectory: "/tmp/skills",
  items: [
    {
      ...skill,
      pinned: true,
      inCatalog: true,
      source: "~/.agents/skills",
      provenance: {
        scope: "user",
        workspaceId: "",
        name: "review-notes",
        spec: "obra/superpowers@brainstorming",
        source: "obra/superpowers",
        digest: "deadbeef0000",
        worstLevel: null,
        installedAt: "2026-09-10T10:00:00.000Z",
      },
    },
    {
      ...skill,
      name: "folded-skill",
      pinned: false,
      inCatalog: false,
    },
  ],
}));
const listSkillPackages = vi.fn(async () => ({
  enabled: true,
  mode: "packages",
  packages: [
    {
      scope: "user",
      name: "review-notes",
      version: "1.0.0",
      state: "active",
      valid: true,
      quarantined: false,
      invocable: true,
      requiredTools: [],
      requiredCapabilities: [],
    },
  ],
  conflicts: [],
}));
const skillUsage = vi.fn(async () => [
  {
    scope: "user",
    name: "review-notes",
    digest: "abcdef123456",
    counts: { invoked: 2, body_read: 2 },
    lastAt: "2026-09-09T10:00:00.000Z",
  },
]);
const searchEcosystemSkills = vi.fn(
  async (query: string): Promise<EcosystemSearchResult> => ({
    query,
    error: null,
    items: [
      {
        spec: "obra/superpowers@brainstorming",
        owner: "obra",
        repo: "superpowers",
        skill: "brainstorming",
        installs: "12.3K",
        url: "https://skills.sh/obra/superpowers/brainstorming",
      },
    ],
  }),
);
const installSkillFromEcosystem = vi.fn(async () => ({
  skill: {
    name: "brainstorming",
    version: "1.0.0",
    digest: "deadbeef0000",
    target: "/tmp/skills/brainstorming",
    upgraded: false,
    worstLevel: null,
  },
  provenance: {
    scope: "workspace",
    workspaceId: "ws_1",
    name: "brainstorming",
    spec: "obra/superpowers@brainstorming",
    source: "obra/superpowers",
    digest: "deadbeef0000",
    worstLevel: null,
    installedAt: "2026-09-10T10:00:00.000Z",
  },
}));
const runSkillCases = vi.fn(async () => ({
  diagnostics: [],
  cases: [{ name: "review", passed: true, failures: [] }],
  toolsUsed: [],
}));

vi.mock("../chat/api", () => ({
  chatApi: {
    listSkills: () => listSkills(),
    listSkillPackages: () => listSkillPackages(),
    skillUsage: () => skillUsage(),
    runSkillCases: () => runSkillCases(),
    patchSkill: vi.fn(async () => ({ disabled: true })),
    searchEcosystemSkills: (query: string) => searchEcosystemSkills(query),
    installSkillFromEcosystem: () => installSkillFromEcosystem(),
    deleteSkill: vi.fn(async () => ({
      deleted: true,
      trashedTo: "/tmp/.trash/x",
    })),
    revealInFinder: vi.fn(async () => undefined),
  },
}));

import type { EcosystemSearchResult } from "../chat/apiTypes";

const { SkillsContent } = await import("./SkillsManagement");

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

const byText = (text: string) =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
    (node) => node.textContent?.trim() === text,
  );

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = async (onInjectSkill?: (name: string) => void) => {
  await act(async () => {
    root.render(
      <SkillsContent onInjectSkill={onInjectSkill} workspaceId="ws_1" />,
    );
  });
  await flush();
};

describe("技能工作台（S2）", () => {
  it("展示来源、固定状态与未进目录说明", async () => {
    await render();
    expect(container.textContent).toContain("~/.agents/skills");
    expect(container.textContent).toContain("已固定");
    expect(container.textContent).toContain("未进目录");
    expect(byText("取消固定")).toBeTruthy();
    expect(byText("固定到目录")).toBeTruthy();
  });

  it("点「注入到对话」把技能名交给上层", async () => {
    const injected: string[] = [];
    await render((name) => injected.push(name));
    const buttons = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    ).filter((node) => node.textContent?.trim() === "注入到对话");
    expect(buttons.length).toBe(2);
    await act(async () => buttons[0].click());
    expect(injected).toEqual(["review-notes"]);
  });

  it("未提供注入回调时不渲染注入按钮", async () => {
    await render();
    const buttons = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    ).filter((node) => node.textContent?.trim() === "注入到对话");
    expect(buttons.length).toBe(0);
  });

  it("工具栏提供导入/新建，卡片显示版本与操作", async () => {
    await render();
    expect(byText("导入技能")).toBeTruthy();
    expect(byText("新建技能")).toBeTruthy();
    expect(container.textContent).toContain("review-notes");
    expect(container.textContent).toContain("v1.0.0");
    expect(byText("详情 / 用例")).toBeTruthy();
    expect(byText("删除")).toBeTruthy();
  });

  it("点开详情可读使用记录并试跑用例", async () => {
    await render();
    await act(async () => byText("详情 / 用例")!.click());
    await flush();

    await act(async () => byText("使用记录")!.click());
    await flush();
    expect(container.textContent).toContain("被调用 2");
    expect(container.textContent).toContain("abcdef12");

    await act(async () => byText("试跑用例")!.click());
    await flush();
    expect(container.textContent).toContain("✓ review");
  });

  it("导入表单可切换并展示校验入口", async () => {
    await render();
    await act(async () => byText("导入技能")!.click());
    const pathInput = container.querySelector<HTMLInputElement>(
      'input[aria-label="技能目录路径"]',
    );
    expect(pathInput?.placeholder).toContain("本地技能目录");
    expect(byText("校验")).toBeTruthy();
    expect(byText("导入")).toBeTruthy();
  });

  it("新建表单要求必填项", async () => {
    await render();
    await act(async () => byText("新建技能")!.click());
    const create = byText("创建")!;
    expect(create.disabled).toBe(true);
  });

  it("已从生态安装的技能带「来自生态」标记", async () => {
    await render();
    expect(container.textContent).toContain("来自生态");
  });

  it("从生态检索 → 确认 → 安装并刷新", async () => {
    await render();
    await act(async () => byText("从生态安装")!.click());
    const keyword = container.querySelector<HTMLInputElement>(
      'input[aria-label="生态检索关键词"]',
    );
    expect(keyword).toBeTruthy();
    expect(container.textContent).toContain("静态扫描门禁");

    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(keyword, "brainstorming");
      keyword!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => byText("检索生态")!.click());
    await flush();
    expect(searchEcosystemSkills).toHaveBeenCalledWith("brainstorming");
    expect(container.textContent).toContain("obra/superpowers@brainstorming");
    expect(container.textContent).toContain("12.3K");

    // 安装前必须再确认一次
    await act(async () => byText("安装")!.click());
    expect(container.textContent).toContain("从生态安装这个技能？");
    await act(async () => byText("确认安装")!.click());
    await flush();
    expect(installSkillFromEcosystem).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("已安装 brainstorming@1.0.0");
    expect(container.textContent).toContain("扫描通过");
  });

  it("检索失败时给出错误提示", async () => {
    searchEcosystemSkills.mockResolvedValueOnce({
      query: "x",
      error: "未找到 npx，请先安装 Node.js。",
      items: [],
    });
    await render();
    await act(async () => byText("从生态安装")!.click());
    const keyword = container.querySelector<HTMLInputElement>(
      'input[aria-label="生态检索关键词"]',
    )!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(keyword, "x");
      keyword.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => byText("检索生态")!.click());
    await flush();
    expect(container.textContent).toContain("未找到 npx");
  });
});
