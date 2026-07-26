// persona_fields.js — option lists + merge logic for the persona editor pickers.
//
// DOM-free on purpose (imports nothing) so the merging logic is unit-testable in
// node and reusable. The editor turns `model`, `effort`, `allowed_tools`, and
// `mcp_servers` into pickers; the option lists come from three places, merged by
// the pure helpers below:
//   1. the live per-provider model list fetched from GET /models (backend-owned,
//      so it follows the real CLI adapters — e.g. a new `fable` alias appears
//      automatically); MODEL_SUGGESTIONS below is only an offline fallback,
//   2. values already in use across the operator's personas (so their own setup
//      shows up without re-typing),
//   3. whatever the persona being edited already has selected (never lose it).
// Models/efforts evolve, so the model field is a dropdown you can ALSO type into
// (a <datalist>), never a hard allowlist — see decision in this change.

// CLI-valid reasoning-effort levels (claude --effort / codex model_reasoning_effort).
// `max` is claude-only; codex rejects it fail-loud, which is acceptable.
export const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

// OFFLINE FALLBACK ONLY. The real source of truth is GET /models (backend-owned
// via each harness's `supported_models`); this list is used solely when that
// fetch is unavailable or omits a provider, so the editor still offers sane
// options. Kept in sync in spirit with the harness adapters (incl. `fable`).
export const MODEL_SUGGESTIONS = {
  claude: ["opus", "sonnet", "haiku", "fable"],
  codex: ["gpt-5.5-codex", "gpt-5.5"],
  mock: ["mock"],
};

// Common tool names to offer in the allowed-tools picker (claude-oriented). These
// are PRE-APPROVED for the persona: claude maps them to `--allowedTools` (so a
// headless run never has to prompt the operator, which it cannot do), and codex
// maps a WebSearch entry to its native `--search`. The list is just a convenience
// — custom entries are always allowed, including scoped commands like "Bash(git *)".
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
 * Model suggestions for a provider: the live per-provider list from the server
 * (GET /models) followed by any models already used by personas on that same
 * provider (deduped, server order kept). When the server map has no entry for
 * the provider (fetch failed, or provider not wired), falls back to the built-in
 * MODEL_SUGGESTIONS so the picker is never empty.
 *
 * @param provider          e.g. "claude"
 * @param modelsByProvider  { claude: [...], codex: [...] } from GET /models
 * @param personas          all personas, to surface models already in use
 */
export function modelSuggestionsFor(provider, modelsByProvider = {}, personas = []) {
  const fromServer = modelsByProvider && modelsByProvider[provider];
  const base = fromServer && fromServer.length ? fromServer : MODEL_SUGGESTIONS[provider] || [];
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

/**
 * Collapse a human label like "opus 4.8" to the CLI alias "opus" so a spaced
 * label never gets stored/sent as a model (the CLI rejects it). Mirrors
 * ClaudeHarness._normalize_model. A bare alias or full id passes through.
 */
export function normalizeModel(model) {
  const stripped = String(model ?? "").trim();
  const head = stripped.split(/\s+/)[0].toLowerCase();
  if (/\s/.test(stripped) && ["opus", "sonnet", "haiku"].includes(head)) {
    return head;
  }
  return stripped;
}
