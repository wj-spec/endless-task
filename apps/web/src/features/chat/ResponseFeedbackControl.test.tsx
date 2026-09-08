import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ResponseFeedbackControl } from "./ResponseFeedbackControl";

const render = (props?: Partial<React.ComponentProps<typeof ResponseFeedbackControl>>) =>
  renderToStaticMarkup(
    <ResponseFeedbackControl
      conversationId="c1"
      turnId="t1"
      variantId={null}
      {...props}
    />,
  );

describe("ResponseFeedbackControl", () => {
  it("渲染 👍/👎 两个反馈按钮", () => {
    const html = render();
    expect(html).toContain('aria-label="满意"');
    expect(html).toContain('aria-label="不满意"');
  });

  it("初始不打开原因面板、不显示已评分", () => {
    const html = render();
    expect(html).not.toContain("feedback-panel");
    expect(html).not.toContain("已评分");
  });

  it("禁用时不响应（按钮 disabled 属性存在）", () => {
    const html = render({ disabled: true });
    expect(html).toContain('disabled=""');
  });
});
