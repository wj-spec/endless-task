import { useCallback, useRef, useState } from "react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { writeClipboard } from "./clipboard";

// 只注册常用语言子集，控制包体；语言名可经别名归一。
const REGISTERED = [
  ["bash", bash],
  ["c", c],
  ["cpp", cpp],
  ["css", css],
  ["diff", diff],
  ["go", go],
  ["java", java],
  ["javascript", javascript],
  ["json", json],
  ["markdown", markdown],
  ["python", python],
  ["rust", rust],
  ["sql", sql],
  ["typescript", typescript],
  ["xml", xml],
  ["yaml", yaml],
] as const;

for (const [name, language] of REGISTERED) {
  if (!hljs.getLanguage(name)) hljs.registerLanguage(name, language);
}

// markdown 围栏中常见的别名/大小写归一，避免误命中未注册语言
const LANGUAGE_ALIASES: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  html: "xml",
  htm: "xml",
  svg: "xml",
  txt: "",
  text: "",
  plaintext: "",
  console: "",
};

const normalizeLanguage = (raw: string): string => {
  const cleaned = raw.trim().toLowerCase();
  if (!cleaned) return "";
  if (hljs.getLanguage(cleaned)) return cleaned;
  const alias = LANGUAGE_ALIASES[cleaned];
  if (alias === undefined) return cleaned; // 未注册语言：按原文尝试，失败则回退无高亮
  return alias;
};

type CodeBlockProps = {
  code: string;
  language?: string;
};

export function CodeBlock({ code, language = "" }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  const normalized = normalizeLanguage(language);
  const canHighlight = normalized !== "" && hljs.getLanguage(normalized) !== undefined;
  const highlightHtml = canHighlight
    ? hljs.highlight(code, { language: normalized, ignoreIllegals: true }).value
    : null;

  const handleCopy = useCallback(async () => {
    await writeClipboard(code);
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 2000);
  }, [code]);

  const displayLanguage = normalized || language || "text";

  return (
    <div className="code-block">
      <div className="code-block-head">
        <span className="code-block-lang">{displayLanguage}</span>
        <button
          type="button"
          aria-label={copied ? "已复制" : `复制代码（${displayLanguage}）`}
          className="code-block-copy"
          onClick={() => void handleCopy()}
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>
        {highlightHtml !== null ? (
          // highlight.js 输出对其输入做了完整转义（不会注入原始标签/属性），
          // 是 render 链中唯一且受控的 HTML 注入点。
          <code
            dangerouslySetInnerHTML={{ __html: highlightHtml }}
            className="hljs"
          />
        ) : (
          <code>{code}</code>
        )}
      </pre>
    </div>
  );
}
