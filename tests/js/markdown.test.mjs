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
import { tokenizeInline } from "../../app/web/js/markdown.js";

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
