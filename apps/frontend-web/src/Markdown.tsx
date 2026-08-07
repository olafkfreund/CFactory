import { type ReactNode } from "react";

// Minimal, safe Markdown renderer for copilot answers and imported issue bodies
// (#213). Both sources emit GitHub-flavoured markdown (headings, **bold**,
// *italic*, `code`, fenced code, bullet/numbered lists, task lists, links,
// paragraphs); we render that subset as React nodes — never via
// dangerouslySetInnerHTML — so neither LLM output nor a third party's issue body
// can inject HTML/script. Anything we don't recognise falls through as plain
// text, which means raw HTML in an issue body renders as the visible characters
// it is rather than as markup. Anchors are the one place a string reaches an
// attribute, so `safeHref` allowlists http(s) — a `javascript:` URL in an issue
// body must not become a clickable one.

const INLINE =
  /(\[[^\]\n]+\]\([^)\s]+\)|\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*|_[^_\n]+_|https?:\/\/[^\s<>()]+)/g;
const LINK = /^\[([^\]\n]+)\]\(([^)\s]+)\)$/;

// A capture group the pattern makes mandatory. `noUncheckedIndexedAccess` types
// every index as possibly-undefined, which for a group that always participates
// in a successful match it is not; read those through here rather than repeat a
// `?? ""` at every call-site. Empty string is the only sane floor: a group that
// somehow did not participate contributes no text, and every consumer below is
// rendering text.
const grp = (m: RegExpExecArray, i: number): string => m[i] ?? "";

// A URL we are willing to put in an href. Anything else (javascript:, data:, a
// protocol-relative //host) stays inert text.
function safeHref(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

function link(href: string, text: string, key: string): ReactNode {
  const safe = safeHref(href);
  if (!safe) return <span key={key}>{text}</span>;
  return (
    <a key={key} href={safe} target="_blank" rel="noreferrer noopener">
      {text}
    </a>
  );
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = grp(m, 0);
    const key = `${keyPrefix}-i${i++}`;
    const md = LINK.exec(tok);
    if (md) nodes.push(link(grp(md, 2), grp(md, 1), key));
    else if (tok.startsWith("http")) nodes.push(link(tok, tok, key));
    else if (tok.startsWith("**")) nodes.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) nodes.push(<code key={key}>{tok.slice(1, -1)}</code>);
    else nodes.push(<em key={key}>{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// A GFM task-list item: "[ ] thing" / "[x] thing". Returned as the state plus the
// remaining text so the item renders a real (disabled) checkbox — an issue body's
// checklist is the most information-dense thing in it, and "[x] " as literal
// characters reads as noise.
const TASK = /^\[([ xX])\]\s+(.*)$/;

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "h"; level: number; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; lines: string[]; lang: string };

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = [];
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let para: string[] = [];
  let fence: { kind: "code"; lines: string[]; lang: string } | null = null;
  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", lines: para });
    para = [];
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    // A fence swallows everything verbatim until it closes, so markdown INSIDE a
    // code block stays code. An unterminated fence still renders (below).
    const fenceMark = /^\s*```(.*)$/.exec(line);
    if (fence) {
      if (fenceMark) {
        blocks.push(fence);
        fence = null;
      } else {
        fence.lines.push(raw);
      }
      continue;
    }
    if (fenceMark) {
      flushPara();
      fence = { kind: "code", lines: [], lang: grp(fenceMark, 1).trim() };
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (line.trim() === "") {
      flushPara();
    } else if (heading) {
      flushPara();
      blocks.push({ kind: "h", level: grp(heading, 1).length, text: grp(heading, 2) });
    } else if (bullet) {
      flushPara();
      const prev = blocks.at(-1);
      if (prev && prev.kind === "ul") prev.items.push(grp(bullet, 1));
      else blocks.push({ kind: "ul", items: [grp(bullet, 1)] });
    } else if (numbered) {
      flushPara();
      const prev = blocks.at(-1);
      if (prev && prev.kind === "ol") prev.items.push(grp(numbered, 1));
      else blocks.push({ kind: "ol", items: [grp(numbered, 1)] });
    } else {
      para.push(line);
    }
  }
  flushPara();
  if (fence) blocks.push(fence); // unterminated fence — show it, don't drop it
  return blocks;
}

// One list item, with a GFM checkbox when it is a task-list item.
function Item({ text, keyPrefix }: { text: string; keyPrefix: string }) {
  const task = TASK.exec(text);
  if (!task) return <>{renderInline(text, keyPrefix)}</>;
  return (
    <>
      <input
        type="checkbox"
        className="md-task"
        checked={grp(task, 1).toLowerCase() === "x"}
        readOnly
        disabled
        aria-label={grp(task, 2)}
      />
      {renderInline(grp(task, 2), keyPrefix)}
    </>
  );
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
          const tasks = b.items.some((it) => TASK.test(it));
          return (
            <ul className={tasks ? "md-tasks" : undefined} key={bi}>
              {b.items.map((it, ii) => (
                <li key={ii}>
                  <Item text={it} keyPrefix={`ul${bi}-${ii}`} />
                </li>
              ))}
            </ul>
          );
        }
        if (b.kind === "ol") {
          return (
            <ol key={bi}>
              {b.items.map((it, ii) => (
                <li key={ii}>
                  <Item text={it} keyPrefix={`ol${bi}-${ii}`} />
                </li>
              ))}
            </ol>
          );
        }
        if (b.kind === "code") {
          // The language is a label, never a class we act on — an issue body's
          // fence info string is untrusted, so it goes in a data attribute.
          return (
            <pre className="md-pre" key={bi} data-lang={b.lang || undefined}>
              <code>{b.lines.join("\n")}</code>
            </pre>
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
