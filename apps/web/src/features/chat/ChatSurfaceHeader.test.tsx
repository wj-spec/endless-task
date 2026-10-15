// @vitest-environment jsdom
/**
 * `ChatSurfaceHeader`：工作面顶栏（P0-B 词级收口新增的最小覆盖）。
 *
 * 这个文件此前没有用例。这里**只**钉住本次改动的「丢弃临时对话」这一组动作词——
 * 「保存为正式对话」和「丢弃临时对话」必须与关闭右侧工作面区分。
 * 其余菜单项/分支导航行为不在本用例范围。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ChatSurfaceHeader, type ChatSurfaceHeaderProps } from "./ChatSurfaceHeader";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const baseProps: ChatSurfaceHeaderProps = {
  archived: false,
  branchLanes: [],
  conversation: null,
  currentLaneId: null,
  currentLaneLabel: "主线",
  isGenerating: false,
  pendingAction: null,
  renaming: false,
  sideMode: null,
  titleDraft: "",
  variant: "side",
  onArchive: () => {},
  onMenu: () => {},
  onRequestSideClose: () => {},
  onRestore: () => {},
  onSearch: () => {},
  onSetConfirmingDelete: () => {},
  onSetRenaming: () => {},
  onSubmitTitle: () => {},
  onTitleDraftChange: () => {},
};

let container: HTMLDivElement;
let root: Root;

const render = (props: Partial<ChatSurfaceHeaderProps> = {}) => {
  act(() => {
    root.render(<ChatSurfaceHeader {...baseProps} {...props} />);
  });
};

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const text = () => container.textContent ?? "";

describe("ChatSurfaceHeader（P0-B「丢弃临时对话」动作词）", () => {
  it("临时对话：关闭按钮读屏名与后果提示都用「丢弃」，并如实说明不可恢复", () => {
    render({ sideMode: "temporary_conversation" });
    expect(container.querySelector(".side-close")!.getAttribute("aria-label")).toBe(
      "关闭并丢弃临时对话",
    );
    expect(text()).toContain("丢弃后不可恢复");
    // 旧词守卫要覆盖**所有属性**（旧版只有 textContent 的负向断言，对 `aria-label`
    // 无效）：`innerHTML` 是完整序列化，属性值也在里面。
    expect(container.innerHTML).not.toContain("关闭即删除");
    expect(container.innerHTML).not.toContain("关闭并删除临时对话");
    expect(container.innerHTML).not.toContain("删除临时对话");
  });

  it("右侧对照：仍是「收起」，不含「丢弃」——两种关闭语义不许混用", () => {
    render({ sideMode: "branch_lane", currentLaneLabel: "探索方案" });
    // 正向：可逆的那一支用 plan 术语「右侧对照」（可见按钮名与 region 名同词）。
    expect(container.querySelector(".side-close")!.getAttribute("aria-label")).toBe(
      "收起右侧对照",
    );
    expect(text()).toContain("关闭仅收起，不删除分支");
    expect(text()).not.toContain("丢弃");
    // 防漂移反向守卫（P0-B 收口）：旧词「分支对照」不许再出现在任何用户可见/可听到的
    // 文案里。`innerHTML` 是完整序列化（属性值也在里面）⇒ 能盖住 `aria-label`。
    // ℹ️ 这条正是 C0 已确立的"允许保留旧词"的例外之一：旧词只出现在**负向**断言里。
    expect(container.innerHTML).not.toContain("分支对照");
  });
});
