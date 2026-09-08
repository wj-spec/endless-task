import type { RuntimeV2Lane } from "./apiTypes";

// 去掉 Markdown 语法符号：强调/代码/标题/引用/链接/图片/表格竖线。
// 链接转为文本（[a](url) -> a），图片转为 alt（![a](url) -> a）。
const MARKDOWN_RE =
  /!\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|[*_`~#>]|\n/g;

const cleanMarkdown = (value: string): string =>
  value
    .replace(MARKDOWN_RE, (_match, imageAlt?: string, linkText?: string) => {
      if (imageAlt !== undefined) return imageAlt;
      if (linkText !== undefined) return linkText;
      return "";
    })
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();

/**
 * 生成一个适合放进标题/横幅/标签的简短、友好的分支名。
 *
 * 分支默认展示名会回退到 `summary`/`baseEntryExcerpt`（来源助手消息的原始内容，
 * 常含 Markdown 符号且很长）。这里统一清洗 Markdown、折叠空白并按位截断，
 * 避免把整段正文当成标题塞进 header / 横幅（issue：#问题二、#问题三）。
 */
export const friendlyLaneName = (
  lane: RuntimeV2Lane | null | undefined,
  fallback = "未命名分支",
  maxLength = 24,
): string => {
  if (!lane) return fallback;
  const raw =
    (lane.displayName ?? "").trim() ||
    (lane.title ?? "").trim() ||
    (lane.baseEntryExcerpt ?? "").trim() ||
    (lane.summary ?? "").trim();
  if (!raw) return fallback;
  const cleaned = cleanMarkdown(raw);
  if (!cleaned) return fallback;
  return cleaned.length > maxLength
    ? `${cleaned.slice(0, maxLength).trimEnd()}…`
    : cleaned;
};
