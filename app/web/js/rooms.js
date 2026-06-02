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
          onClick: (e) => {
            e.stopPropagation();
            onSettings(r);
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

  openModal({
    title: "New room",
    body: [
      field("Name", name).field,
      field("Topic", topic).field,
      field("Default reply mode", mode).field,
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
