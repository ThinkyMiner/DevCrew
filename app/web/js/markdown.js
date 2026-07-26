// markdown.js — SAFE markdown rendering (block + inline).
//
// We render a subset of UNTRUSTED model output as proper formatted DOM so the
// transcript reads like rendered prose, NOT raw markdown source: headings show
// at heading sizes, lists become real bullets/numbers, blockquotes indent, and
// `[text](url)` becomes a clickable link — no `#`, `-`, `*`, or `[]()` syntax
// is ever visible on the frontend.
//
// Two layers:
//   * BLOCK parser (parseBlocks): splits the source into heading / list /
//     blockquote / hr / fenced-code / paragraph blocks. Pure (no DOM).
//   * INLINE tokenizer (tokenizeInline): within a block's text, splits out code
//     spans, links, bold, and italic. Pure (no DOM).
//
// The cardinal rule: we NEVER assign untrusted text to innerHTML. The renderer
// appends only DOM text nodes + a fixed set of elements (headings, <ul>/<ol>/
// <li>, <blockquote>, <hr>, <p>, <br>, <pre>/<code>/<strong>/<em>/<a>) whose
// text is set via textContent. Link hrefs pass through safeHref (scheme
// allowlist). Because content only ever reaches the DOM as a text node, markup
// in the source ("<img onerror=...>") is displayed literally and can never
// execute. Anything we don't recognize falls through as plain text.
//
// EMPHASIS RULE (code-discussion safe):
//   The previous regex italicized intra-word `_`/`*`, mangling `do_a_thing`,
//   `foo_bar_baz.py`, `2*3*4=24`, `a*b`, `path/to_file` into <em>. We fixed it:
//     1. `_` NEVER produces emphasis. Underscores appear constantly inside
//        identifiers/filenames, so `_italic_` is dropped entirely.
//     2. `*italic*` / `**bold**` only match when the run is FLANKED at word
//        boundaries (CommonMark-ish): the opening delimiter must not be
//        preceded by an alphanumeric, and the closing delimiter must not be
//        followed by an alphanumeric. That keeps `2*3*4`, `a*b` literal while
//        still rendering ` *real italic* ` and `**bold**`.
//   Inline code spans are split out FIRST, so their contents are always literal
//   (e.g. `` `_x_` `` and `` `2*3*4` `` stay verbatim).
//
// `tokenizeInline` is a PURE function (no DOM) returning a flat list of
// segments — unit-tested in tests/js/markdown.test.mjs. The DOM renderer below
// consumes it and only ever uses textContent (XSS-safe).

import { el, safeHref } from "./dom.js";

const isAlnum = (ch) => ch != null && /[A-Za-z0-9]/.test(ch);

// Inline link: [label](target). Label has no nested ']' or newline; target has
// no whitespace or ')'. The target is scheme-checked via safeHref at render.
const LINK = /\[([^\]\n]+)\]\(([^)\s]+)\)/g;

/**
 * Tokenize one inline string into segments. Pure (no DOM); returns
 *   [{ type: "text" | "code" | "em" | "strong" | "link", value, href? }]
 * Code spans take precedence (their contents are literal); then links; then
 * emphasis, which only triggers at word boundaries.
 */
export function tokenizeInline(text) {
  const src = String(text ?? "");
  const out = [];
  // Inline code spans first: their contents are literal.
  const parts = src.split(/(`[^`]+`)/g);
  for (const part of parts) {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      out.push({ type: "code", value: part.slice(1, -1) });
    } else if (part) {
      tokenizeLinks(part, out);
    }
  }
  return out;
}

// Split out [label](target) links, running emphasis on the text between them.
function tokenizeLinks(text, out) {
  let last = 0;
  let m;
  LINK.lastIndex = 0;
  while ((m = LINK.exec(text)) !== null) {
    if (m.index > last) tokenizeEmphasis(text.slice(last, m.index), out);
    out.push({ type: "link", value: m[1], href: m[2] });
    last = LINK.lastIndex;
  }
  if (last < text.length) tokenizeEmphasis(text.slice(last), out);
}

// Match **bold** or *italic*. We validate flanking manually (see below) so a
// purely greedy regex would over-match; instead we scan for `*` runs.
function tokenizeEmphasis(text, out) {
  let i = 0;
  const n = text.length;
  let buf = ""; // accumulates plain text between emphasis runs

  const flush = () => {
    if (buf) {
      out.push({ type: "text", value: buf });
      buf = "";
    }
  };

  while (i < n) {
    if (text[i] === "*") {
      const strong = text[i + 1] === "*";
      const open = strong ? "**" : "*";
      const openLen = open.length;
      const before = text[i - 1];
      // Left-flanking: opener not glued to an alphanumeric on its left, and the
      // char immediately after the opener starts non-whitespace content.
      const afterOpen = text[i + openLen];
      const leftFlank = !isAlnum(before) && afterOpen != null && !/\s/.test(afterOpen);
      if (leftFlank) {
        // Find a matching closer of the same length that is right-flanking.
        // Strong (**) closes only on a '**' run; italic (*) closes on a single
        // '*' that is not the start of a '**' run.
        const closeLen = strong ? 2 : 1;
        let j = i + openLen;
        let close = -1;
        while (j < n) {
          const here = strong ? text[j] === "*" && text[j + 1] === "*" : text[j] === "*";
          if (here) {
            const beforeClose = text[j - 1];
            const afterClose = text[j + closeLen];
            // Right-flanking: closer not glued to an alphanumeric on its right,
            // and the content char before the closer is non-whitespace.
            const rightFlank =
              !isAlnum(afterClose) && beforeClose != null && !/\s/.test(beforeClose);
            const content = text.slice(i + openLen, j);
            if (rightFlank && content.length && !content.includes("\n")) {
              close = j;
              break;
            }
          }
          j += 1;
        }
        if (close !== -1) {
          flush();
          const content = text.slice(i + openLen, close);
          out.push({ type: strong ? "strong" : "em", value: content });
          i = close + (strong ? 2 : 1);
          continue;
        }
      }
      // Not a valid emphasis run: emit the literal '*' and advance one char.
      buf += "*";
      i += 1;
      continue;
    }
    buf += text[i];
    i += 1;
  }
  flush();
}

function renderInline(parent, text) {
  for (const tok of tokenizeInline(text)) {
    if (tok.type === "code") {
      parent.append(el("code", { text: tok.value })); // safe sink
    } else if (tok.type === "strong") {
      parent.append(el("strong", { text: tok.value }));
    } else if (tok.type === "em") {
      parent.append(el("em", { text: tok.value }));
    } else if (tok.type === "link") {
      const href = safeHref(tok.href);
      if (href) {
        parent.append(
          el("a", { href, target: "_blank", rel: "noopener noreferrer", text: tok.value })
        );
      } else {
        // Unsafe scheme (e.g. javascript:): show the visible label, never the
        // raw markup — no markdown syntax leaks to the frontend.
        parent.append(document.createTextNode(tok.value));
      }
    } else {
      parent.append(document.createTextNode(tok.value));
    }
  }
  return parent;
}

// Render inline text that may contain soft line breaks, emitting <br> between
// source lines (used by paragraphs and blockquotes).
function renderInlineMultiline(parent, text) {
  const lines = String(text ?? "").split("\n");
  lines.forEach((line, i) => {
    if (i) parent.append(el("br"));
    renderInline(parent, line);
  });
}

// -- block parsing ----------------------------------------------------------

const RE_FENCE = /^\s*```/;
const RE_HEADING = /^(#{1,6})\s+(.*)$/;
const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const RE_QUOTE = /^\s*>\s?/;
const RE_LIST = /^\s*(?:[-*+]\s+|\d+\.\s+)/;
const RE_ORDERED = /^\s*\d+\.\s+/;
const RE_BLANK = /^\s*$/;

/** True when `line` opens a non-paragraph block (used to terminate paragraphs). */
function startsBlock(line) {
  return (
    RE_FENCE.test(line) ||
    RE_HEADING.test(line) ||
    RE_HR.test(line) ||
    RE_QUOTE.test(line) ||
    RE_LIST.test(line)
  );
}

/**
 * Parse markdown source into an ordered list of block descriptors. Pure (no
 * DOM). Returns blocks of shape:
 *   { type: "heading", level, text }
 *   { type: "code", text }
 *   { type: "list", ordered, items: [text, ...] }
 *   { type: "blockquote", text }     // inner lines joined with "\n"
 *   { type: "hr" }
 *   { type: "paragraph", text }      // source lines joined with "\n"
 */
export function parseBlocks(src) {
  const lines = String(src ?? "").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (RE_FENCE.test(line)) {
      const code = [];
      i += 1;
      while (i < lines.length && !RE_FENCE.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1; // consume the closing fence (no-op if EOF)
      blocks.push({ type: "code", text: code.join("\n") });
      continue;
    }

    if (RE_BLANK.test(line)) {
      i += 1;
      continue;
    }

    const h = RE_HEADING.exec(line);
    if (h) {
      blocks.push({ type: "heading", level: h[1].length, text: h[2].trim() });
      i += 1;
      continue;
    }

    if (RE_HR.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    if (RE_QUOTE.test(line)) {
      const q = [];
      while (i < lines.length && RE_QUOTE.test(lines[i])) {
        q.push(lines[i].replace(RE_QUOTE, ""));
        i += 1;
      }
      blocks.push({ type: "blockquote", text: q.join("\n") });
      continue;
    }

    if (RE_LIST.test(line)) {
      const ordered = RE_ORDERED.test(line);
      const items = [];
      // A switch between bullets and numbers ends the current list.
      while (i < lines.length && RE_LIST.test(lines[i]) && RE_ORDERED.test(lines[i]) === ordered) {
        items.push(lines[i].replace(RE_LIST, ""));
        i += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // Paragraph: gather until a blank line or the start of another block.
    const p = [];
    while (i < lines.length && !RE_BLANK.test(lines[i]) && !startsBlock(lines[i])) {
      p.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "paragraph", text: p.join("\n") });
  }
  return blocks;
}

/** Render `text` into `target` as formatted blocks, replacing its children. */
export function renderMarkdown(target, text) {
  target.replaceChildren();
  for (const block of parseBlocks(text)) {
    if (block.type === "heading") {
      target.append(renderInline(el("h" + block.level), block.text));
    } else if (block.type === "code") {
      const pre = el("pre");
      pre.append(el("code", { text: block.text })); // safe sink
      target.append(pre);
    } else if (block.type === "hr") {
      target.append(el("hr"));
    } else if (block.type === "list") {
      const list = el(block.ordered ? "ol" : "ul");
      for (const item of block.items) list.append(renderInline(el("li"), item));
      target.append(list);
    } else if (block.type === "blockquote") {
      const bq = el("blockquote");
      renderInlineMultiline(bq, block.text);
      target.append(bq);
    } else {
      const p = el("p");
      renderInlineMultiline(p, block.text);
      target.append(p);
    }
  }
  return target;
}
