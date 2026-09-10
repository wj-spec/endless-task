// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { SkillInvocationCandidate } from "./apiTypes";
import { ChatComposer } from "./ChatComposer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const candidates: SkillInvocationCandidate[] = [
  {
    name: "review-notes",
    description: "评审笔记",
    scope: "user",
    source: "~/.agents/skills",
    pinned: true,
  },
  { name: "release-check", description: "发布检查", scope: "user" },
  { name: "other-skill", description: "别的", scope: "workspace" },
];

let container: HTMLDivElement;
let root: Root;
let draft = "";

const renderComposer = (
  onSend: () => void = () => undefined,
  skills: SkillInvocationCandidate[] = candidates,
) => {
  const element = () => (
    <ChatComposer
      conversation={null}
      draft={draft}
      health={null}
      isGenerating={false}
      loading={false}
      otherLaneRunning={false}
      pendingAction={null}
      providers={[]}
      runningLaneLabel=""
      sideMode={null}
      skillCandidates={skills}
      steerable={false}
      variant="main"
      onCancel={() => undefined}
      onDraftChange={(value) => {
        draft = value;
        act(() => root.render(element()));
      }}
      onModelChange={() => undefined}
      onRemoveFile={() => undefined}
      onSend={onSend}
      onUploadFile={() => undefined}
    />
  );
  act(() => root.render(element()));
};

const textarea = () =>
  container.querySelector<HTMLTextAreaElement>("textarea")!;

const type = (value: string) => {
  const node = textarea();
  // React 会拦截直接赋值：必须走原型 setter 才能触发 onChange。
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(node, value);
  node.setSelectionRange(value.length, value.length);
  act(() => {
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
};

const menu = () =>
  container.querySelector<HTMLUListElement>("ul.composer-slash-menu");

const options = () =>
  Array.from(container.querySelectorAll(".composer-slash-menu button")).map(
    (node) => node.querySelector(".composer-slash-name")?.textContent,
  );

const press = (key: string) =>
  act(() => {
    textarea().dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
    );
  });

beforeEach(() => {
  draft = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("ChatComposer `/技能名` 候选（S1）", () => {
  it("行首输入 / 前缀触发候选并按前缀过滤", () => {
    renderComposer();
    type("/");
    expect(menu()).not.toBeNull();
    expect(options()).toEqual(["/review-notes", "/release-check", "/other-skill"]);

    type("/re");
    expect(options()).toEqual(["/review-notes", "/release-check"]);

    type("/review");
    expect(options()).toEqual(["/review-notes"]);
  });

  it("路径与普通文本不触发", () => {
    renderComposer();
    type("看看 /Users/endless 目录");
    expect(menu()).toBeNull();
    type("3/4 的比例");
    expect(menu()).toBeNull();
  });

  it("Enter 接受候选并补空格，不发送消息", () => {
    let sent = 0;
    renderComposer(() => (sent += 1));
    type("/re");
    expect(options()).toEqual(["/review-notes", "/release-check"]);
    press("ArrowDown");
    press("Enter");
    expect(draft).toBe("/release-check ");
    expect(sent).toBe(0);
  });

  it("固定进目录的候选带已固定标记", () => {
    renderComposer();
    type("/review");
    expect(container.querySelector(".composer-slash-pin")?.textContent).toBe(
      "已固定",
    );
  });

  it("候选项展示来源标签", () => {
    renderComposer();
    type("/review");
    expect(container.querySelector(".composer-slash-source")?.textContent).toBe(
      "~/.agents/skills",
    );
  });

  it("没有候选时给出明确提示（不是没反应）", () => {
    renderComposer(() => undefined, []);
    type("/");
    const empty = container.querySelector(".composer-slash-menu.is-empty");
    expect(empty).not.toBeNull();
    expect(empty!.textContent).toContain("还没有可调用的技能");
    expect(empty!.textContent).toContain("~/.claude/skills");
    expect(menu()).toBeNull();
  });

  it("Escape 关闭候选，Enter 恢复发送", () => {
    let sent = 0;
    renderComposer(() => (sent += 1));
    type("/re");
    expect(menu()).not.toBeNull();
    press("Escape");
    expect(menu()).toBeNull();
    press("Enter");
    expect(sent).toBe(1);
  });
});
