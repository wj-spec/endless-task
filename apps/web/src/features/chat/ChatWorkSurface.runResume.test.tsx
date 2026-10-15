// @vitest-environment jsdom
/**
 * E3/B：硬停横幅的「继续」在**可复活**形态下必须落到"同一条 run 复活"这条通道上，
 * 而且判据来自后端快照（`revivableRunId`）。
 *
 * 为什么这份用例在 `ChatWorkSurface` 这一层：这条接线是
 *   `snapshot.revivableRunId` → `RuntimeStatusStrip` → `EscalationNotice` →
 *   `ChatWorkSurface.onResumeRun` → `onResolveRuntimeRecovery(runId, "resume")`
 * 五段串起来的。本会话反复踩到"**接线改坏了也不红**"（C3b 一次抓到 3 段），
 * 所以这里**渲染真组件**、只给回调打桩（`vi.fn()`），断言**实际收到的实参**——
 * 不做 `vi.mock` 整模块替换（那会把被测接线一起换掉）。
 *
 * 反向也钉住：不可复活的终态（`revivableRunId` 为空）仍然走「新开一轮」，
 * **不许**借用复活通道。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import { ChatWorkSurface } from "./ChatWorkSurface";
import type {
  Conversation,
  ConversationSnapshot,
  RuntimeV2Escalation,
  RuntimeV2Snapshot,
} from "./apiTypes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CONVERSATION_ID = "conv-e3b-resume";

const conversation: Conversation = {
  id: CONVERSATION_ID,
  title: "硬停后接着跑",
  status: "active",
  nextTurnOrdinal: 2,
  titleIsManual: false,
  createdAt: "2026-09-15T10:00:00Z",
  updatedAt: "2026-09-15T10:20:00Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
  workspaceId: null,
};

const conversationSnapshot: ConversationSnapshot = {
  conversation,
  turns: [],
  files: [],
} as unknown as ConversationSnapshot;

/** 后端下发的硬停升级（`willStop=true`；终态后**特意**保留）。 */
const escalation: RuntimeV2Escalation = {
  runId: "run_1",
  reason: "time_budget_exhausted",
  summary: "运行时长已到阈值（15 分钟）。",
  options: ["continue", "change_approach", "take_over"],
  progress: {
    modelTurns: 1,
    toolCalls: 1,
    toolFailures: 0,
    producedCharacters: 7,
    inputTokens: 11,
    outputTokens: 4,
    elapsedSeconds: 900,
  },
  budget: {
    usedTokens: 15,
    limitTokens: null,
    usedRatio: 0,
    usedSeconds: 900,
    limitSeconds: 900,
  },
  repeatedFailures: [],
  failures: [],
  guidance: "到上限不是故障。",
  willStop: true,
};

const runtimeSnapshot = (revivableRunId: string | null): RuntimeV2Snapshot =>
  ({
    snapshotVersion: 1,
    conversationId: CONVERSATION_ID,
    activeLaneId: "lane_1",
    mainLaneId: "lane_1",
    runningLaneId: null,
    runningRunId: null,
    activeRunId: "run_1",
    activeRunVariantId: "run_1",
    revivableRunId,
    lastEventSeq: 7,
    entries: [],
    runState: null,
    pendingApprovals: [],
    toolStates: [],
    contextUsage: { inputTokens: 0, outputTokens: 0 },
    interruptedRuns: [],
    stuck: null,
    escalation,
    verification: null,
    usage: null,
    capabilities: [],
  }) as unknown as RuntimeV2Snapshot;

type SurfaceProps = Parameters<typeof ChatWorkSurface>[0];

let container: HTMLDivElement;
let root: Root;

const surfaceProps = (
  revivableRunId: string | null,
  overrides: Partial<SurfaceProps>,
): SurfaceProps =>
  ({
    conversation: conversationSnapshot,
    draft: "",
    error: null,
    health: null,
    isGenerating: false,
    liveTurns: {},
    loading: false,
    pendingAction: null,
    providers: [],
    runtimeEvents: [],
    runtimeSnapshot: runtimeSnapshot(revivableRunId),
    currentLaneId: "lane_1",
    onArchive: () => undefined,
    onCancel: () => undefined,
    onDelete: () => undefined,
    onDismissError: () => undefined,
    onDraftChange: () => undefined,
    onMenu: () => undefined,
    onPromote: () => undefined,
    onRegenerate: () => undefined,
    onResolveApproval: () => undefined,
    onRemoveFile: () => undefined,
    onRename: () => undefined,
    onRestore: () => undefined,
    onRetry: () => undefined,
    onSelectVariant: () => undefined,
    onSend: () => undefined,
    onModelChange: () => undefined,
    onUploadFile: () => undefined,
    proposalBusyId: null,
    proposalErrors: {},
    resolvedArtifacts: {},
    turnProposals: () => ({
      artifacts: [],
      memories: [],
      tasks: [],
      knowledge: [],
    }),
    onResolveArtifactProposal: () => undefined,
    onResolveKnowledgeProposal: () => undefined,
    onResolveMemoryProposal: () => undefined,
    onResolveTaskProposal: () => undefined,
    ...overrides,
  }) as unknown as SurfaceProps;

const render = (props: SurfaceProps) => {
  act(() => {
    root.render(<ChatWorkSurface {...props} />);
  });
};

const button = (label: string) =>
  Array.from(container.querySelectorAll("button")).find(
    (item) => item.textContent === label,
  );

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // 真模块 + 打桩方法（不是整模块替换）。
  vi.spyOn(chatApi, "listConversations").mockResolvedValue([conversation]);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("ChatWorkSurface：硬停横幅「继续」的分流接线（E3/B）", () => {
  it("可复活（后端给出 revivableRunId）：落 onResolveRuntimeRecovery(runId,「resume」)", () => {
    const onResolveRuntimeRecovery = vi.fn();
    const onDraftChange = vi.fn();
    render(
      surfaceProps("run_1", { onDraftChange, onResolveRuntimeRecovery }),
    );

    const resume = button("继续（接上这条运行）");
    expect(resume).not.toBeUndefined();

    act(() => resume!.click());

    // 实际收到的实参：**同一个 run** + 显式的 `resume` 动作。
    expect(onResolveRuntimeRecovery).toHaveBeenCalledTimes(1);
    expect(onResolveRuntimeRecovery).toHaveBeenCalledWith("run_1", "resume");
    // 反证：没有偷偷走"新开一轮"（那会把内容塞进草稿）。
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it("不可复活（revivableRunId 为空）：仍然走「新开一轮」，不碰恢复通道", () => {
    const onResolveRuntimeRecovery = vi.fn();
    const onDraftChange = vi.fn();
    render(
      surfaceProps(null, { onDraftChange, onResolveRuntimeRecovery }),
    );

    const freshTurn = button("新开一轮");
    expect(freshTurn).not.toBeUndefined();
    expect(button("继续（接上这条运行）")).toBeUndefined();

    act(() => freshTurn!.click());

    expect(onResolveRuntimeRecovery).not.toHaveBeenCalled();
    expect(onDraftChange).toHaveBeenCalledTimes(1);
    expect(onDraftChange.mock.calls[0][0]).toContain("请继续按当前思路推进");
  });
});
