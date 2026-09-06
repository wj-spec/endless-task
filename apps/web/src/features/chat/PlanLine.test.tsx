import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PlanLine } from "./PlanLine";

const plan = {
  title: "调研计划",
  steps: [
    { title: "步骤一", status: "completed" },
    { title: "步骤二", status: "in_progress" },
  ],
};

describe("PlanLine（S-P1-3a）", () => {
  it("终态默认折叠：显示标题/摘要/展开按钮，不渲染步骤列表", () => {
    const html = renderToStaticMarkup(<PlanLine plan={plan} />);
    expect(html).toContain("调研计划");
    expect(html).toContain("已完成 1/2");
    expect(html).toContain('aria-label="展开计划"');
    expect(html).not.toContain("plan-line-steps");
  });

  it("运行中默认展开：显示步骤与状态文案", () => {
    const html = renderToStaticMarkup(<PlanLine active plan={plan} />);
    expect(html).toContain("进行中 1/2");
    expect(html).toContain("<ol");
    expect(html).toContain("步骤一");
    expect(html).toContain("完成");
    expect(html).toContain("进行中");
  });

  it("无步骤时不渲染展开按钮", () => {
    const html = renderToStaticMarkup(<PlanLine plan={{ title: "空计划" }} />);
    expect(html).not.toContain("plan-line-toggle");
    expect(html).toContain("已记录 0 步");
  });
});
