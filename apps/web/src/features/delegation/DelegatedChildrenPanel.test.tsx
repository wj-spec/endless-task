// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DelegatedChildRun } from "../chat/apiTypes";

/** 三条样本：running（在跑、无摘要）、queued（未开始，elapsedSeconds 为 null）、completed（终态 + 结果摘要）。 */
const rows: DelegatedChildRun[] = [
  {
    childRunId: "child_run_alpha_000001",
    status: "running",
    task: "重构取数层并补测试",
    createdAt: "2026-09-11T10:00:00Z",
    startedAt: "2026-09-11T10:00:05Z",
    terminalAt: null,
    elapsedSeconds: 65,
    summary: null,
    errorCode: null,
    safeMessage: null,
    workspaceMode: "shared",
  },
  {
    childRunId: "child_run_beta_000002",
    status: "queued",
    task: "写迁移脚本",
    createdAt: "2026-09-11T10:01:00Z",
    startedAt: null,
    terminalAt: null,
    elapsedSeconds: null,
    summary: null,
    errorCode: null,
    safeMessage: null,
    workspaceMode: "isolated_snapshot",
  },
  {
    childRunId: "child_run_gamma_000003",
    status: "completed",
    task: "整理发布说明",
    createdAt: "2026-09-11T09:00:00Z",
    startedAt: "2026-09-11T09:00:02Z",
    terminalAt: "2026-09-11T09:02:10Z",
    elapsedSeconds: 128,
    summary: "已完成 3 个文件的整理。",
    errorCode: null,
    safeMessage: null,
    workspaceMode: "shared",
  },
];

class MockApiClientError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(
    response: Response,
    payload?: { error?: { code?: string; message?: string } },
  ) {
    super(payload?.error?.message ?? `请求失败（${response.status}）`);
    this.name = "ApiClientError";
    this.status = response.status;
    this.code = payload?.error?.code ?? "request_failed";
  }
}

const listDelegatedChildren = vi.fn(
  async (_conversationId: string): Promise<DelegatedChildRun[]> => rows,
);
const cancelDelegatedChild = vi.fn(
  async (_conversationId: string, childRunId: string) => ({
    childRunId,
    status: "cancelling",
  }),
);

vi.mock("../chat/api", () => ({
  ApiClientError: MockApiClientError,
  chatApi: {
    listDelegatedChildren: (conversationId: string) =>
      listDelegatedChildren(conversationId),
    cancelDelegatedChild: (conversationId: string, childRunId: string) =>
      cancelDelegatedChild(conversationId, childRunId),
  },
}));

const { ApiClientError } = await import("../chat/api");
const { DelegatedChildrenPanel } = await import("./DelegatedChildrenPanel");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

const render = async () => {
  await act(async () => {
    root.render(<DelegatedChildrenPanel conversationId="conv_1" />);
  });
  await flush();
};

/** 换会话就是换 prop：同一个组件实例重渲染（挂载点没有 key）。 */
const rerenderWith = async (conversationId: string | null) => {
  await act(async () => {
    root.render(<DelegatedChildrenPanel conversationId={conversationId} />);
  });
  await flush();
};

/** 两个会话各自的样本行：任务文本带会话名，便于断言"现在渲染的是谁的列表"。 */
const aRows: DelegatedChildRun[] = [
  { ...rows[0], childRunId: "child_run_a_000001", task: "会话 A 的任务" },
];
const bRows: DelegatedChildRun[] = [
  { ...rows[0], childRunId: "child_run_b_000001", task: "会话 B 的任务" },
];

const cancelButtons = () =>
  Array.from(container.querySelectorAll<HTMLButtonElement>(".memory-actions button"));

beforeEach(() => {
  listDelegatedChildren.mockReset();
  listDelegatedChildren.mockImplementation(async () => rows);
  cancelDelegatedChild.mockReset();
  cancelDelegatedChild.mockImplementation(async (_conversationId, childRunId) => ({
    childRunId,
    status: "cancelling",
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("子任务页签", () => {
  it("加载中显示骨架，不提前显示空态", async () => {
    listDelegatedChildren.mockImplementationOnce(
      () => new Promise<DelegatedChildRun[]>(() => {}),
    );
    await render();

    expect(container.querySelector(".skeleton-panel")).not.toBeNull();
    expect(container.textContent).not.toContain("还没有子任务");
  });

  it("渲染中文状态、任务、已用时与终态摘要", async () => {
    await render();

    const text = container.textContent ?? "";
    expect(text).toContain("运行中");
    expect(text).toContain("排队中"); // queued 不能漏成空/英文
    expect(text).toContain("已完成");
    expect(text).toContain("重构取数层并补测试");
    expect(text).toContain("已用时 1 分 5 秒");
    expect(text).toContain("尚未开始"); // 排队中：elapsedSeconds 为 null
    expect(text).toContain("已用时 2 分 8 秒");
    expect(text).toContain("已完成 3 个文件的整理。");
    // total 是返回条数（最多 50），文案只能是"最近 N 条"
    expect(text).toContain("最近 3 条");
    expect(text).not.toContain("共有");
    // 不外漏内部 id，也不外漏英文状态
    expect(text).not.toContain("child_run_alpha_000001");
    expect(container.innerHTML).not.toContain("child_run");
    expect(text).not.toContain("running");
    expect(text).not.toContain("completed");
  });

  it("终态词表的中文标签全覆盖（failed/cancelled/interrupted）且不可取消", async () => {
    listDelegatedChildren.mockImplementation(
      async (): Promise<DelegatedChildRun[]> => [
        { ...rows[0], childRunId: "c_failed", status: "failed" },
        { ...rows[0], childRunId: "c_cancelled", status: "cancelled" },
        { ...rows[0], childRunId: "c_interrupted", status: "interrupted" },
      ],
    );
    await render();

    const text = container.textContent ?? "";
    expect(text).toContain("失败");
    expect(text).toContain("已取消");
    expect(text).toContain("已中断");
    expect(container.querySelectorAll(".memory-actions").length).toBe(0);
  });

  it("没有选中会话时显示空态且不请求接口", async () => {
    await act(async () => {
      root.render(<DelegatedChildrenPanel conversationId={null} />);
    });
    await flush();

    expect(container.textContent).toContain("还没有子任务");
    expect(listDelegatedChildren).not.toHaveBeenCalled();
  });

  it("已用时按分/小时换算（59 秒 / 1 分 / 1 小时 / 1 小时 1 分）", async () => {
    listDelegatedChildren.mockImplementation(
      async (): Promise<DelegatedChildRun[]> =>
        [59, 60, 3600, 3660].map((seconds, index) => ({
          ...rows[0],
          childRunId: `c_elapsed_${index}`,
          status: "completed",
          elapsedSeconds: seconds,
        })),
    );
    await render();

    const text = container.textContent ?? "";
    expect(text).toContain("已用时 59 秒");
    expect(text).toContain("已用时 1 分");
    expect(text).toContain("已用时 1 小时");
    expect(text).toContain("已用时 1 小时 1 分");
  });

  it("只有 running/queued 行可取消", async () => {
    await render();

    const actionRows = Array.from(
      container.querySelectorAll<HTMLDivElement>(".memory-item"),
    ).filter((row) => row.querySelector(".memory-actions") !== null);
    expect(actionRows.length).toBe(2);
    expect(actionRows[0].textContent).toContain("重构取数层并补测试");
    expect(actionRows[1].textContent).toContain("写迁移脚本");
  });

  it("没有子运行时显示空态", async () => {
    listDelegatedChildren.mockImplementation(async () => []);
    await render();

    expect(container.textContent).toContain("还没有子任务");
    expect(container.textContent).toContain(
      "主 Agent 派生子代理后，这里会列出它们的进度和结果。",
    );
    expect(container.querySelector(".skeleton-panel")).toBeNull();
  });

  it("点击取消调用 cancelDelegatedChild 并重新加载一次", async () => {
    await render();
    expect(listDelegatedChildren).toHaveBeenCalledTimes(1);

    await act(async () => cancelButtons()[0].click());
    await flush();

    expect(cancelDelegatedChild).toHaveBeenCalledTimes(1);
    expect(cancelDelegatedChild).toHaveBeenCalledWith(
      "conv_1",
      "child_run_alpha_000001",
    );
    expect(listDelegatedChildren).toHaveBeenCalledTimes(2);
    expect(cancelButtons()[0].disabled).toBe(false);
  });

  it("排队中的子运行也能取消", async () => {
    await render();

    await act(async () => cancelButtons()[1].click());
    await flush();

    expect(cancelDelegatedChild).toHaveBeenCalledWith(
      "conv_1",
      "child_run_beta_000002",
    );
  });

  it("取消请求进行中时禁用该行按钮", async () => {
    let resolveCancel: (value: { childRunId: string; status: string }) => void =
      () => {};
    cancelDelegatedChild.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCancel = resolve;
        }),
    );
    await render();

    await act(async () => cancelButtons()[0].click());
    expect(cancelButtons()[0].disabled).toBe(true);

    await act(async () => {
      resolveCancel({
        childRunId: "child_run_alpha_000001",
        status: "cancelling",
      });
    });
    await flush();

    expect(cancelButtons()[0].disabled).toBe(false);
  });

  it("取消失败时显示错误文案且不谎报成功", async () => {
    cancelDelegatedChild.mockRejectedValueOnce(
      new ApiClientError(new Response(null, { status: 409 }), {
        error: {
          code: "delegation_child_owner_unavailable",
          message: "该子运行的执行协调器已不在本进程内，暂不能取消。",
        },
      }),
    );
    await render();

    await act(async () => cancelButtons()[0].click());
    await flush();

    const alert = container.querySelector(".proposal-error");
    expect(alert?.textContent).toBe(
      "该子运行的执行协调器已不在本进程内，暂不能取消。",
    );
    // 失败不触发重新加载（没有"取消中"的假象状态）
    expect(listDelegatedChildren).toHaveBeenCalledTimes(1);
    expect(cancelButtons()[0].disabled).toBe(false);
  });

  it("加载失败显示重试，点重试后渲染列表", async () => {
    listDelegatedChildren.mockRejectedValueOnce(new Error("boom"));
    await render();

    expect(container.textContent).toContain("没加载出来");
    const retry = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent === "重试");
    expect(retry).toBeDefined();

    await act(async () => retry!.click());
    await flush();

    expect(container.textContent).toContain("重构取数层并补测试");
    expect(container.querySelector(".proposal-error")).toBeNull();
  });

  it("切换会话时立刻丢弃上一个会话的列表并回到加载态", async () => {
    let resolveB: (value: DelegatedChildRun[]) => void = () => {};
    listDelegatedChildren.mockImplementation((conversationId: string) => {
      if (conversationId === "conv_a") return Promise.resolve(aRows);
      return new Promise<DelegatedChildRun[]>((resolve) => {
        resolveB = resolve;
      });
    });

    await rerenderWith("conv_a");
    expect(container.textContent).toContain("会话 A 的任务");
    expect(container.textContent).toContain("最近 1 条");

    // B 的请求还没回来：既不能留 A 的行，也不能闪空态，只能是骨架。
    await rerenderWith("conv_b");
    expect(container.textContent).not.toContain("会话 A 的任务");
    expect(container.textContent).not.toContain("最近");
    expect(container.querySelector(".skeleton-panel")).not.toBeNull();
    expect(container.textContent).not.toContain("还没有子任务");

    await act(async () => {
      resolveB(bRows);
    });
    await flush();

    expect(container.textContent).toContain("会话 B 的任务");
    expect(container.textContent).not.toContain("会话 A 的任务");
    expect(container.querySelector(".skeleton-panel")).toBeNull();
  });

  it("过期响应晚到不能覆盖当前会话的列表", async () => {
    let resolveA: (value: DelegatedChildRun[]) => void = () => {};
    listDelegatedChildren.mockImplementation((conversationId: string) => {
      if (conversationId === "conv_a") {
        return new Promise<DelegatedChildRun[]>((resolve) => {
          resolveA = resolve;
        });
      }
      return Promise.resolve(bRows);
    });

    await rerenderWith("conv_a");
    expect(listDelegatedChildren).toHaveBeenCalledWith("conv_a");

    await rerenderWith("conv_b");
    expect(container.textContent).toContain("会话 B 的任务");

    // A 的响应在 B 之后才到：必须整包丢弃，不能覆盖列表、不能清掉加载态。
    await act(async () => {
      resolveA(aRows);
    });
    await flush();

    expect(container.textContent).toContain("会话 B 的任务");
    expect(container.textContent).not.toContain("会话 A 的任务");
    expect(container.querySelector(".skeleton-panel")).toBeNull();
  });

  it("过期响应不能把新会话的加载态提前收掉", async () => {
    let resolveA: (value: DelegatedChildRun[]) => void = () => {};
    let resolveB: (value: DelegatedChildRun[]) => void = () => {};
    listDelegatedChildren.mockImplementation((conversationId: string) => {
      return new Promise<DelegatedChildRun[]>((resolve) => {
        if (conversationId === "conv_a") resolveA = resolve;
        else resolveB = resolve;
      });
    });

    await rerenderWith("conv_a");
    await rerenderWith("conv_b");

    // B 还在路上，A 的响应先回来：A 既不能落地成列表，也不能把 loading 关掉，
    // 否则会闪一下"还没有子任务"的空态。
    await act(async () => {
      resolveA(aRows);
    });
    await flush();

    expect(container.querySelector(".skeleton-panel")).not.toBeNull();
    expect(container.textContent).not.toContain("会话 A 的任务");
    expect(container.textContent).not.toContain("还没有子任务");

    await act(async () => {
      resolveB(bRows);
    });
    await flush();

    expect(container.textContent).toContain("会话 B 的任务");
    expect(container.querySelector(".skeleton-panel")).toBeNull();
  });

  it("切换会话后，旧会话的取消完成不会把旧列表拉回来", async () => {
    let resolveCancel: (value: { childRunId: string; status: string }) => void =
      () => {};
    cancelDelegatedChild.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCancel = resolve;
        }),
    );
    const requested: string[] = [];
    listDelegatedChildren.mockImplementation((conversationId: string) => {
      requested.push(conversationId);
      return Promise.resolve(conversationId === "conv_a" ? aRows : bRows);
    });

    await rerenderWith("conv_a");
    await act(async () => cancelButtons()[0].click());
    expect(cancelDelegatedChild).toHaveBeenCalledWith("conv_a", "child_run_a_000001");

    await rerenderWith("conv_b");
    expect(container.textContent).toContain("会话 B 的任务");

    // 旧会话的取消这时才返回：它手里的 load 是旧会话的闭包，不能覆盖 B 的列表。
    await act(async () => {
      resolveCancel({ childRunId: "child_run_a_000001", status: "cancelling" });
    });
    await flush();

    expect(container.textContent).toContain("会话 B 的任务");
    expect(container.textContent).not.toContain("会话 A 的任务");
    expect(requested).toEqual(["conv_a", "conv_b"]);
  });

  it("从无会话切到有会话时先显示骨架而不是空态", async () => {
    let resolveA: (value: DelegatedChildRun[]) => void = () => {};
    listDelegatedChildren.mockImplementation(
      () =>
        new Promise<DelegatedChildRun[]>((resolve) => {
          resolveA = resolve;
        }),
    );

    await rerenderWith(null);
    expect(container.textContent).toContain("还没有子任务");

    await rerenderWith("conv_a");
    expect(container.querySelector(".skeleton-panel")).not.toBeNull();
    expect(container.textContent).not.toContain("还没有子任务");

    await act(async () => {
      resolveA(aRows);
    });
    await flush();

    expect(container.textContent).toContain("会话 A 的任务");
  });
});
