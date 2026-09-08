import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeV2Verification } from "./apiTypes";
import { VerificationBadge } from "./VerificationBadge";

const base: RuntimeV2Verification = {
  runId: "run_1",
  status: "verified",
  verdict: "pass",
  reasons: [],
  missing: [],
  model: "verifier-model",
};

describe("VerificationBadge（C1 独立验证）", () => {
  it("通过时展示徽标与验证模型", () => {
    const html = renderToStaticMarkup(<VerificationBadge state={base} />);
    expect(html).toContain("独立验证：通过");
    expect(html).toContain("verifier-model");
    expect(html).not.toContain("带结论重试");
  });

  it("未通过时展示理由、缺失项与两条出路", () => {
    const html = renderToStaticMarkup(
      <VerificationBadge
        onRetry={() => undefined}
        onTakeOver={() => undefined}
        state={{
          ...base,
          verdict: "fail",
          reasons: ["报告未写入"],
          missing: ["report.md"],
        }}
      />,
    );
    expect(html).toContain("独立验证：未通过");
    expect(html).toContain("报告未写入");
    expect(html).toContain("report.md");
    expect(html).toContain("带结论重试");
    expect(html).toContain("人工接管");
  });

  it("验证中不显示操作按钮", () => {
    const html = renderToStaticMarkup(
      <VerificationBadge
        onRetry={() => undefined}
        onTakeOver={() => undefined}
        state={{ ...base, status: "verifying", verdict: null }}
      />,
    );
    expect(html).toContain("独立验证中");
    expect(html).not.toContain("带结论重试");
  });

  it("不确定状态用独立样式类", () => {
    const html = renderToStaticMarkup(
      <VerificationBadge state={{ ...base, verdict: "uncertain" }} />,
    );
    expect(html).toContain("verification-uncertain");
    expect(html).toContain("独立验证：不确定");
  });
});
