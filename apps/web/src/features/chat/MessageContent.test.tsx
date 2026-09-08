import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MessageContent } from "./MessageContent";

const render = (
  content: string,
  onCitationClick?: (label: string) => void,
  resolvableCitationLabels?: Set<string>,
) =>
  renderToStaticMarkup(
    <MessageContent
      content={content}
      onCitationClick={onCitationClick}
      resolvableCitationLabels={resolvableCitationLabels}
    />,
  );

describe("MessageContent（react-markdown P0-1 渲染）", () => {
  it("渲染段落与标题", () => {
    const html = render("# 标题\n\n正文第一段。");
    expect(html).toContain("<h1>标题</h1>");
    expect(html).toContain("正文第一段。");
  });

  it("外链新窗口并带 noopener", () => {
    const html = render("[OpenAI](https://openai.com)");
    expect(html).toContain(
      '<a href="https://openai.com" target="_blank" rel="noopener noreferrer">OpenAI</a>',
    );
  });

  it("拒绝 javascript: 协议链接", () => {
    const html = render("[x](javascript:alert(1))");
    expect(html).not.toContain("javascript:");
  });

  it("原始 HTML 转义为字面文本（无注入面）", () => {
    const html = render("before <b>bold</b> after <script>alert(1)</script> end");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;b&gt;bold&lt;/b&gt;");
    expect(html).toContain("&lt;script&gt;");
  });

  it("渲染 GFM 表格与删除线", () => {
    const html = render("|a|b|\n|-|-|\n|1|2|\n\n~~gone~~");
    expect(html).toContain("<table>");
    expect(html).toContain("<del>gone</del>");
  });

  it("渲染任务列表 checkbox", () => {
    const html = render("- [x] done\n- [ ] todo");
    expect(html).toContain('<input type="checkbox" disabled="" checked=""/>');
    expect(html).toContain('<input type="checkbox" disabled=""/>');
  });

  it("围栏代码块带语言标签与复制按钮，并高亮转义", () => {
    const html = render('```python\nprint("<script>")\n```');
    expect(html).toContain('<span class="code-block-lang">python</span>');
    expect(html).toContain('aria-label="复制代码（python）"');
    expect(html).toContain("复制</button>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain('<script>alert');
  });

  it("无语言围栏回退为纯文本代码块（text 标签，无 hljs 注入）", () => {
    const html = render("```\nplain <tag>\n```");
    expect(html).toContain('aria-label="复制代码（text）"');
    expect(html).toContain("&lt;tag&gt;");
    expect(html).not.toContain("hljs");
  });

  it("未注册语言（如 elixir）不做高亮但保留代码", () => {
    const html = render("```elixir\nIO.puts 1\n```");
    expect(html).toContain("IO.puts 1");
    expect(html).not.toContain("hljs");
  });

  it("带点击回调时 [K1] 渲染为可点引用角标", () => {
    const html = render("见 [K1] 与 [K12]", (label) => label);
    expect(html).toContain('aria-label="查看引用 K1 的来源"');
    expect(html).toContain('aria-label="查看引用 K12 的来源"');
    expect(html).toContain("citation-chip-button");
  });

  it("无回调时引用渲染为只读角标（无 button）", () => {
    const html = render("见 [K1] 来源");
    expect(html).toContain("citation-chip");
    expect(html).not.toContain("citation-chip-button");
  });

  it("resolvableCitationLabels 命中时渲染为可点引用角标", () => {
    const html = render("见 [K1]", (label) => label, new Set(["K1"]));
    expect(html).toContain("citation-chip-button");
    expect(html).toContain('aria-label="查看引用 K1 的来源"');
  });

  it("resolvableCitationLabels 未命中时渲染为弱化占位引用（不可点）", () => {
    const html = render("见 [K2]", (label) => label, new Set(["K3"]));
    expect(html).toContain("citation-chip-unresolved");
    expect(html).not.toContain("citation-chip-button");
    expect(html).toContain("K2");
  });

  it("未提供 resolvableCitationLabels 时保持乐观可点（数据未就绪）", () => {
    const html = render("见 [K1]", (label) => label, undefined);
    expect(html).toContain("citation-chip-button");
    expect(html).not.toContain("citation-chip-unresolved");
  });

  it("行内代码中的 [K1] 不触发引用角标", () => {
    const html = render("code `x [K1] y` here", (label) => label);
    expect(html).not.toContain("查看引用 1 的来源");
    expect(html).toContain("<code>x [K1] y</code>");
  });

  it("引用角标不进入粗体等内联节点内部", () => {
    const html = render("**bold [K1] still**", (label) => label);
    expect(html).toContain("<strong>bold [K1] still</strong>");
    expect(html).not.toContain("查看引用 1 的来源");
  });

  it("渲染图片（懒加载）", () => {
    const html = render("![alt text](https://example.com/a.png)");
    expect(html).toContain('<img alt="alt text" loading="lazy"');
  });
});
