// @vitest-environment jsdom
/**
 * C4：「会话」视图本体（真组件，jsdom）。
 *
 * 这一层只钉视图自己的职责：**怎么渲染传进来的数据**、**点击时交出什么实参**。
 * "数据从哪来 / 有没有额外请求 / 新建走哪条路"由 App 级接线用例
 * （`conversationsAppWiring.test.tsx`）负责——两层分别能红，缺一方都会留下
 * "改坏了也不红"的盲区。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, RuntimeV2Lane } from "../../chat/apiTypes";
import { ConversationsView, type ConversationsViewProps } from "./ConversationsView";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const lane = (over: Partial<RuntimeV2Lane>): RuntimeV2Lane =>
  ({
    id: "lane",
    conversationId: "conv-1",
    kind: "persistent_branch",
    status: "active",
    archived: false,
    archivedAt: null,
    displayName: null,
    summary: null,
    title: null,
    baseEntryExcerpt: null,
    baseEntryId: null,
    leafEntryId: null,
    createdFromEntryId: null,
    createdAt: "2026-09-01T00:00:00Z",
    sourceLaneId: null,
    isMain: false,
    ...over,
  }) as RuntimeV2Lane;

const MAIN = lane({
  id: "lane-main",
  kind: "main",
  isMain: true,
  displayName: "主线",
  createdAt: "2026-09-01T00:00:00Z",
});
const BRANCH = lane({
  id: "lane-branch",
  title: "对照分支",
  sourceLaneId: MAIN.id,
  createdAt: "2026-09-02T00:00:00Z",
});
const ARCHIVED = lane({
  id: "lane-archived",
  displayName: "已归档的分支",
  sourceLaneId: MAIN.id,
  status: "archived",
  archived: true,
  createdAt: "2026-09-03T00:00:00Z",
});

const conversation = (over: Partial<Conversation>): Conversation =>
  ({
    id: "conv",
    title: "某个会话",
    status: "active",
    nextTurnOrdinal: 1,
    titleIsManual: false,
    createdAt: "2026-09-01T00:00:00Z",
    updatedAt: "2026-09-02T00:00:00Z",
    archivedAt: null,
    parentConversationId: null,
    forkTurnId: null,
    kind: "normal",
    providerProfileId: null,
    modelOverride: null,
    promotedAt: null,
    workspaceId: "ws-1",
    ...over,
  }) as Conversation;

const defaults: ConversationsViewProps = {
  lanes: [],
  currentLaneId: null,
  conversations: [],
  onCreateTemporaryConversation: () => undefined,
  onOpenConversation: () => undefined,
  onSelectLane: () => undefined,
};

const render = (over: Partial<ConversationsViewProps> = {}) => {
  act(() => root.render(<ConversationsView {...defaults} {...over} />));
};

const laneButtonNames = () =>
  Array.from(container.querySelectorAll(".workspace-lane-row .workspace-item-title")).map(
    (node) => node.textContent,
  );

const temporaryButtonNames = () =>
  Array.from(
    container.querySelectorAll(".workspace-temporary-list .workspace-item-title"),
  ).map((node) => node.textContent);

describe("C4 会话视图：分支树", () => {
  it("父在前、子缩进、当前 lane 高亮、归档有标注（顺序复用 orderLanes）", () => {
    render({
      lanes: [ARCHIVED, BRANCH, MAIN], // 故意乱序传入，断言的是排序结果
      currentLaneId: BRANCH.id,
    });

    expect(laneButtonNames()).toEqual(["主线", "对照分支", "已归档的分支"]);
    const items = Array.from(
      container.querySelectorAll<HTMLElement>(".workspace-lane-tree li"),
    );
    // 缩进 = 深度（父 0、子 +1）。
    expect(items.map((node) => node.style.paddingInlineStart)).toEqual([
      "0px",
      "14px",
      "14px",
    ]);
    // 当前 lane 只有一个高亮，且是真正在看的那条。
    const currents = container.querySelectorAll('[aria-current="true"]');
    expect(currents).toHaveLength(1);
    expect(currents[0].textContent).toContain("对照分支");
    expect(container.querySelector(".workspace-lane-row.is-current")?.textContent).toContain(
      "对照分支",
    );
    // 归档标注（数据来自 lane.status/archived，不新增字段）。
    const archivedRow = Array.from(
      container.querySelectorAll(".workspace-lane-row"),
    ).find((node) => node.textContent?.includes("已归档的分支"));
    expect(archivedRow?.querySelector(".workspace-item-meta")?.textContent).toBe(
      "分支 · 已归档",
    );
  });

  it("没有分支时给出空态（而不是空白）", () => {
    render({ lanes: [] });
    expect(laneButtonNames()).toEqual([]);
    expect(container.textContent).toContain("当前会话还没有分支");
  });

  it("点击分支行交出该 lane 的 id（实参断言）", () => {
    const onSelectLane = vi.fn();
    render({ lanes: [MAIN, BRANCH], currentLaneId: MAIN.id, onSelectLane });

    const rows = container.querySelectorAll<HTMLButtonElement>(".workspace-lane-row");
    act(() => rows[1].click());

    expect(onSelectLane.mock.calls).toEqual([[BRANCH.id]]);
  });
});

describe("C4 会话视图：临时对话", () => {
  it("只列 kind === \"ephemeral\"，普通会话不进列表", () => {
    render({
      conversations: [
        conversation({ id: "c-normal", title: "普通会话", kind: "normal" }),
        conversation({ id: "c-temp", title: "临时探索 A", kind: "ephemeral" }),
        conversation({
          id: "c-temp-2",
          title: "临时探索 B",
          kind: "ephemeral",
          status: "archived",
        }),
      ],
    });

    expect(temporaryButtonNames()).toEqual(["临时探索 A", "临时探索 B"]);
    expect(container.querySelector(".workspace-temporary-list")?.textContent).not.toContain(
      "普通会话",
    );
    // "带状态"：活着的与已归档的分得开。
    expect(container.textContent).toContain("进行中");
    expect(container.textContent).toContain("已归档");
  });

  // C4 复审判 I-3：判据只有 `kind === "ephemeral"`，**不做**"进行中"过滤（已归档的
  // 照样列，见上一条）。所以小节标题/空态**不能**写"进行中"——否则左栏切到「已归档」
  // 后，面板标题写"进行中"、行标写"已归档"，用户一步即达矛盾。这里把措辞钉住。
  it("小节标题是「临时对话」而不是「进行中的临时对话」（与 aria-label/徽标术语统一）", () => {
    const sectionTitle = () =>
      Array.from(
        container.querySelectorAll<HTMLElement>(
          ".workspace-section-title",
        ),
      ).map((node) => node.textContent);

    render({
      conversations: [
        conversation({ id: "c-temp", title: "临时探索 A", kind: "ephemeral" }),
        conversation({
          id: "c-temp-2",
          title: "临时探索 B",
          kind: "ephemeral",
          status: "archived",
        }),
      ],
    });

    expect(sectionTitle()).toEqual(["当前会话的分支树", "临时对话"]);
    // 已归档的仍在列表里 + 行标如实写「已归档」，而标题不再声称只有"进行中"。
    expect(temporaryButtonNames()).toEqual(["临时探索 A", "临时探索 B"]);
    expect(container.textContent).not.toContain("进行中的临时对话");
    expect(container.textContent).not.toContain("正在进行的临时对话");
  });

  it("没有临时对话时给出空态", () => {
    render({ conversations: [conversation({ kind: "normal" })] });
    expect(temporaryButtonNames()).toEqual([]);
    expect(container.textContent).toContain("当前没有临时对话");
    // 空态也不许再声称"正在进行中"。
    expect(container.textContent).not.toContain("正在进行的临时对话");
  });

  it("点击临时对话行交出该会话 id（实参断言）", () => {
    const onOpenConversation = vi.fn();
    render({
      conversations: [
        conversation({ id: "c-temp", title: "临时探索", kind: "ephemeral" }),
      ],
      onOpenConversation,
    });

    act(() =>
      container
        .querySelector<HTMLButtonElement>(".workspace-temporary-list button")!
        .click(),
    );

    expect(onOpenConversation.mock.calls).toEqual([["c-temp"]]);
  });
});

describe("C4 会话视图：新建入口", () => {
  it("「新建临时对话」把点击交给既有创建回调（不在视图里另造流程）", () => {
    const onCreateTemporaryConversation = vi.fn();
    render({ onCreateTemporaryConversation });

    const button = container.querySelector<HTMLButtonElement>(
      ".workspace-temporary-new",
    );
    expect(button?.textContent).toContain("新建临时对话");
    act(() => button!.click());

    expect(onCreateTemporaryConversation).toHaveBeenCalledTimes(1);
  });
});
