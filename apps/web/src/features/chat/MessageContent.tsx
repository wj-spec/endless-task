import type { ReactNode } from "react";

type Block =
  | { kind: "code"; language: string; content: string }
  | { kind: "text"; content: string };

const splitBlocks = (source: string): Block[] => {
  const blocks: Block[] = [];
  const pattern = /```([^\n]*)\n?([\s\S]*?)```/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > cursor) {
      blocks.push({ kind: "text", content: source.slice(cursor, match.index) });
    }
    blocks.push({
      kind: "code",
      language: match[1].trim(),
      content: match[2].replace(/\n$/, ""),
    });
    cursor = pattern.lastIndex;
  }
  if (cursor < source.length) blocks.push({ kind: "text", content: source.slice(cursor) });
  return blocks;
};

const inline = (source: string): ReactNode[] => {
  const tokens = source.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return tokens.map((token, index) => {
    if (token.startsWith("`") && token.endsWith("`")) {
      return <code key={index}>{token.slice(1, -1)}</code>;
    }
    if (token.startsWith("**") && token.endsWith("**")) {
      return <strong key={index}>{token.slice(2, -2)}</strong>;
    }
    return token;
  });
};

function TextBlock({ content }: { content: string }) {
  const lines = content.split("\n");
  const nodes: ReactNode[] = [];
  let list: string[] = [];

  const flushList = () => {
    if (list.length === 0) return;
    nodes.push(
      <ul key={`list-${nodes.length}`}>
        {list.map((item, index) => (
          <li key={`${index}-${item}`}>{inline(item)}</li>
        ))}
      </ul>,
    );
    list = [];
  };

  lines.forEach((line, index) => {
    const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
    if (listMatch) {
      list.push(listMatch[1]);
      return;
    }
    flushList();
    if (!line.trim()) {
      nodes.push(<span className="markdown-space" key={`space-${index}`} />);
      return;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const Heading = `h${heading[1].length + 2}` as "h3" | "h4" | "h5";
      nodes.push(<Heading key={`heading-${index}`}>{inline(heading[2])}</Heading>);
      return;
    }
    nodes.push(<p key={`line-${index}`}>{inline(line)}</p>);
  });
  flushList();
  return nodes;
}

export function MessageContent({ content }: { content: string }) {
  return (
    <div className="message-copy">
      {splitBlocks(content).map((block, index) =>
        block.kind === "code" ? (
          <div className="code-block" key={`code-${index}`}>
            {block.language ? <span>{block.language}</span> : null}
            <pre>
              <code>{block.content}</code>
            </pre>
          </div>
        ) : (
          <TextBlock content={block.content} key={`text-${index}`} />
        ),
      )}
    </div>
  );
}
