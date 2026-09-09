import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ArtifactProposal } from "../chat/apiTypes";
import { ArtifactProposalCard, ArtifactReminder } from "./ArtifactProposalCard";

const proposal = (over: Partial<ArtifactProposal> = {}): ArtifactProposal => ({
  id: "p1",
  conversationId: "c1",
  turnId: "t1",
  title: "发布记录",
  kind: "markdown",
  content: "# 发布记录\n\n本次发布包含三项改动。",
  reason: "本轮产出了可复用的发布记录。",
  status: "pending",
  sourceLabels: [],
  targetArtifactId: null,
  baseVersionOrdinal: null,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  resolvedArtifactId: null,
  resolvedAt: null,
  ...over,
});

const reminder = (
  over: Partial<ArtifactProposal> = {},
  props: Partial<Parameters<typeof ArtifactReminder>[0]> = {},
) =>
  renderToStaticMarkup(
    <ArtifactReminder
      busy={false}
      error={null}
      expanded={false}
      onResolve={() => undefined}
      onToggle={() => undefined}
      proposal={proposal(over)}
      resolvedArtifact={null}
      {...props}
    />,
  );

describe("ArtifactReminder（紧凑提醒）", () => {
  it("折叠态只有一行：类型、标题、规模与操作，不渲染正文与理由", () => {
    const html = reminder();
    expect(html).toContain("文档");
    expect(html).toContain("发布记录");
    expect(html).toContain("19 字");
    expect(html).toContain("待确认");
    expect(html).toContain('aria-expanded="false"');
    // 正文与理由都不在折叠态里，卡片高度才压得住。
    expect(html).not.toContain("本轮产出了可复用的发布记录。");
    expect(html).not.toContain("本次发布包含三项改动。");
    expect(html).not.toContain("proposal-compact-detail");
  });

  it("展开态渲染理由与 Markdown 正文", () => {
    const html = reminder({}, { expanded: true });
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain("proposal-compact-detail");
    expect(html).toContain("本轮产出了可复用的发布记录。");
    expect(html).toContain("本次发布包含三项改动。");
    // Markdown 走 MessageContent（标题渲染为 h1），而不是纯文本。
    expect(html).toContain("<h1>发布记录</h1>");
    expect(html).not.toContain("proposal-compact-plain");
  });

  it("纯文本 Artifact 展开后保留换行", () => {
    const html = reminder(
      { kind: "text", content: "第一行\n第二行" },
      { expanded: true },
    );
    expect(html).toContain("proposal-compact-plain");
    expect(html).toContain("第一行\n第二行");
  });

  it("更新类提案展示版本基线与更新操作", () => {
    const html = reminder({
      targetArtifactId: "a1",
      baseVersionOrdinal: 2,
    });
    expect(html).toContain("更新《发布记录》");
    expect(html).toContain("基于 v2");
    expect(html).toContain('aria-label="更新文档"');
  });

  it("处理中禁用操作按钮并给出失败提示", () => {
    const busyHtml = reminder({}, { busy: true });
    expect(busyHtml).toContain("处理中");
    expect(busyHtml).toContain("disabled");

    const errorHtml = reminder({}, { error: "保存失败" });
    expect(errorHtml).toContain("处理失败");
    expect(errorHtml).toContain('role="alert"');
    expect(errorHtml).toContain("保存失败");
  });

  it("保留/放弃按钮带完整可访问名称", () => {
    const html = reminder();
    expect(html).toContain('aria-label="保留为 Artifact"');
    expect(html).toContain('aria-label="放弃该提案"');
  });
});

describe("ArtifactProposalCard", () => {
  it("待确认时默认折叠", () => {
    const html = renderToStaticMarkup(
      <ArtifactProposalCard
        busy={false}
        error={null}
        proposal={proposal()}
        resolvedArtifact={null}
        onResolve={() => undefined}
      />,
    );
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain("本次发布包含三项改动。");
  });

  it("已采纳时给出结果位置与打开入口", () => {
    const html = renderToStaticMarkup(
      <ArtifactProposalCard
        busy={false}
        error={null}
        onOpen={() => undefined}
        onResolve={() => undefined}
        proposal={proposal({ status: "accepted", resolvedArtifactId: "a1" })}
        resolvedArtifact={{
          id: "a1",
          title: "发布记录",
          kind: "markdown",
          status: "active",
          currentVersionOrdinal: 3,
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
          deletedAt: null,
        }}
      />,
    );
    expect(html).toContain("已保存到工作区 Artifact《发布记录》 v3");
    expect(html).toContain("打开 Artifact");
  });

  it("已放弃时只保留一行结果", () => {
    const html = renderToStaticMarkup(
      <ArtifactProposalCard
        busy={false}
        error={null}
        onResolve={() => undefined}
        proposal={proposal({ status: "rejected" })}
        resolvedArtifact={null}
      />,
    );
    expect(html).toContain("已放弃该提案");
    expect(html).not.toContain("proposal-compact-actions");
  });
});
