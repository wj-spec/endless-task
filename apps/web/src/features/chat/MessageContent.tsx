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
  let ordered: string[] = [];
  let quote: string[] = [];

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

  const flushOrdered = () => {
    if (ordered.length === 0) return;
    nodes.push(
      <ol key={`ordered-${nodes.length}`}>
        {ordered.map((item, index) => (
          <li key={`${index}-${item}`}>{inline(item)}</li>
        ))}
      </ol>,
    );
    ordered = [];
  };

  const flushQuote = () => {
    if (quote.length === 0) return;
    nodes.push(
      <blockquote key={`quote-${nodes.length}`}>
        {quote.map((item, index) => (
          <p key={`${index}-${item}`}>{inline(item)}</p>
        ))}
      </blockquote>,
    );
    quote = [];
  };

  const flushAll = () => {
    flushList();
    flushOrdered();
    flushQuote();
  };

  lines.forEach((line, index) => {
    const quoteMatch = line.match(/^\s*>\s?(.*)$/);
    if (quoteMatch) {
      flushList();
      flushOrdered();
      if (quoteMatch[1].trim()) quote.push(quoteMatch[1]);
      return;
    }
    flushQuote();
    const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
    if (listMatch) {
      flushOrdered();
      list.push(listMatch[1]);
      return;
    }
    flushList();
    const orderedMatch = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (orderedMatch) {
      ordered.push(orderedMatch[1]);
      return;
    }
    flushOrdered();
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
  flushAll();
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
