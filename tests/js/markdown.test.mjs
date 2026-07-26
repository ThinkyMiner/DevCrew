// JS logic test for the markdown tokenizer — run with: node --test tests/js/
//
// Verifies the inline emphasis rule in app/web/js/markdown.js does NOT mangle
// code-like text (intra-word `_`/`*`) while still rendering real emphasis and
// code spans. We test the PURE tokenizer (tokenizeInline) which returns a flat
// list of {type:'text'|'code'|'em'|'strong', value} segments — no DOM needed.
//
// Rule under test (CommonMark-ish flanking, simplified):
//   * `_` NEVER produces italics (intra-word `_` is common in identifiers).
//   * `*italic*` / `**bold**` only when the run is "flanked" at word
//     boundaries: the opening `*` is not preceded by an alphanumeric and the
//     closing `*` is not followed by an alphanumeric. This leaves `2*3*4`,
//     `a*b`, `do_a_thing`, `foo_bar_baz.py`, `path/to_file` as plain text.
//   * Code spans are split out first; their contents are literal.

import { test } from "node:test";
import assert from "node:assert/strict";
import { tokenizeInline, parseBlocks } from "../../app/web/js/markdown.js";

// Collapse tokens to a compact representation for assertions.
const types = (toks) => toks.map((t) => t.type);
const plain = (toks) => toks.every((t) => t.type === "text");
const text = (toks) => toks.map((t) => t.value).join("");

test("intra-word underscores stay plain (do_a_thing)", () => {
  const toks = tokenizeInline("call do_a_thing now");
  assert.ok(plain(toks), `expected all text, got ${JSON.stringify(types(toks))}`);
  assert.equal(text(toks), "call do_a_thing now");
});

test("filename with underscores stays plain (foo_bar_baz.py)", () => {
  const toks = tokenizeInline("see foo_bar_baz.py here");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "see foo_bar_baz.py here");
});

test("arithmetic with stars stays plain (2*3*4=24)", () => {
  const toks = tokenizeInline("2*3*4=24");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "2*3*4=24");
});

test("intra-word single star stays plain (a*b)", () => {
  const toks = tokenizeInline("a*b");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "a*b");
});

test("path with underscore stays plain (path/to_file)", () => {
  const toks = tokenizeInline("path/to_file");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "path/to_file");
});

test("standalone _underscores_ stay plain (no em from _)", () => {
  const toks = tokenizeInline("an _italic_ word");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "an _italic_ word");
});

test("real italic renders an em", () => {
  const toks = tokenizeInline("a *real italic* here");
  assert.deepEqual(types(toks), ["text", "em", "text"]);
  assert.equal(toks[1].value, "real italic");
});

test("bold renders a strong", () => {
  const toks = tokenizeInline("a **bold** here");
  assert.deepEqual(types(toks), ["text", "strong", "text"]);
  assert.equal(toks[1].value, "bold");
});

test("code span renders code and contents are literal", () => {
  const toks = tokenizeInline("use `code` now");
  assert.deepEqual(types(toks), ["text", "code", "text"]);
  assert.equal(toks[1].value, "code");
});

test("code span containing _x_ stays literal (no em inside backticks)", () => {
  const toks = tokenizeInline("`_x_`");
  assert.deepEqual(types(toks), ["code"]);
  assert.equal(toks[0].value, "_x_");
});

test("code span with *stars* stays literal", () => {
  const toks = tokenizeInline("`2*3*4`");
  assert.deepEqual(types(toks), ["code"]);
  assert.equal(toks[0].value, "2*3*4");
});

test("bold and italic can coexist", () => {
  const toks = tokenizeInline("**b** and *i*");
  assert.deepEqual(types(toks), ["strong", "text", "em"]);
  assert.equal(toks[0].value, "b");
  assert.equal(toks[2].value, "i");
});

// -- inline links ----------------------------------------------------------

test("link renders a link token with text + href", () => {
  const toks = tokenizeInline("see [docs](https://x.io/y) now");
  assert.deepEqual(types(toks), ["text", "link", "text"]);
  assert.equal(toks[1].value, "docs");
  assert.equal(toks[1].href, "https://x.io/y");
});

test("non-link brackets stay plain", () => {
  const toks = tokenizeInline("an array[0] index");
  assert.ok(plain(toks), JSON.stringify(types(toks)));
  assert.equal(text(toks), "an array[0] index");
});

test("link inside a code span stays literal", () => {
  const toks = tokenizeInline("`[x](y)`");
  assert.deepEqual(types(toks), ["code"]);
  assert.equal(toks[0].value, "[x](y)");
});

// -- block parsing ---------------------------------------------------------

const blockTypes = (bs) => bs.map((b) => b.type);

test("ATX headings parse to heading blocks with levels", () => {
  const bs = parseBlocks("# Title\n## Sub\n###### Deep");
  assert.deepEqual(blockTypes(bs), ["heading", "heading", "heading"]);
  assert.deepEqual(bs.map((b) => b.level), [1, 2, 6]);
  assert.equal(bs[0].text, "Title");
  assert.equal(bs[2].text, "Deep");
});

test("seven hashes is not a heading (paragraph)", () => {
  const bs = parseBlocks("####### nope");
  assert.deepEqual(blockTypes(bs), ["paragraph"]);
  assert.equal(bs[0].text, "####### nope");
});

test("unordered list groups consecutive items", () => {
  const bs = parseBlocks("- one\n- two\n* three");
  assert.deepEqual(blockTypes(bs), ["list"]);
  assert.equal(bs[0].ordered, false);
  assert.deepEqual(bs[0].items, ["one", "two", "three"]);
});

test("ordered list keeps order flag", () => {
  const bs = parseBlocks("1. first\n2. second");
  assert.deepEqual(blockTypes(bs), ["list"]);
  assert.equal(bs[0].ordered, true);
  assert.deepEqual(bs[0].items, ["first", "second"]);
});

test("blockquote groups consecutive quote lines", () => {
  const bs = parseBlocks("> a\n> b\nnormal");
  assert.deepEqual(blockTypes(bs), ["blockquote", "paragraph"]);
  assert.equal(bs[0].text, "a\nb");
});

test("--- on its own line is a horizontal rule", () => {
  const bs = parseBlocks("above\n\n---\n\nbelow");
  assert.deepEqual(blockTypes(bs), ["paragraph", "hr", "paragraph"]);
});

test("fenced code block is one literal block", () => {
  const bs = parseBlocks("text\n```\n# not a heading\n- not a list\n```\nafter");
  assert.deepEqual(blockTypes(bs), ["paragraph", "code", "paragraph"]);
  assert.equal(bs[1].text, "# not a heading\n- not a list");
});

test("blank lines split paragraphs", () => {
  const bs = parseBlocks("one\ntwo\n\nthree");
  assert.deepEqual(blockTypes(bs), ["paragraph", "paragraph"]);
  assert.equal(bs[0].text, "one\ntwo");
  assert.equal(bs[1].text, "three");
});
