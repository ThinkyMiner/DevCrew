// persona_fields.js — option lists + merge logic for the persona editor pickers.
//
// DOM-free on purpose (imports nothing) so the merging logic is unit-testable in
// node and reusable. The editor turns `model`, `effort`, `allowed_tools`, and
// `mcp_servers` into pickers; the option lists come from three places, merged by
// the pure helpers below:
//   1. a curated base list (what's commonly valid),
//   2. values already in use across the operator's personas (so their own setup
//      shows up without re-typing),
//   3. whatever the persona being edited already has selected (never lose it).
// Models/efforts evolve, so the model field is a dropdown you can ALSO type into
// (a <datalist>), never a hard allowlist — see decision in this change.

// CLI-valid reasoning-effort levels (claude --effort / codex model_reasoning_effort).
// `max` is claude-only; codex rejects it fail-loud, which is acceptable.
export const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

// Curated, provider-aware model suggestions. Not exhaustive and not a hard limit
// — the field accepts free text — just the common, known-good starting points.
export const MODEL_SUGGESTIONS = {
  claude: ["opus", "sonnet", "haiku"],
  codex: ["gpt-5.5", "gpt-5.5-codex"],
  mock: ["mock"],
};

// Common tool names to offer in the allowed-tools picker (claude-oriented; the
// field is not currently enforced by either CLI, so this is a convenience list
// and custom entries are always allowed).
export const TOOL_SUGGESTIONS = [
  "Read",
  "Edit",
  "Write",
  "Bash",
  "Glob",
  "Grep",
  "WebSearch",
  "WebFetch",
  "Task",
  "TodoWrite",
  "NotebookEdit",
];

/** Dedupe while preserving first-seen order. */
function dedupe(values) {
  const seen = new Set();
  const out = [];
  for (const v of values) {
    if (v == null || v === "") continue;
    if (!seen.has(v)) {
      seen.add(v);
      out.push(v);
    }
  }
  return out;
}

/**
 * Model suggestions for a provider: the curated base list followed by any models
 * already used by personas on that same provider (deduped, base order kept).
 */
export function modelSuggestionsFor(provider, personas = []) {
  const base = MODEL_SUGGESTIONS[provider] || [];
  const used = personas.filter((p) => p && p.provider === provider).map((p) => p.model);
  return dedupe([...base, ...used]);
}

/** Union of every persona's configured MCP servers (deduped). */
export function mcpSuggestionsFrom(personas = []) {
  return dedupe(personas.flatMap((p) => (p && Array.isArray(p.mcp_servers) ? p.mcp_servers : [])));
}

/**
 * Suggestion list guaranteed to contain the current selection: the suggestions
 * first, then any selected value not already present (so a custom/legacy value
 * still renders as a checked option instead of silently disappearing).
 */
export function withSelected(suggestions, selected = []) {
  return dedupe([...suggestions, ...selected]);
}
