// personas.js — persona manager + editor (Task 6.4 / FR-P1..P3).
//
// A modal listing all personas (and templates) with create / edit / duplicate /
// delete, and a "from template" action. The editor form covers EVERY FR-P2
// field: name, handle, color, provider, model, effort, system_prompt,
// mcp_servers, allowed_tools, working_dir, permission_mode (+ is_template so a
// persona can be saved as a reusable template, FR-P3).
//
// All inputs are real form controls; list rows render names via textContent.

import { el, truncate } from "./dom.js";
import { openModal, closeModal, field } from "./modal.js";
import { api } from "./api.js";
import { toast } from "./errors.js";
import { PERSONA_PROMPT_BOILERPLATE } from "./persona_templates.js";
import {
  EFFORT_LEVELS,
  TOOL_SUGGESTIONS,
  modelSuggestionsFor,
  mcpSuggestionsFrom,
  withSelected,
  normalizeModel,
} from "./persona_fields.js";

const PROVIDERS = ["claude", "codex", "mock"];
const PERMS = ["read-only", "ask", "auto"];

// A compact multi-select: a checkbox per suggestion plus an "add" row for custom
// values. Returns the wrapper element and a getValues() collector. Option labels
// are set via textContent (XSS-safe, see dom.js). Used for allowed-tools and
// MCP-servers, which the operator chose to keep as friendly pickers.
function multiPick(suggestions, selected) {
  const chosen = new Set(selected);
  const grid = el("div", { class: "pick-grid" });
  const addOption = (value, checked) => {
    const cb = el("input", { type: "checkbox", value });
    if (checked) cb.checked = true;
    grid.append(el("label", { class: "pick-opt" }, [cb, el("span", { text: value })]));
    return cb;
  };
  for (const o of withSelected(suggestions, selected)) addOption(o, chosen.has(o));

  const custom = el("input", { class: "pick-add-input", placeholder: "add custom…" });
  const add = () => {
    const v = custom.value.trim();
    if (!v) return;
    const existing = [...grid.querySelectorAll('input[type="checkbox"]')].find((c) => c.value === v);
    if (existing) existing.checked = true;
    else addOption(v, true);
    custom.value = "";
    custom.focus();
  };
  custom.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  });
  const addRow = el("div", { class: "pick-add" }, [
    custom,
    el("button", { class: "btn tiny", type: "button", text: "Add", onClick: add }),
  ]);

  const wrap = el("div", { class: "pick" }, [grid, addRow]);
  const getValues = () =>
    [...grid.querySelectorAll('input[type="checkbox"]')].filter((c) => c.checked).map((c) => c.value);
  return { wrap, getValues };
}

export async function openPersonaManager({ onChange } = {}) {
  let personas = [];
  let templates = [];
  try {
    [personas, templates] = await Promise.all([api.listPersonas(), api.listTemplates()]);
  } catch (e) {
    toast(e.kind, e.message);
    return;
  }
  const refresh = async () => {
    await openPersonaManager({ onChange });
    onChange?.();
  };

  const personaList = el("div", { class: "persona-list" });
  const regular = personas.filter((p) => !p.is_template);
  if (!regular.length) {
    personaList.append(el("div", { class: "hint", text: "No personas yet." }));
  }
  for (const p of regular) {
    personaList.append(
      el("div", { class: "list-row" }, [
        el("span", { class: "dot", style: { background: p.color } }),
        el("div", { class: "grow" }, [
          el("div", { class: "name", text: `${p.name}  @${p.handle}` }),
          el("div", {
            class: "sub",
            text: `${p.job ? p.job + " · " : ""}${p.provider} · ${p.model}${p.effort ? " · " + p.effort : ""}`,
          }),
        ]),
        el("button", { class: "btn tiny", text: "Edit", onClick: () => openPersonaEditor({ persona: p, onSaved: refresh, allPersonas: personas }) }),
        el("button", {
          class: "btn tiny",
          text: "Duplicate",
          onClick: async () => {
            try {
              await api.duplicatePersona(p.id);
              await refresh();
            } catch (e) {
              toast(e.kind, e.message);
            }
          },
        }),
        el("button", {
          class: "btn tiny danger",
          text: "Delete",
          onClick: async () => {
            if (!confirm(`Delete persona ${p.name}?`)) return;
            try {
              await api.deletePersona(p.id);
              await refresh();
            } catch (e) {
              toast(e.kind, e.message);
            }
          },
        }),
      ])
    );
  }

  const templateList = el("div", { class: "template-list" });
  if (!templates.length) {
    templateList.append(el("div", { class: "hint", text: "No templates yet — tick “save as template” in the editor." }));
  }
  for (const t of templates) {
    templateList.append(
      el("div", { class: "list-row" }, [
        el("span", { class: "dot", style: { background: t.color } }),
        el("div", { class: "grow" }, [
          el("div", { class: "name", text: t.name }),
          el("div", { class: "sub", text: `${t.provider} · ${t.model}` }),
        ]),
        el("button", {
          class: "btn tiny",
          text: "New from template",
          onClick: async () => {
            try {
              await api.personaFromTemplate(t.id);
              await refresh();
            } catch (e) {
              toast(e.kind, e.message);
            }
          },
        }),
      ])
    );
  }

  openModal({
    title: "Personas",
    body: [
      personaList,
      el("div", { class: "section-head", text: "Templates" }),
      templateList,
    ],
    footer: [
      el("button", { class: "btn primary", text: "New persona", onClick: () => openPersonaEditor({ onSaved: refresh, allPersonas: personas }) }),
      el("button", { class: "btn", text: "Close", onClick: closeModal }),
    ],
  });
}

export function openPersonaEditor({ persona, onSaved, allPersonas = [] }) {
  const p = persona || {};
  const isNew = !p.id;
  const f = {};
  f.name = el("input", { value: p.name || "" });
  f.handle = el("input", { value: p.handle || "", placeholder: "lowercase, [a-z0-9_-]" });
  f.job = el("input", { value: p.job || "", placeholder: "e.g. System architect" });
  f.color = el("input", { type: "color", value: p.color || "#6aa0ff" });

  f.provider = el("select");
  for (const pr of PROVIDERS) f.provider.append(el("option", { value: pr, text: pr }));
  f.provider.value = p.provider || "claude";

  // Model: a dropdown you can ALSO type into (datalist), provider-aware — models
  // change over time, so we suggest but never lock you out. Re-derived when the
  // provider changes so the suggestions follow the chosen backend.
  const modelList = el("datalist", { id: "persona-model-suggest" });
  const fillModelList = () => {
    modelList.replaceChildren();
    for (const m of modelSuggestionsFor(f.provider.value, allPersonas)) {
      modelList.append(el("option", { value: m }));
    }
  };
  fillModelList();
  f.model = el("input", {
    value: p.model || "",
    list: "persona-model-suggest",
    placeholder: "pick or type a model",
  });
  f.provider.addEventListener("change", fillModelList);

  // Effort: dropdown of the CLI-valid levels, a blank default, and any current
  // custom value (so editing never silently drops it).
  f.effort = el("select");
  f.effort.append(el("option", { value: "", text: "— default —" }));
  for (const lvl of withSelected(EFFORT_LEVELS, p.effort ? [p.effort] : [])) {
    f.effort.append(el("option", { value: lvl, text: lvl }));
  }
  f.effort.value = p.effort || "";

  // New personas start from the layered boilerplate scaffold (the operator edits
  // the <blanks>); editing an existing persona keeps its own prompt untouched.
  f.system_prompt = el("textarea", {
    value: p.system_prompt || (isNew ? PERSONA_PROMPT_BOILERPLATE : ""),
    rows: 16,
  });

  const toolPick = multiPick(TOOL_SUGGESTIONS, p.allowed_tools || []);
  const mcpPick = multiPick(mcpSuggestionsFrom(allPersonas), p.mcp_servers || []);

  f.working_dir = el("input", { value: p.working_dir || "", placeholder: "optional path" });
  f.permission_mode = el("select");
  for (const pm of PERMS) f.permission_mode.append(el("option", { value: pm, text: pm }));
  f.permission_mode.value = p.permission_mode || "read-only";
  f.is_template = el("input", { type: "checkbox" });
  if (p.is_template) f.is_template.checked = true;

  const save = async () => {
    const payload = {
      name: f.name.value.trim(),
      handle: f.handle.value.trim(),
      job: f.job.value.trim(),
      color: f.color.value,
      provider: f.provider.value,
      model: normalizeModel(f.model.value),
      effort: f.effort.value.trim() || null,
      system_prompt: f.system_prompt.value,
      mcp_servers: mcpPick.getValues(),
      allowed_tools: toolPick.getValues(),
      working_dir: f.working_dir.value.trim() || null,
      permission_mode: f.permission_mode.value,
      is_template: f.is_template.checked,
    };
    try {
      if (p.id) await api.updatePersona(p.id, payload);
      else await api.createPersona(payload);
      onSaved?.();
    } catch (e) {
      toast(e.kind, e.message);
    }
  };

  const notEnforced = "Tick to select; add your own. (Not yet sent to the CLI — stored only.)";
  const body = [
    el("div", { class: "field-row" }, [
      field("Name", f.name).field,
      field("Handle", f.handle).field,
    ]),
    el("div", { class: "field-row" }, [
      field("Job", f.job, "Short role label shown next to the name in chat.").field,
      field("Color", f.color).field,
    ]),
    el("div", { class: "field-row" }, [
      field("Provider", f.provider).field,
      field("Model", f.model).field,
    ]),
    modelList,
    el("div", { class: "field-row" }, [
      field("Effort", f.effort).field,
      field("Permission mode", f.permission_mode).field,
    ]),
    field(
      "System prompt",
      f.system_prompt,
      isNew ? "Edit this scaffold — fill the <blanks> and cut what you don't need." : undefined
    ).field,
    field("MCP servers", mcpPick.wrap, notEnforced).field,
    field("Allowed tools", toolPick.wrap, notEnforced).field,
    field("Working dir", f.working_dir).field,
    el("div", { class: "field" }, [
      el("label", { text: "Template" }),
      el("label", { class: "hint" }, [f.is_template, el("span", { text: "  save as reusable template (FR-P3)" })]),
    ]),
  ];

  openModal({
    title: p.id ? `Edit · ${truncate(p.name, 30)}` : "New persona",
    body,
    footer: [
      el("button", { class: "btn primary", text: "Save", onClick: save }),
      el("button", { class: "btn", text: "Cancel", onClick: closeModal }),
    ],
  });
}

// -- authors (FR-A1..A3) ----------------------------------------------------

export async function openAuthorManager({ onChange } = {}) {
  let authors = [];
  try {
    authors = await api.listAuthors();
  } catch (e) {
    toast(e.kind, e.message);
    return;
  }
  const refresh = async () => {
    await openAuthorManager({ onChange });
    onChange?.();
  };

  const list = el("div", { class: "persona-list" });
  for (const a of authors) {
    list.append(
      el("div", { class: "list-row" }, [
        el("span", { class: "dot", style: { background: a.color } }),
        el("div", { class: "grow" }, [
          el("div", { class: "name", text: a.name }),
          el("div", {
            class: "sub",
            text: a.weight_enabled && (a.weight_note || "").trim()
              ? "weighted · " + truncate(a.weight_note, 60)
              : "no weighting",
          }),
        ]),
        el("button", { class: "btn tiny", text: "Edit", onClick: () => openAuthorEditor({ author: a, onSaved: refresh }) }),
        el("button", {
          class: "btn tiny danger",
          text: "Delete",
          onClick: async () => {
            if (!confirm(`Delete author ${a.name}?`)) return;
            try {
              await api.deleteAuthor(a.id);
              await refresh();
            } catch (e) {
              toast(e.kind, e.message);
            }
          },
        }),
      ])
    );
  }

  openModal({
    title: "Authors",
    body: [
      el("div", { class: "hint", text: "Me + Boss ship seeded. The Boss weight note is prepended to weighted posts (FR-A3)." }),
      list,
    ],
    footer: [
      el("button", { class: "btn primary", text: "New author", onClick: () => openAuthorEditor({ onSaved: refresh }) }),
      el("button", { class: "btn", text: "Close", onClick: closeModal }),
    ],
  });
}

export function openAuthorEditor({ author, onSaved }) {
  const a = author || {};
  const name = el("input", { value: a.name || "" });
  const color = el("input", { type: "color", value: a.color || "#9ee37d" });
  const weightNote = el("textarea", { value: a.weight_note || "", placeholder: "e.g. Leadership input — weight heavily." });
  const weightEnabled = el("input", { type: "checkbox" });
  if (a.weight_enabled ?? true) weightEnabled.checked = true;

  const save = async () => {
    const payload = {
      name: name.value.trim(),
      color: color.value,
      weight_note: weightNote.value,
      weight_enabled: weightEnabled.checked,
    };
    try {
      if (a.id) await api.updateAuthor(a.id, payload);
      else await api.createAuthor(payload);
      onSaved?.();
    } catch (e) {
      toast(e.kind, e.message);
    }
  };

  openModal({
    title: a.id ? `Edit author · ${truncate(a.name, 24)}` : "New author",
    body: [
      el("div", { class: "field-row" }, [field("Name", name).field, field("Color", color).field]),
      field("Weight note", weightNote, "Prepended to this author's posts when weighting is on.").field,
      el("div", { class: "field" }, [
        el("label", { text: "Weighting" }),
        el("label", { class: "hint" }, [weightEnabled, el("span", { text: "  enable weighting (FR-A2/A3)" })]),
      ]),
    ],
    footer: [
      el("button", { class: "btn primary", text: "Save", onClick: save }),
      el("button", { class: "btn", text: "Cancel", onClick: closeModal }),
    ],
  });
}
