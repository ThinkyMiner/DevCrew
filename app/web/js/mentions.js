// mentions.js — `@handle` parsing, mirroring app/services/mentions.py.
//
// Single source of truth on the FRONTEND for mention detection (autocomplete +
// "is this message directed at me?" affordances). The regex deliberately mirrors
// the backend `_MENTION_RE = (?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)` so the UI's
// idea of a mention matches what routing.py will actually resolve. Pure
// functions, no DOM — unit-tested via `node --test` (tests/js/mentions.test.mjs).

// JS supports lookbehind in all evergreen browsers + Node 16+.
const MENTION_RE = /(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)/g;

/** Lowercased handles in order of appearance (duplicates preserved). */
export function findMentions(text) {
  const out = [];
  for (const m of String(text).matchAll(MENTION_RE)) out.push(m[1].toLowerCase());
  return out;
}

/**
 * If the caret sits inside a `@partial` token being typed, return
 * { start, query } so the composer can show an autocomplete popup; else null.
 */
export function activeMention(text, caret) {
  const upTo = text.slice(0, caret);
  const m = /(?:^|[^A-Za-z0-9_])@([A-Za-z0-9_-]*)$/.exec(upTo);
  if (!m) return null;
  const start = caret - m[1].length - 1; // index of the '@'
  return { start, query: m[1].toLowerCase() };
}

/** True if `text` directs at `handle` (explicitly or via @everyone). */
export function mentionsHandle(text, handle) {
  const found = findMentions(text);
  return found.includes("everyone") || found.includes(String(handle).toLowerCase());
}
