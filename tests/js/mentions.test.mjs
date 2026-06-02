// JS logic test for mention parsing — run with: node --test tests/js/
//
// Verifies the frontend mention parser (app/web/js/mentions.js) matches the
// backend's behavior (app/services/mentions.py): lowercased handles in order,
// `@everyone` recognized, emails NOT treated as mentions, and active-mention
// detection for autocomplete.

import { test } from "node:test";
import assert from "node:assert/strict";
import { findMentions, activeMention, mentionsHandle } from "../../app/web/js/mentions.js";

test("findMentions lowercases and preserves order", () => {
  assert.deepEqual(findMentions("hey @Arch and @Boss-Bot"), ["arch", "boss-bot"]);
});

test("findMentions recognizes @everyone", () => {
  assert.deepEqual(findMentions("ship it @everyone"), ["everyone"]);
});

test("findMentions ignores emails (mirrors backend lookbehind)", () => {
  assert.deepEqual(findMentions("mail kartik@example.com please"), []);
});

test("findMentions handles no mentions", () => {
  assert.deepEqual(findMentions("just a normal message"), []);
});

test("activeMention detects a token being typed at the caret", () => {
  const text = "hello @ar";
  const m = activeMention(text, text.length);
  assert.equal(m.query, "ar");
  assert.equal(m.start, 6); // index of '@'
});

test("activeMention returns null when caret is not in a mention", () => {
  assert.equal(activeMention("done @arch ", 11), null);
  assert.equal(activeMention("kartik@example.com", 18), null);
});

test("mentionsHandle resolves explicit and @everyone", () => {
  assert.equal(mentionsHandle("@arch go", "arch"), true);
  assert.equal(mentionsHandle("@everyone go", "arch"), true);
  assert.equal(mentionsHandle("@other go", "arch"), false);
});
