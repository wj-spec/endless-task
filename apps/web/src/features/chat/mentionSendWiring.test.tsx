// @vitest-environment jsdom
/**
 * C3b：**UI 发送回调 → 提交路径**这一段的接线（引用不许在这里被吞掉）。
 *
 * 为什么单独有这份文件：`sendRuntimeV2Message` 是**位置参数**，`references` 排在
 * 最后一位。在 `send()` / `sendSide()` 里少传一个实参，TypeScript 不会报错
 * （形参有默认值），四个门禁也全绿——而用户侧的表现就是"我选了引用，模型没看到"。
 * `apiWiring.test.ts` 只钉住 `chatApi` 那一层（请求体长什么样），钉不住"调用方到底
 * 传没传"。这里把两个发送入口的实参逐项钉住。
 *
 * 真模块、真 hook（不 `vi.mock("./api")`）：只把 `sendRuntimeV2Message` 换成记录器，
 * 断言的就是"它实际收到了什么"。
 */
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeV2ConversationReference } from "./apiTypes";
import { useMessageActions, type MessageActionsParams } from "./useMessageActions";
import {
  useSideConversation,
  type SideConversationParams,
} from "./useSideConversation";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SENDING = "conv-c3b-seam";
const LANE = "lane-c3b-seam";
const DRAFT = "把引用的结论也带上";

const references: RuntimeV2ConversationReference[] = [
  {
    kind: "conversation",
    conversationId: "conv-c3b-seam-other",
    mode: "recent",
    recentTurns: 5,
    entryIds: [],
  },
];

type Call = unknown[];
let calls: Call[] = [];
const record = async (...args: unknown[]) => {
  calls.push(args);
  return undefined;
};

let container: HTMLDivElement;
let root: Root;
let latestSend:
  | ((references?: RuntimeV2ConversationReference[]) => Promise<boolean>)
  | null = null;

function MainHost() {
  const actions = useMessageActions({
    draft: DRAFT,
    activeConversationId: SENDING,
    isGenerating: false,
    activeRunRunning: false,
    activeRunningRunId: null,
    viewLaneIds: { [SENDING]: LANE },
    mainLaneIds: {},
    setDraft: () => undefined,
    setCommandFeedback: () => undefined,
    sendRuntimeV2Message: record,
  } as unknown as MessageActionsParams);
  useEffect(() => {
    latestSend = actions.send;
  });
  return <div />;
}

/** 只满足 hook 在挂载期读到的几个属性（其余一律空实现，用到即暴露）。 */
const runtimeControllerStub = new Proxy(
  { snapshots: {}, connections: {} },
  {
    get: (target: Record<string, unknown>, prop: string) =>
      prop in target ? target[prop] : () => undefined,
  },
) as never;

function SideHost() {
  const actions = useSideConversation({
    sideDraft: DRAFT,
    sideConversationId: SENDING,
    sideIsGenerating: false,
    sideLane: null,
    mainLaneIds: { [SENDING]: LANE },
    runtimeController: runtimeControllerStub,
    setSideDraft: () => undefined,
    setCommandFeedback: () => undefined,
    sendRuntimeV2Message: record,
  } as unknown as SideConversationParams);
  useEffect(() => {
    latestSend = actions.sendSide;
  });
  return <div />;
}

const render = (element: React.ReactElement) => {
  act(() => root.render(element));
};

beforeEach(() => {
  calls = [];
  latestSend = null;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("C3b：发送入口把 `@` 引用交给提交路径", () => {
  it("主区 send(references)：实参顺序与内容逐项一致（幂等键不在这一层）", async () => {
    render(<MainHost />);
    expect(latestSend).not.toBeNull();
    await act(async () => {
      await latestSend!(references);
    });
    expect(calls).toEqual([[SENDING, DRAFT, LANE, "main", references]]);
  });

  it("侧区 sendSide(references)：同样原样交给提交路径", async () => {
    render(<SideHost />);
    expect(latestSend).not.toBeNull();
    await act(async () => {
      await latestSend!(references);
    });
    // 侧区没有 sideLane（临时对话）⇒ 目标面是 `main`、lane 取主线 lane。
    expect(calls).toEqual([[SENDING, DRAFT, LANE, "main", references]]);
  });
});
