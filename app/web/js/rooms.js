// rooms.js — room list/switcher + room settings (Task 6.4 / FR-R*).
//
// The sidebar list (renderRoomList) shows every room; clicking switches; a gear
// opens settings. Settings cover name / topic / default_reply_mode / archive
// (PATCH) plus membership management (add/remove personas — the roster that
// feeds @-mentions and routing). New-room creation prompts for a name.

import { el, clear } from "./dom.js";
import { openModal, closeModal, field } from "./modal.js";
import { api } from "./api.js";
import { toast } from "./errors.js";

const MODES = ["sequential", "parallel"];

// A text input + inline folder browser for a room's shared working_dir. The
// modal shell is single-instance, so the picker expands *inside* the current
// modal (a separate modal would destroy the form). Returns { wrap, getValue }.
function workingDirField(initialValue) {
  const input = el("input", {
    value: initialValue || "",
    placeholder: "shared folder all personas work in (optional)",
  });
  const panel = el("div", { class: "dir-picker hidden" });
  let cwd = null;
  let open = false;

  const navigate = async (path) => {
    try {
      render(await api.listDirs(path));
    } catch (e) {
      toast(e.kind, e.message);
    }
  };

  const row = (ico, label, onClick) =>
    el("div", { class: "dir-row", role: "button", tabindex: "0", onClick }, [
      el("span", { class: "dir-ico", text: ico }),
      el("span", { class: "grow", text: label }),
    ]);

  function render(listing) {
    cwd = listing.path;
    clear(panel);
    panel.append(el("div", { class: "dir-cwd", text: listing.path }));
    const list = el("div", { class: "dir-list" });
    if (listing.parent) list.append(row("↩", ".. (parent)", () => navigate(listing.parent)));
    for (const e of listing.entries) list.append(row("📁", e.name, () => navigate(e.path)));
    if (!listing.entries.length) list.append(el("div", { class: "hint", text: "No sub-folders here." }));
    panel.append(list);
    panel.append(
      el("div", { class: "dir-actions" }, [
        el("button", {
          class: "btn tiny primary",
          type: "button",
          text: "Use this folder",
          onClick: () => {
            input.value = cwd;
            toggle(false);
          },
        }),
      ])
    );
  }

  function toggle(next) {
    open = next === undefined ? !open : next;
    panel.classList.toggle("hidden", !open);
    if (open && cwd === null) navigate(input.value.trim() || null);
  }

  const browse = el("button", {
    class: "btn tiny",
    type: "button",
    text: "Browse…",
    onClick: () => toggle(),
  });

  const wrap = el("div", { class: "dir-field" }, [
    el("div", { class: "dir-input-row" }, [input, browse]),
    panel,
  ]);
  return { wrap, getValue: () => input.value.trim() || null };
}

export function renderRoomList(container, rooms, activeId, { onSelect, onSettings }) {
  clear(container);
  for (const r of rooms) {
    const item = el(
      "div",
      {
        class: "room-item" + (r.id === activeId ? " active" : "") + (r.archived ? " archived" : ""),
        onClick: () => onSelect(r),
      },
      [
        el("span", { class: "hash", text: "#" }),
        el("span", { class: "grow", text: r.name }),
        el("span", {
          class: "gear",
          text: "⚙",
          role: "button",
          tabindex: "0",
          "aria-label": `Room settings for ${r.name}`,
          onClick: (e) => {
            e.stopPropagation();
            onSettings(r);
          },
          onKeydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              onSettings(r);
            }
          },
        }),
      ]
    );
    container.append(item);
  }
}

export async function createRoom(onCreated) {
  const name = el("input", { placeholder: "room name" });
  const topic = el("input", { placeholder: "topic (optional)" });
  const mode = el("select");
  for (const m of MODES) mode.append(el("option", { value: m, text: m }));
  const workdir = workingDirField("");

  openModal({
    title: "New room",
    body: [
      field("Name", name).field,
      field("Topic", topic).field,
      field("Default reply mode", mode).field,
      field(
        "Working directory",
        workdir.wrap,
        "Shared folder this room's personas read/edit. Chat history is not stored here."
      ).field,
    ],
    footer: [
      el("button", {
        class: "btn primary",
        text: "Create",
        onClick: async () => {
          if (!name.value.trim()) return;
          try {
            const room = await api.createRoom({
              name: name.value.trim(),
              topic: topic.value.trim(),
              default_reply_mode: mode.value,
              working_dir: workdir.getValue(),
            });
            closeModal();
            onCreated?.(room);
          } catch (e) {
            toast(e.kind, e.message);
          }
        },
      }),
      el("button", { class: "btn", text: "Cancel", onClick: closeModal }),
    ],
  });
}

export async function openRoomSettings(room, { onChanged }) {
  let members = [];
  let allPersonas = [];
  try {
    [members, allPersonas] = await Promise.all([api.listMembers(room.id), api.listPersonas()]);
  } catch (e) {
    toast(e.kind, e.message);
    return;
  }
  const memberIds = new Set(members.map((m) => m.id));
  const candidates = allPersonas.filter((p) => !p.is_template && !memberIds.has(p.id));

  const name = el("input", { value: room.name });
  const topic = el("input", { value: room.topic || "" });
  const mode = el("select");
  for (const m of MODES) mode.append(el("option", { value: m, text: m }));
  mode.value = room.default_reply_mode || "sequential";
  const archived = el("input", { type: "checkbox" });
  if (room.archived) archived.checked = true;
  const delegation = el("input", { type: "checkbox" });
  if (room.delegation_enabled ?? true) delegation.checked = true;
  const workdir = workingDirField(room.working_dir || "");

  const reopen = async () => {
    const fresh = await api.getRoom(room.id);
    await openRoomSettings(fresh, { onChanged });
    onChanged?.();
  };

  const memberList = el("div", { class: "persona-list" });
  if (!members.length) memberList.append(el("div", { class: "hint", text: "No personas in this room yet." }));
  for (const m of members) {
    memberList.append(
      el("div", { class: "list-row" }, [
        el("span", { class: "dot", style: { background: m.color } }),
        el("div", { class: "grow" }, [el("div", { class: "name", text: `${m.name}  @${m.handle}` })]),
        el("button", {
          class: "btn tiny danger",
          text: "Remove",
          onClick: async () => {
            try {
              await api.removeMember(room.id, m.id);
              await reopen();
            } catch (e) {
              toast(e.kind, e.message);
            }
          },
        }),
      ])
    );
  }

  const addSelect = el("select");
  addSelect.append(el("option", { value: "", text: candidates.length ? "add persona…" : "no more personas" }));
  for (const c of candidates) addSelect.append(el("option", { value: c.id, text: `${c.name} @${c.handle}` }));
  addSelect.onchange = async () => {
    if (!addSelect.value) return;
    try {
      await api.addMember(room.id, addSelect.value);
      await reopen();
    } catch (e) {
      toast(e.kind, e.message);
    }
  };

  const saveRoom = async () => {
    try {
      await api.updateRoom(room.id, {
        name: name.value.trim(),
        topic: topic.value.trim(),
        default_reply_mode: mode.value,
        delegation_enabled: delegation.checked,
        working_dir: workdir.getValue(),
        archived: archived.checked,
      });
      onChanged?.();
      closeModal();
    } catch (e) {
      toast(e.kind, e.message);
    }
  };

  openModal({
    title: `Room · ${room.name}`,
    body: [
      el("div", { class: "field-row" }, [field("Name", name).field, field("Default reply mode", mode).field]),
      field("Topic", topic).field,
      field(
        "Working directory",
        workdir.wrap,
        "Shared folder this room's personas read/edit. Chat history is not stored here. " +
          "After changing it, reset a persona's session below so it restarts in the new folder."
      ).field,
      el("div", { class: "field" }, [
        el("label", { text: "Delegation" }),
        el("label", { class: "hint" }, [
          delegation,
          el("span", { text: "  let personas call each other by @handle (auto-adds them to the room)" }),
        ]),
      ]),
      el("div", { class: "field" }, [
        el("label", { text: "Archive" }),
        el("label", { class: "hint" }, [archived, el("span", { text: "  archive room" })]),
      ]),
      el("div", { class: "section-head", text: "Members" }),
      memberList,
      field("Add member", addSelect).field,
    ],
    footer: [
      el("button", {
        class: "btn danger",
        text: "Delete room",
        onClick: async () => {
          if (!confirm(`Delete room ${room.name}? This cannot be undone.`)) return;
          try {
            await api.deleteRoom(room.id);
            closeModal();
            onChanged?.({ deleted: room.id });
          } catch (e) {
            toast(e.kind, e.message);
          }
        },
      }),
      el("button", { class: "btn primary", text: "Save", onClick: saveRoom }),
      el("button", { class: "btn", text: "Close", onClick: closeModal }),
    ],
  });
}
