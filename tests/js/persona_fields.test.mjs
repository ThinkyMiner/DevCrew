// Logic test for the persona-editor field helpers — run: node --test tests/js/
//
// The model/effort/tools/mcp pickers derive their option lists from a DOM-free
// module so the merging logic (base suggestions ∪ what's already in use ∪ the
// current selection) is testable without a browser. We assert the pure helpers,
// not the DOM wiring (that's covered live via Playwright).

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EFFORT_LEVELS,
  MODEL_SUGGESTIONS,
  modelSuggestionsFor,
  mcpSuggestionsFrom,
  withSelected,
} from "../../app/web/js/persona_fields.js";

test("effort levels cover the CLI-valid set", () => {
  for (const level of ["low", "medium", "high", "xhigh", "max"]) {
    assert.ok(EFFORT_LEVELS.includes(level), `missing effort level ${level}`);
  }
});

test("base model suggestions exist per provider", () => {
  for (const provider of ["claude", "codex", "mock"]) {
    assert.ok(Array.isArray(MODEL_SUGGESTIONS[provider]) && MODEL_SUGGESTIONS[provider].length);
  }
});

test("modelSuggestionsFor unions base list with models already used by that provider", () => {
  const personas = [
    { provider: "claude", model: "opus 4.8" }, // custom-ish, not in base
    { provider: "claude", model: "sonnet" }, // already in base → no dup
    { provider: "codex", model: "gpt-9-future" }, // different provider → excluded
  ];
  const got = modelSuggestionsFor("claude", personas);
  // base claude suggestions come first, in order
  assert.deepEqual(got.slice(0, MODEL_SUGGESTIONS.claude.length), MODEL_SUGGESTIONS.claude);
  // the custom claude model is appended
  assert.ok(got.includes("opus 4.8"));
  // no duplicates
  assert.equal(new Set(got).size, got.length);
  // other-provider models are not pulled in
  assert.ok(!got.includes("gpt-9-future"));
});

test("mcpSuggestionsFrom unions all personas' mcp servers, deduped", () => {
  const personas = [
    { mcp_servers: ["fs", "git"] },
    { mcp_servers: ["git", "playwright"] },
    { mcp_servers: [] },
    {},
  ];
  const got = mcpSuggestionsFrom(personas);
  assert.deepEqual([...got].sort(), ["fs", "git", "playwright"]);
});

test("withSelected appends already-selected values not in the suggestion list", () => {
  const got = withSelected(["Read", "Edit"], ["Edit", "CustomTool"]);
  assert.deepEqual(got, ["Read", "Edit", "CustomTool"]); // suggestions first, extras after, no dup
});
