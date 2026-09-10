// @vitest-environment jsdom
/**
 * `StreamNotices`：消息流顶部的错误条与"已自动回滚"通知（从 `ChatWorkSurface` 搬出）。
 *
 * 重点钉住两条容易改坏的规则：
 * ① 自动回滚提示取的是**最近一条** `run.auto_restored` / `run_auto_restored` 事件
 *    （原实现是 `[...events].reverse().find(...)`，"最近"的语义靠顺序保证）；
 * ② 载荷缺字段时用 `"?"` 占位，而不是渲染 `undefined`。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeV2ProductEvent } from "./apiTypes";
import { StreamNotices, latestAutoRestoreNotice } from "./StreamNotices";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const event = (
  type: string,
  data: Record<string, unknown>,
  seq = 1,
): RuntimeV2ProductEvent =>
  ({
    id: `evt_${seq}`,
    eventId: `evt_${seq}`,
    type,
    data,
    seq,
    eventSeq: seq,
    conversationId: "conv_1",
    laneId: "lane_1",
    runId: "run_1",
    createdAt: "2026-01-01T00:00:00Z",
  }) as RuntimeV2ProductEvent;

let container: HTMLDivElement;
let root: Root;

const render = (props: {
  error?: string | null;
  runtimeEvents?: RuntimeV2ProductEvent[];
  onDismissError?: () => void;
}) => {
  act(() => {
    root.render(
      <StreamNotices
        error={props.error ?? null}
        onDismissError={props.onDismissError ?? (() => {})}
        runtimeEvents={props.runtimeEvents}
      />,
    );
  });
};

const text = () => container.textContent ?? "";

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("latestAutoRestoreNotice（取最近一条）", () => {
  it("没有事件或事件列表为空时返回 null", () => {
    expect(latestAutoRestoreNotice(undefined)).toBeNull();
    expect(latestAutoRestoreNotice([])).toBeNull();
    expect(latestAutoRestoreNotice([event("run.completed", {})])).toBeNull();
  });

  it("两种事件名都认，且取**最后一条**", () => {
    const first = latestAutoRestoreNotice([
      event("run.auto_restored", { restored: 1, skipped: 0 }, 1),
      event("run_auto_restored", { restored: 3, skipped: 2 }, 2),
    ]);
    expect(first).toEqual({ restored: 3, skipped: 2 });
    const second = latestAutoRestoreNotice([
      event("run_auto_restored", { restored: 3, skipped: 2 }, 1),
      event("run.auto_restored", { restored: 5, skipped: 1 }, 2),
    ]);
    expect(second).toEqual({ restored: 5, skipped: 1 });
  });

  it("载荷不是对象时按没有处理", () => {
    expect(
      latestAutoRestoreNotice([
        { id: "e", type: "run.auto_restored", data: "文本", seq: 1 } as never,
      ]),
    ).toBeNull();
  });
});

describe("StreamNotices（渲染）", () => {
  it("没有错误也没有回滚事件时不渲染任何提示", () => {
    render({});
    expect(container.querySelector(".inline-error")).toBeNull();
    expect(container.querySelector(".inline-notice")).toBeNull();
    expect(text()).toBe("");
  });

  it("有错误时显示错误条并可关闭", () => {
    const onDismissError = vi.fn();
    render({ error: "请求失败，请重试。", onDismissError });
    const alert = container.querySelector(".inline-error")!;
    expect(alert.getAttribute("role")).toBe("alert");
    expect(text()).toContain("请求失败，请重试。");
    act(() => {
      Array.from(container.querySelectorAll("button"))
        .find((item) => item.textContent === "关闭")!
        .click();
    });
    expect(onDismissError).toHaveBeenCalledTimes(1);
  });

  it("有自动回滚事件时显示恢复数量", () => {
    render({
      runtimeEvents: [event("run.auto_restored", { restored: 2, skipped: 1 })],
    });
    const notice = container.querySelector(".inline-notice")!;
    expect(notice.getAttribute("role")).toBe("status");
    expect(text()).toContain("已自动回滚");
    expect(text()).toContain("2 个文件恢复，1 个跳过");
  });

  it("回滚事件缺字段时用「?」占位（不渲染 undefined）", () => {
    render({ runtimeEvents: [event("run.auto_restored", {})] });
    expect(text()).toContain("? 个文件恢复，? 个跳过");
    expect(text()).not.toContain("undefined");
  });

  it("错误与回滚提示可以同时出现（错误在前）", () => {
    render({
      error: "出错了",
      runtimeEvents: [event("run_auto_restored", { restored: 1, skipped: 0 })],
    });
    const children = Array.from(container.children);
    expect(children[0].className).toBe("inline-error");
    expect(children[1].className).toBe("inline-notice");
  });
});
