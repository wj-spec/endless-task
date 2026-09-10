// @vitest-environment jsdom
/**
 * `UserMessageRow`：用户消息行（显示态 + 就地编辑态）。
 *
 * 从 `ChatWorkSurface.tsx` 的轮次渲染块搬出后补的直测——此前这段"编辑按钮什么时候出现、
 * 保存按钮什么时候禁用、技能角标怎么来的"只能靠 e2e 顺带覆盖。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  UserMessageRow,
  type UserMessageRowProps,
} from "./UserMessageRow";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const baseProps: UserMessageRowProps = {
  turnId: "turn_1",
  content: "原始提问",
  belongsToConversation: true,
  isLatest: true,
  status: "completed",
  laneBusy: false,
  isGenerating: false,
  pending: false,
  editing: false,
  editDraft: "",
  onEditDraftChange: () => {},
  onCancelEdit: () => {},
  onStartEdit: () => {},
  onEditResend: () => {},
};

let container: HTMLDivElement;
let root: Root;

const render = (props: Partial<UserMessageRowProps> = {}) => {
  act(() => {
    root.render(<UserMessageRow {...baseProps} {...props} />);
  });
};

const text = () => container.textContent ?? "";
const button = (label: string) =>
  Array.from(container.querySelectorAll("button")).find(
    (item) => item.textContent === label,
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

describe("UserMessageRow（显示态）", () => {
  it("显示原文与复制按钮；最新且属于本会话时提供「编辑」", () => {
    render();
    expect(container.querySelector(".user-copy")?.textContent).toBe("原始提问");
    expect(button("编辑")).not.toBeUndefined();
    expect(text()).not.toContain("保存并重新生成");
  });

  it("编辑后的文案覆盖原文", () => {
    render({ editedContent: "改过的提问" });
    expect(container.querySelector(".user-copy")?.textContent).toBe("改过的提问");
  });

  it("用户显式指定的技能渲染成角标", () => {
    render({ content: "/review-notes 看看这个 /release-check" });
    const chips = Array.from(container.querySelectorAll(".user-skill-chip")).map(
      (item) => item.textContent,
    );
    expect(chips).toEqual(["已加载技能 /review-notes", "已加载技能 /release-check"]);
  });

  it("不满足条件时不给「编辑」：非最新 / 不属于本会话 / 运行中 / 没回调", () => {
    for (const props of [
      { isLatest: false },
      { belongsToConversation: false },
      { status: "running" },
      { status: "created" },
      { onEditResend: undefined },
    ] as Partial<UserMessageRowProps>[]) {
      render(props);
      expect(button("编辑")).toBeUndefined();
    }
  });

  it("运行中或等待时可以显示，但「编辑」按钮禁用", () => {
    render({ laneBusy: true });
    expect(button("编辑")?.disabled).toBe(true);
    render({ isGenerating: true });
    expect(button("编辑")?.disabled).toBe(true);
    render({ pending: true });
    expect(button("编辑")?.disabled).toBe(true);
  });
});

describe("UserMessageRow（编辑态）", () => {
  it("文本框带标签与当前草稿，保存会带上轮次 id 与裁剪后的正文", () => {
    const onEditResend = vi.fn();
    const onCancelEdit = vi.fn();
    render({
      editing: true,
      editDraft: "  改后的提问  ",
      onEditResend,
      onCancelEdit,
    });
    const editor = container.querySelector<HTMLTextAreaElement>(
      '[aria-label="编辑这条消息"]',
    )!;
    expect(editor.value).toBe("  改后的提问  ");
    act(() => {
      button("保存并重新生成")!.click();
    });
    expect(onEditResend).toHaveBeenCalledWith("turn_1", "改后的提问");
    expect(onCancelEdit).toHaveBeenCalledTimes(1);
  });

  it("草稿为空或与原文相同时保存禁用", () => {
    render({ editing: true, editDraft: "" });
    expect(button("保存并重新生成")?.disabled).toBe(true);
    render({ editing: true, editDraft: "原始提问" });
    expect(button("保存并重新生成")?.disabled).toBe(true);
    render({ editing: true, editDraft: "改过了" });
    expect(button("保存并重新生成")?.disabled).toBe(false);
  });

  it("与编辑后文案相同也算没改（拿 editedContent 比较）", () => {
    render({ editing: true, editedContent: "已经改过", editDraft: "已经改过" });
    expect(button("保存并重新生成")?.disabled).toBe(true);
  });

  it("取消只回调，不触发重新生成", () => {
    const onEditResend = vi.fn();
    const onCancelEdit = vi.fn();
    render({ editing: true, editDraft: "改过了", onEditResend, onCancelEdit });
    act(() => {
      button("取消")!.click();
    });
    expect(onCancelEdit).toHaveBeenCalledTimes(1);
    expect(onEditResend).not.toHaveBeenCalled();
  });
});
