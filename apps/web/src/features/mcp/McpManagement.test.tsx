// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listMcpServers = vi.fn(async () => [
  {
    id: "srv_1",
    name: "demo",
    transport: "stdio" as const,
    command: "python",
    args: ["-m", "demo"],
    env: {},
    cwd: "",
    url: "",
    headers: {},
    enabled: true,
    toolCallTimeoutSeconds: 60,
    state: "connected",
    toolCount: 1,
    lastError: null,
    tools: [
      {
        publicName: "mcp__demo__echo",
        rawName: "echo",
        description: "Echo",
        effect: "read_only",
        requiresExplicitConfirmation: false,
      },
    ],
  },
]);

const listMcpCalls = vi.fn(async () => [
  {
    time: "2026-09-09T10:00:00.000Z",
    operation: "mcp__demo__echo",
    detail: "server=demo; status=ok",
    durationMs: 12,
    status: "ok",
  },
  {
    time: "2026-09-09T10:01:00.000Z",
    operation: "mcp__demo__boom",
    detail: "server=demo; status=error",
    durationMs: 5,
    status: "error",
  },
]);

vi.mock("../chat/api", () => ({
  chatApi: {
    listMcpServers: () => listMcpServers(),
    listMcpCalls: () => listMcpCalls(),
  },
}));

const { McpContent } = await import("./McpManagement");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

const byText = (text: string) =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
    (node) => node.textContent?.trim() === text,
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

describe("MCP 最近调用（M2）", () => {
  it("读取并展示调用耗时与状态", async () => {
    await act(async () => {
      root.render(<McpContent onChanged={() => undefined} />);
    });
    await flush();

    expect(container.textContent).toContain("mcp__demo__echo");
    await act(async () => byText("最近调用")!.click());
    await flush();

    expect(container.textContent).toContain("12 ms");
    expect(container.textContent).toContain("error");
    expect(container.querySelectorAll(".mcp-call-list li").length).toBe(2);
  });
});
