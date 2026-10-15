// @vitest-environment jsdom
/**
 * `SessionRow`：左栏会话行的可见标识（P0-B / B2 新增用例）。
 *
 * 背景：临时对话**已经出现在左栏**，但此前这行只渲染标题，于是它看起来和普通对话
 * 一模一样——而它「关闭即丢弃」。这里钉住两向：临时/分支**有**标识、普通对话**没有**。
 *
 * 标识数据取自既有 payload 字段（`apiTypes.ts` 的 `Conversation.kind` 与
 * `parentConversationId`），不新增后端字段。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Conversation } from "./apiTypes";
import { SessionRow } from "./SessionRow";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const conversation = (overrides: Partial<Conversation> = {}): Conversation => ({
  id: "conv_1",
  title: "季度复盘",
  status: "active",
  nextTurnOrdinal: 3,
  titleIsManual: false,
  createdAt: "2026-09-14T00:00:00.000Z",
  updatedAt: "2026-09-14T00:00:00.000Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
  workspaceId: null,
  ...overrides,
});

let container: HTMLDivElement;
let root: Root;

const render = (item: Conversation, renaming = false) => {
  act(() => {
    root.render(
      <SessionRow
        active={false}
        conversation={item}
        onChangeStatus={() => {}}
        onDelete={() => {}}
        onRenameCancel={() => {}}
        onRenameCommit={() => {}}
        onRenameDraftChange={() => {}}
        onRenameStart={() => {}}
        onSelect={() => {}}
        renameDraft=""
        renaming={renaming}
      />,
    );
  });
};

const badge = () => container.querySelector(".session-item-badge");
const titleNode = () => container.querySelector(".session-item-title");

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("SessionRow（左栏标识：临时对话 / 分支）", () => {
  it(`纯临时对话有「临时」标识（kind === "ephemeral"、无父对话）`, () => {
    render(conversation({ kind: "ephemeral", parentConversationId: null }));
    expect(badge()).not.toBeNull();
    expect(badge()!.textContent).toBe("临时");
    // 提示里如实说明后果（读屏可达），与本批统一的「丢弃后不可恢复」同词。
    expect(badge()!.getAttribute("title")).toContain("丢弃后不可恢复");
  });

  it(`普通对话没有任何标识（kind === "normal" 且无父对话）`, () => {
    render(conversation());
    expect(badge()).toBeNull();
    expect(titleNode()!.textContent).toBe("季度复盘");
  });

  it("分支对话有「分支」标识（parentConversationId != null）", () => {
    render(conversation({ parentConversationId: "conv_parent" }));
    expect(badge()).not.toBeNull();
    expect(badge()!.textContent).toBe("分支");
    expect(badge()!.getAttribute("title")).toContain("分支");
  });

  it("两者同时成立时显示优先级：临时优先（承载「丢弃后不可恢复」这条不可逆后果）", () => {
    // 后端 `create_branch` 的分支默认就是 `kind=ephemeral` + 有父对话，
    // 所以"两者都真"是常态而不是边角。
    render(conversation({ kind: "ephemeral", parentConversationId: "conv_parent" }));
    expect(badge()!.textContent).toBe("临时");
    expect(badge()!.textContent).not.toBe("分支");
  });

  it("临时对话的行菜单动作词是「丢弃临时对话」，普通对话仍是「删除」", () => {
    // 左栏已经会说「临时」，行菜单却写笼统的「删除」，看起来像普通的"删除对话"。
    // `RowMenu` 走 `createPortal` 挂到 `document.body`，所以查的是 body 而不是 container。
    const menuLabels = () => {
      act(() => container.querySelector<HTMLButtonElement>(".session-menu-button")!.click());
      return Array.from(
        document.querySelectorAll('[role="menuitem"]'),
      ).map((item) => item.textContent);
    };

    render(conversation({ kind: "ephemeral" }));
    const ephemeralLabels = menuLabels();
    expect(ephemeralLabels).toContain("丢弃临时对话");
    expect(ephemeralLabels).not.toContain("删除");

    act(() => root.unmount());
    container.replaceChildren();
    root = createRoot(container);
    render(conversation());
    const normalLabels = menuLabels();
    expect(normalLabels).toContain("删除");
    expect(normalLabels).not.toContain("丢弃临时对话");
  });

  it("重命名输入态不渲染标识（此时这一行显示的是输入框）", () => {
    render(conversation({ kind: "ephemeral" }), true);
    expect(badge()).toBeNull();
  });
});
