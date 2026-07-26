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
  normalizeModel,
} from "../../app/web/js/persona_fields.js";

test("effort levels cover the CLI-valid set", () => {
  for (const level of ["low", "medium", "high", "xhigh", "max"]) {
    assert.ok(EFFORT_LEVELS.includes(level), `missing effort level ${level}`);
  }
});

test("built-in fallback model suggestions exist per provider (incl. fable for claude)", () => {
  for (const provider of ["claude", "codex", "mock"]) {
    assert.ok(Array.isArray(MODEL_SUGGESTIONS[provider]) && MODEL_SUGGESTIONS[provider].length);
  }
  // fable is a current claude alias — the offline fallback must not omit it.
  assert.ok(MODEL_SUGGESTIONS.claude.includes("fable"));
});

test("modelSuggestionsFor uses the dynamic per-provider list from the server", () => {
  const modelsByProvider = {
    claude: ["opus", "sonnet", "haiku", "fable"],
    codex: ["gpt-5.5"],
  };
  const personas = [
    { provider: "claude", model: "opus 4.8" }, // custom-ish, not in the server list
    { provider: "claude", model: "sonnet" }, // already listed → no dup
    { provider: "codex", model: "gpt-9-future" }, // different provider → excluded
  ];
  const got = modelSuggestionsFor("claude", modelsByProvider, personas);
  // the server's claude list comes first, in order
  assert.deepEqual(got.slice(0, 4), ["opus", "sonnet", "haiku", "fable"]);
  // the custom claude model is appended
  assert.ok(got.includes("opus 4.8"));
  // no duplicates
  assert.equal(new Set(got).size, got.length);
  // other-provider models are not pulled in
  assert.ok(!got.includes("gpt-9-future"));
});

test("modelSuggestionsFor falls back to built-ins when the server map lacks the provider", () => {
  const got = modelSuggestionsFor("claude", {}, []);
  assert.deepEqual(got, MODEL_SUGGESTIONS.claude);
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

test("normalizeModel collapses an alias+version label to the bare alias", () => {
  assert.equal(normalizeModel("opus 4.8"), "opus");
  assert.equal(normalizeModel("Opus 4.8 (1M context)"), "opus");
  assert.equal(normalizeModel("sonnet 4.6"), "sonnet");
  assert.equal(normalizeModel("  haiku   4.5 "), "haiku");
});

test("normalizeModel leaves valid model strings unchanged", () => {
  for (const valid of ["opus", "sonnet", "haiku", "claude-opus-4-8", "gpt-5.5", ""]) {
    assert.equal(normalizeModel(valid), valid);
  }
});
