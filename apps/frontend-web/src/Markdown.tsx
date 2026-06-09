import { type ReactNode } from "react";

// Minimal, safe Markdown renderer for copilot answers. The copilot emits GitHub-
// flavoured markdown (headings, **bold**, *italic*, `code`, bullet/numbered
// lists, paragraphs); we render that subset as React nodes — never via
// dangerouslySetInnerHTML — so LLM output can't inject HTML/script. Anything we
// don't recognise falls through as plain text.

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*|_[^_\n]+_)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyPrefix}-i${i++}`;
    if (tok.startsWith("**")) nodes.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) nodes.push(<code key={key}>{tok.slice(1, -1)}</code>);
    else nodes.push(<em key={key}>{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "h"; level: number; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] };

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = [];
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", lines: para });
    para = [];
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (line.trim() === "") {
      flushPara();
    } else if (heading) {
      flushPara();
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2] });
    } else if (bullet) {
      flushPara();
      const prev = blocks[blocks.length - 1];
      if (prev && prev.kind === "ul") prev.items.push(bullet[1]);
      else blocks.push({ kind: "ul", items: [bullet[1]] });
    } else if (numbered) {
      flushPara();
      const prev = blocks[blocks.length - 1];
      if (prev && prev.kind === "ol") prev.items.push(numbered[1]);
      else blocks.push({ kind: "ol", items: [numbered[1]] });
    } else {
      para.push(line);
    }
  }
  flushPara();
  return blocks;
}

export default function Markdown({ text, className }: { text: string; className?: string }) {
  const blocks = parseBlocks(text);
  return (
    <div className={`md${className ? ` ${className}` : ""}`}>
      {blocks.map((b, bi) => {
        if (b.kind === "h") {
          const lvl = Math.min(b.level, 4);
          return (
            <div className={`md-h md-h${lvl}`} key={bi}>
              {renderInline(b.text, `h${bi}`)}
            </div>
          );
        }
        if (b.kind === "ul") {
          return (
            <ul key={bi}>
              {b.items.map((it, ii) => (
                <li key={ii}>{renderInline(it, `ul${bi}-${ii}`)}</li>
              ))}
            </ul>
          );
        }
        if (b.kind === "ol") {
          return (
            <ol key={bi}>
              {b.items.map((it, ii) => (
                <li key={ii}>{renderInline(it, `ol${bi}-${ii}`)}</li>
              ))}
            </ol>
          );
        }
        // Paragraph: join wrapped lines, preserving intentional breaks.
        return (
          <p key={bi}>
            {b.lines.map((ln, li) => (
              <span key={li}>
                {renderInline(ln, `p${bi}-${li}`)}
                {li < b.lines.length - 1 ? <br /> : null}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
