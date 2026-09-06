import { describe, expect, it } from "vitest";
import { ApiClientError } from "./api";
import { readableError } from "./apiErrorText";

describe("readableError", () => {
  it("surfaces the ApiClientError message", () => {
    const error = new ApiClientError({ ok: false, status: 500 } as Response, {
      error: { code: "x", message: "上游故障" },
    } as never);
    expect(readableError(error)).toBe("上游故障");
  });

  it("maps abort errors to empty (silent cancel)", () => {
    const error = new DOMException("aborted", "AbortError");
    expect(readableError(error)).toBe("");
  });

  it("falls back to the local-service hint for anything else", () => {
    expect(readableError("boom")).toBe("无法连接本地服务，请确认 API 已启动。");
    expect(readableError(null)).toBe("无法连接本地服务，请确认 API 已启动。");
    expect(readableError(new Error("boom"))).toBe(
      "无法连接本地服务，请确认 API 已启动。",
    );
  });
});
