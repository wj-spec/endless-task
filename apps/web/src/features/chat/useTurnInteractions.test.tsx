// @vitest-environment jsdom
/**
 * `useTurnInteractions`：逐轮交互态（消息编辑 / 审批改参 / 引用卡片 / 文件预览）。
 *
 * 这些状态此前埋在 `ChatWorkSurface.tsx` 里，只能靠"渲染整个工作面 + 点角标"间接覆盖。
 * 收成 hook 后可以直测，重点是三件容易在搬迁中走样的事：
 * ① 引用列表按 `conversationId` 拉取，失败静默；
 * ② 点同一个角标是**收起**、点另一个是展开，且只有命中真实引用才上报点击；
 * ③ 切换会话时清掉展开的引用卡片。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useTurnInteractions,
  type TurnInteractions,
} from "./useTurnInteractions";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { getConversationCitations, recordCitationClick } = vi.hoisted(() => ({
  getConversationCitations: vi.fn(async () => ({})),
  recordCitationClick: vi.fn(async () => undefined),
}));

vi.mock("./api", () => ({
  ApiClientError: class extends Error {},
  chatApi: {
    getConversationCitations,
    recordCitationClick,
  },
}));

const citation = (label: string) => ({
  label,
  scope: "conversation",
  refId: `ref_${label}`,
  conversationId: "conv_1",
  title: `来源 ${label}`,
});

let container: HTMLDivElement;
let root: Root;
let latest: TurnInteractions | null = null;

function Host({ conversationId }: { conversationId?: string }) {
  latest = useTurnInteractions(conversationId);
  return <div />;
}

/** 卸载再挂载（模拟"切换会话后重新进入"），确保观察到的是新会话的状态。 */
const remount = async (conversationId?: string) => {
  act(() => root.unmount());
  container.remove();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container); // 卸载后的 root 不能再 render，必须新建
  await render(conversationId);
};

const render = async (conversationId?: string) => {
  await act(async () => {
    root.render(<Host conversationId={conversationId} />);
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  latest = null;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getConversationCitations.mockClear();
  getConversationCitations.mockResolvedValue({});
  recordCitationClick.mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useTurnInteractions", () => {
  it("初值：没有编辑/改参/引用/预览在进行", async () => {
    await render("conv_1");
    const state = latest!;
    expect(state.editTurnId).toBeNull();
    expect(state.editDraft).toBe("");
    expect(state.modifyApprovalId).toBeNull();
    expect(state.modifyDraft).toBe("");
    expect(state.modifyError).toBeNull();
    expect(state.openCitation).toBeNull();
    expect(state.previewTarget).toBeNull();
    expect(state.citationsByTurn).toEqual({});
  });

  it("按会话拉取引用列表，失败时静默（保持空表，不阻断交互）", async () => {
    getConversationCitations.mockResolvedValueOnce({
      turn_1: [citation("K1")],
    });
    await render("conv_1");
    expect(getConversationCitations).toHaveBeenCalledWith("conv_1");
    expect(Object.keys(latest!.citationsByTurn)).toEqual(["turn_1"]);

    // 引用拉取失败不阻断交互：这里必须**卸载重挂**来观察新会话的状态——
    // 只改 props 的话实例还在，上一会话的引用表会留着（第一版就是这么误判的）。
    getConversationCitations.mockRejectedValueOnce(new Error("boom"));
    await remount("conv_2");
    expect(latest!.citationsByTurn).toEqual({});
  });

  it("没有会话时不请求引用列表", async () => {
    await render(undefined);
    expect(getConversationCitations).not.toHaveBeenCalled();
    expect(latest!.citationsByTurn).toEqual({});
  });

  it("点角标展开引用卡片；同一角标再点一次收起", async () => {
    getConversationCitations.mockResolvedValue({
      turn_1: [citation("K1")],
    });
    await render("conv_1");
    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K1");
    });
    expect(latest!.openCitation).toEqual({ turnId: "turn_1", label: "K1" });

    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K1");
    });
    expect(latest!.openCitation).toBeNull();
  });

  it("命中真实引用才上报点击；未命中（占位角标）只展开不上报", async () => {
    getConversationCitations.mockResolvedValue({
      turn_1: [citation("K1")],
    });
    await render("conv_1");
    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K1");
    });
    expect(recordCitationClick).toHaveBeenCalledWith({
      label: "K1",
      scope: "conversation",
      refId: "ref_K1",
      turnId: "turn_1",
      conversationId: "conv_1",
    });

    recordCitationClick.mockClear();
    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K9");
    });
    expect(latest!.openCitation).toEqual({ turnId: "turn_1", label: "K9" });
    expect(recordCitationClick).not.toHaveBeenCalled();
  });

  it("上报点击失败不影响展开态（静默）", async () => {
    getConversationCitations.mockResolvedValue({ turn_1: [citation("K1")] });
    recordCitationClick.mockRejectedValueOnce(new Error("network"));
    await render("conv_1");
    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K1");
    });
    expect(latest!.openCitation).toEqual({ turnId: "turn_1", label: "K1" });
  });

  it("切换会话时清掉展开的引用卡片（避免残留上一会话的卡片）", async () => {
    getConversationCitations.mockResolvedValue({ turn_1: [citation("K1")] });
    await render("conv_1");
    await act(async () => {
      await latest!.handleCitationClick("turn_1", "K1");
    });
    expect(latest!.openCitation).not.toBeNull();
    act(() => {
      latest!.resetForConversation();
    });
    expect(latest!.openCitation).toBeNull();
  });

  it("编辑态与改参态可独立设置与清除", async () => {
    await render("conv_1");
    act(() => {
      latest!.setEditTurnId("turn_1");
      latest!.setEditDraft("改过的正文");
      latest!.setModifyApprovalId("appr_1");
      latest!.setModifyDraft("{}");
      latest!.setModifyError("参数必须是 JSON 对象。");
      latest!.setPreviewTarget({
        path: "notes.md",
        workspaceId: "ws_1",
      } as never);
    });
    expect(latest!.editTurnId).toBe("turn_1");
    expect(latest!.editDraft).toBe("改过的正文");
    expect(latest!.modifyApprovalId).toBe("appr_1");
    expect(latest!.modifyDraft).toBe("{}");
    expect(latest!.modifyError).toBe("参数必须是 JSON 对象。");
    expect(latest!.previewTarget).toMatchObject({ path: "notes.md" });

    act(() => {
      latest!.setEditTurnId(null);
      latest!.setModifyApprovalId(null);
      latest!.setModifyError(null);
      latest!.setPreviewTarget(null);
    });
    expect(latest!.editTurnId).toBeNull();
    expect(latest!.modifyApprovalId).toBeNull();
    expect(latest!.modifyError).toBeNull();
    expect(latest!.previewTarget).toBeNull();
  });
});
