// markdown.js — minimal, SAFE markdown rendering.
//
// We render a SMALL subset (fenced/inline code, bold, italic) of UNTRUSTED model
// output. The cardinal rule: we NEVER assign untrusted text to innerHTML. The
// renderer tokenizes the raw string and appends only DOM text nodes + a fixed
// set of elements (<pre>/<code>/<strong>/<em>) whose text is set via
// textContent. Because content only ever reaches the DOM as a text node, markup
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

import { el } from "./dom.js";

// Split on fenced code blocks first; render each non-fence span inline.
const FENCE = /```([\s\S]*?)```/g;

const isAlnum = (ch) => ch != null && /[A-Za-z0-9]/.test(ch);

/**
 * Tokenize one inline string into segments. Pure (no DOM); returns
 *   [{ type: "text" | "code" | "em" | "strong", value }]
 * Code spans take precedence; emphasis only triggers at word boundaries.
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
      tokenizeEmphasis(part, out);
    }
  }
  return out;
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
    } else {
      parent.append(document.createTextNode(tok.value));
    }
  }
}

/** Render `text` into `target`, replacing its children. Returns target. */
export function renderMarkdown(target, text) {
  target.replaceChildren();
  const src = String(text ?? "");
  let last = 0;
  let m;
  FENCE.lastIndex = 0;
  while ((m = FENCE.exec(src)) !== null) {
    if (m.index > last) renderInline(target, src.slice(last, m.index));
    const code = m[1].replace(/^\n/, "");
    const pre = el("pre");
    pre.append(el("code", { text: code })); // safe sink
    target.append(pre);
    last = FENCE.lastIndex;
  }
  if (last < src.length) renderInline(target, src.slice(last));
  return target;
}
