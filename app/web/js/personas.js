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

const PROVIDERS = ["claude", "codex", "mock"];
const PERMS = ["read-only", "ask", "auto"];

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
          el("div", { class: "sub", text: `${p.provider} · ${p.model}${p.effort ? " · " + p.effort : ""}` }),
        ]),
        el("button", { class: "btn tiny", text: "Edit", onClick: () => openPersonaEditor({ persona: p, onSaved: refresh }) }),
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
      el("button", { class: "btn primary", text: "New persona", onClick: () => openPersonaEditor({ onSaved: refresh }) }),
      el("button", { class: "btn", text: "Close", onClick: closeModal }),
    ],
  });
}

export function openPersonaEditor({ persona, onSaved }) {
  const p = persona || {};
  const f = {};
  f.name = el("input", { value: p.name || "" });
  f.handle = el("input", { value: p.handle || "", placeholder: "lowercase, [a-z0-9_-]" });
  f.color = el("input", { type: "color", value: p.color || "#6aa0ff" });
  f.provider = el("select");
  for (const pr of PROVIDERS) f.provider.append(el("option", { value: pr, text: pr }));
  f.provider.value = p.provider || "claude";
  f.model = el("input", { value: p.model || "" });
  f.effort = el("input", { value: p.effort || "", placeholder: "e.g. high (optional)" });
  f.system_prompt = el("textarea", { value: p.system_prompt || "" });
  f.mcp_servers = el("input", { value: (p.mcp_servers || []).join(", "), placeholder: "comma-separated" });
  f.allowed_tools = el("input", { value: (p.allowed_tools || []).join(", "), placeholder: "comma-separated" });
  f.working_dir = el("input", { value: p.working_dir || "", placeholder: "optional path" });
  f.permission_mode = el("select");
  for (const pm of PERMS) f.permission_mode.append(el("option", { value: pm, text: pm }));
  f.permission_mode.value = p.permission_mode || "read-only";
  f.is_template = el("input", { type: "checkbox" });
  if (p.is_template) f.is_template.checked = true;

  const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);

  const save = async () => {
    const payload = {
      name: f.name.value.trim(),
      handle: f.handle.value.trim(),
      color: f.color.value,
      provider: f.provider.value,
      model: f.model.value.trim(),
      effort: f.effort.value.trim() || null,
      system_prompt: f.system_prompt.value,
      mcp_servers: list(f.mcp_servers.value),
      allowed_tools: list(f.allowed_tools.value),
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

  const body = [
    el("div", { class: "field-row" }, [
      field("Name", f.name).field,
      field("Handle", f.handle).field,
    ]),
    el("div", { class: "field-row" }, [
      field("Color", f.color).field,
      field("Provider", f.provider).field,
    ]),
    el("div", { class: "field-row" }, [
      field("Model", f.model).field,
      field("Effort", f.effort).field,
    ]),
    field("System prompt", f.system_prompt).field,
    el("div", { class: "field-row" }, [
      field("MCP servers", f.mcp_servers).field,
      field("Allowed tools", f.allowed_tools).field,
    ]),
    el("div", { class: "field-row" }, [
      field("Working dir", f.working_dir).field,
      field("Permission mode", f.permission_mode).field,
    ]),
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
