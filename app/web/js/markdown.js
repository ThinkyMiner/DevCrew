// markdown.js — minimal, SAFE markdown rendering.
//
// We render a SMALL subset (fenced/inline code, bold, italic) of UNTRUSTED model
// output. The cardinal rule: we NEVER assign untrusted text to innerHTML. The
// renderer tokenizes the raw string and appends only DOM text nodes + a fixed
// set of elements (<pre>/<code>/<strong>/<em>) whose text is set via
// textContent. Because content only ever reaches the DOM as a text node, markup
// in the source ("<img onerror=...>") is displayed literally and can never
// execute. Anything we don't recognize falls through as plain text.

import { el } from "./dom.js";

// Split on fenced code blocks first; render each non-fence span inline.
const FENCE = /```([\s\S]*?)```/g;

function renderInline(parent, text) {
  // Inline code spans take precedence; the rest gets bold/italic.
  const parts = text.split(/(`[^`]+`)/g);
  for (const part of parts) {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      parent.append(el("code", { text: part.slice(1, -1) })); // safe sink
    } else {
      renderEmphasis(parent, part);
    }
  }
}

function renderEmphasis(parent, text) {
  // **bold** then *italic* / _italic_. Tokens carry their delimiters; we emit
  // styled elements with textContent only.
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("**")) {
      parent.append(el("strong", { text: tok.slice(2, -2) }));
    } else {
      parent.append(el("em", { text: tok.slice(1, -1) }));
    }
    last = re.lastIndex;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
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
