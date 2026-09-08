import { Children, createElement, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import { CodeBlock } from "./CodeBlock";

// 渲染策略（P0-1）：
// - 标准库渲染：react-markdown + remark-gfm（表格/任务列表/删除线）。
// - 不接 rehype-raw：模型/用户内容中的原始 HTML 一律按字面文本显示（转义、无损）。
//   react-markdown 会把 mdast html 节点以 raw 形式带入 hast；本插件先把 raw 还原为
//   text，避免 rehype-sanitize 对 raw 内容做解析（那会静默剥离标签），再交给 sanitize
//   对元素树做纵深防御（dangerous 协议、危险属性）。
const rawHtmlToText = () => (tree: RawAwareNode) => {
  const walk = (node: RawAwareNode): void => {
    if (node.type === "raw") node.type = "text";
    node.children?.forEach(walk);
  };
  walk(tree);
};

type RawAwareNode = { type: string; children?: RawAwareNode[] };

const SANITIZE_SCHEMA = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [["className", /^language-./]],
    input: [["type", "checkbox"], ["disabled"], ["checked"]],
  },
  tagNames: [...(defaultSchema.tagNames ?? []), "input"],
};

const CITATION_PATTERN = /\[K\d+\]/;

type CitationClickHandler = (label: string) => void;

/**
 * 把容器顶层的文本串按 [K\d+] 切成引用角标。
 *
 * 区分"真实引用"与"占位引用"：
 * - `resolvableLabels` 提供且命中 → 渲染为可点击的真实引用角标。
 * - `resolvableLabels` 提供但未命中（模型杜撰/未依据真实知识）→ 渲染为弱化的不可点占位角标，避免误导。
 * - `resolvableLabels` 未提供（数据未就绪/旧数据）→ 保持乐观可点击。
 */
const splitCitationText = (
  text: string,
  onCitationClick: CitationClickHandler | undefined,
  resolvableLabels?: Set<string>,
): ReactNode[] => {
  const segments = text.split(/(\[K\d+\])/g);
  return segments.map((segment, index) => {
    if (CITATION_PATTERN.test(segment)) {
      const label = segment.slice(1, -1);
      if (resolvableLabels && !resolvableLabels.has(label)) {
        return (
          <sup
            className="citation-chip citation-chip-unresolved"
            key={`${index}-${segment}`}
            title={`引用 ${label} 无法溯源（可能未依据真实来源）`}
          >
            {label}
          </sup>
        );
      }
      if (onCitationClick) {
        return (
          <sup key={`${index}-${segment}`}>
            <button
              type="button"
              aria-label={`查看引用 ${label} 的来源`}
              className="citation-chip citation-chip-button"
              onClick={() => onCitationClick(label)}
            >
              {label}
            </button>
          </sup>
        );
      }
      return (
        <sup className="citation-chip" key={`${index}-${segment}`} title="来自相关知识">
          {label}
        </sup>
      );
    }
    return segment;
  });
};

const textifiedChildren = (
  children: ReactNode,
  onCitationClick: CitationClickHandler | undefined,
  resolvableLabels?: Set<string>,
): ReactNode =>
  Children.map(children, (child) =>
    typeof child === "string"
      ? splitCitationText(child, onCitationClick, resolvableLabels)
      : child,
  );

// 带引用角标的行内容器集合：段落、列表项、表元、引用、标题。
const TEXT_CONTAINERS = [
  "p",
  "li",
  "td",
  "th",
  "blockquote",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
] as const;

const isSafeExternalUrl = (href: string | undefined): boolean =>
  typeof href === "string" && /^https?:\/\//i.test(href);

type MessageContentProps = {
  content: string;
  onCitationClick?: CitationClickHandler;
  /** 该轮次真正注入的引用标签集合；提供后用于区分"真实引用"与"无法溯源的占位引用"。 */
  resolvableCitationLabels?: Set<string>;
};

export function MessageContent({
  content,
  onCitationClick,
  resolvableCitationLabels,
}: MessageContentProps) {
  const components: Components = {
    // 外链新窗口打开并禁止 opener 回指；相对/同页链接保持默认行为。
    a({ href, children: linkChildren, node: _node, ...rest }) {
      if (isSafeExternalUrl(href)) {
        return (
          <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
            {linkChildren}
          </a>
        );
      }
      return (
        <a href={href} {...rest}>
          {linkChildren}
        </a>
      );
    },
    img({ alt, src, node: _node, ...rest }) {
      return <img alt={alt ?? ""} loading="lazy" decoding="async" src={src} {...rest} />;
    },
    // 围栏代码块统一走 CodeBlock（复制 + 高亮）。block code 总是被 <pre> 包裹，
    // 在 pre 层取回 code 元素的 language class 与文本，避免与行内 code 混淆。
    pre({ children }) {
      const child = Children.only(children);
      if (isValidElement<ExtraContentProps & { className?: string }>(child)) {
        const language =
          /language-([^\s]+)/.exec(child.props.className ?? "")?.[1] ?? "";
        return (
          <CodeBlock
            code={String(child.props.children ?? "").replace(/\n$/, "")}
            language={language}
          />
        );
      }
      return <pre>{children}</pre>;
    },
    // 行内 code：保持字面显示（[K\d+] 不触发引用角标）
    code({ children, className, node: _node, ...rest }) {
      return (
        <code className={className} {...rest}>
          {children}
        </code>
      );
    },
  };

  const container = (tag: (typeof TEXT_CONTAINERS)[number]) =>
    ({ children, node: _node, ...rest }: Record<string, unknown>) =>
      createElement(
        tag,
        rest,
        textifiedChildren(children as ReactNode, onCitationClick, resolvableCitationLabels),
      );
  for (const tag of TEXT_CONTAINERS) {
    (components as Record<string, unknown>)[tag] = container(tag);
  }

  return (
    <div className="message-copy">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rawHtmlToText, [rehypeSanitize, SANITIZE_SCHEMA]]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

type ExtraContentProps = { children?: ReactNode };
