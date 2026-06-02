// main.js — application bootstrap (Task 6.1 / FR-E1 health banner).
//
// Wires the whole UI:
//   1. GET /health -> banner if a CLI is missing/unauthenticated (FR-E1).
//   2. Load authors + rooms; pick the first non-archived room.
//   3. Per room: load members + messages, render the transcript, build the
//      composer, open a RoomSocket.
//   4. WS frames drive the transcript (optimistic stream) and, on
//      turn_complete, REFETCH messages to reconcile canonical ids/run_ids
//      (canonical-id contract, app/api/ws.py).

import { api } from "./api.js";
import { el, clear } from "./dom.js";
import { RoomSocket } from "./ws.js";
import { Transcript } from "./transcript.js";
import { Composer } from "./composer.js";
import { renderRoomList, createRoom, openRoomSettings } from "./rooms.js";
import { openPersonaManager, openAuthorManager } from "./personas.js";
import { openConsole } from "./console.js";
import { toast } from "./errors.js";

const state = {
  authors: [],
  authorsById: new Map(),
  personasById: new Map(), // all personas seen, for attribution
  rooms: [],
  activeRoom: null,
  members: [],
  socket: null,
  transcript: null,
  composer: null,
};

const $ = (id) => document.getElementById(id);

// -- health banner (FR-E1) --------------------------------------------------

async function loadHealth() {
  const banner = $("health-banner");
  try {
    const h = await api.health();
    if (h.ok) {
      banner.className = "health-banner";
      return;
    }
    const msgs = (h.messages || []).join("  ·  ");
    banner.textContent = "⚠ Backend not fully ready — " + msgs;
    banner.className = "health-banner show";
  } catch (e) {
    banner.textContent = "⚠ Health check failed: " + e.message;
    banner.className = "health-banner show";
  }
}

// -- attribution indexes ----------------------------------------------------

async function loadAuthors() {
  state.authors = await api.listAuthors();
  state.authorsById = new Map(state.authors.map((a) => [a.id, a]));
}

function indexPersonas(personas) {
  for (const p of personas) state.personasById.set(p.id, p);
}

// -- rooms -------------------------------------------------------------------

async function loadRooms() {
  state.rooms = await api.listRooms();
  renderRoomList($("room-list"), state.rooms, state.activeRoom?.id, {
    onSelect: (r) => selectRoom(r.id),
    onSettings: (r) =>
      openRoomSettings(r, {
        onChanged: async (res) => {
          await loadRooms();
          if (res && res.deleted && res.deleted === state.activeRoom?.id) {
            const next = state.rooms.find((x) => !x.archived) || state.rooms[0];
            if (next) selectRoom(next.id);
          } else if (state.activeRoom) {
            selectRoom(state.activeRoom.id);
          }
        },
      }),
  });
}

async function selectRoom(roomId) {
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
  state.activeRoom = await api.getRoom(roomId);
  state.members = await api.listMembers(roomId);
  indexPersonas(state.members);
  // also index all personas so historical authors render correctly
  try {
    indexPersonas(await api.listPersonas());
  } catch {
    /* non-fatal */
  }

  renderRoomList($("room-list"), state.rooms, roomId, {
    onSelect: (r) => selectRoom(r.id),
    onSettings: (r) =>
      openRoomSettings(r, {
        onChanged: async () => {
          await loadRooms();
          selectRoom(roomId);
        },
      }),
  });

  renderHeader();
  buildTranscript();
  buildComposer();
  await refreshMessages();
  openSocket(roomId);
}

function renderHeader() {
  const h = $("room-header");
  clear(h);
  const r = state.activeRoom;
  h.append(
    el("span", { class: "title", text: "#" + r.name }),
    r.topic ? el("span", { class: "topic", text: r.topic }) : null,
    el("span", { class: "spacer" })
  );
  const chips = el("div", { class: "member-chips" });
  for (const m of state.members) {
    chips.append(
      el(
        "div",
        {
          class: "member-chip",
          title: "Open session console",
          onClick: () =>
            openConsole({
              persona: m,
              roomId: r.id,
              socket: state.socket,
              onReset: () => toast("Session", `@${m.handle} session reset`, 3000),
            }),
        },
        [
          el("span", { class: "dot", style: { background: m.color }, dataset: { p: m.id } }),
          el("span", { text: "@" + m.handle }),
        ]
      )
    );
  }
  h.append(chips);
}

// -- transcript + composer ---------------------------------------------------

function buildTranscript() {
  state.transcript = new Transcript($("transcript"), {
    authorsById: state.authorsById,
    personasById: state.personasById,
    onQuote: (msg) => state.composer?.addQuote(msg, quoteLabel(msg)),
  });
}

function quoteLabel(msg) {
  const a =
    msg.author_kind === "persona"
      ? state.personasById.get(msg.author_ref)
      : state.authorsById.get(msg.author_ref);
  const who = a ? a.name : msg.author_kind;
  return `${who}: ${msg.content}`;
}

function buildComposer() {
  state.composer = new Composer($("composer"), {
    authors: state.authors,
    members: state.members,
    defaultReplyMode: state.activeRoom.default_reply_mode,
    onSend: ({ authorId, text, replyMode, quotedIds }) => {
      if (!state.socket) {
        toast("Disconnected", "Not connected to room");
        return false;
      }
      const ok = state.socket.post({ authorId, text, replyMode, quotedIds });
      if (!ok) {
        toast("Disconnected", "WebSocket not open — try again in a moment");
        return false;
      }
      // optimistic echo of the human message (reconciled on turn_complete)
      state.transcript.messages.push({
        id: "optimistic-" + Date.now(),
        author_kind: "human",
        author_ref: authorId,
        content: text,
        quoted_message_ids: quotedIds,
        run_id: null,
        created_at: new Date().toISOString(),
      });
      state.transcript.renderMessages(state.transcript.messages);
      return true;
    },
  });
}

async function refreshMessages() {
  try {
    const msgs = await api.listMessages(state.activeRoom.id);
    state.transcript.renderMessages(msgs);
  } catch (e) {
    toast(e.kind, e.message);
  }
}

// -- websocket ---------------------------------------------------------------

function openSocket(roomId) {
  state.socket = new RoomSocket(roomId, {
    event: (frame) => state.transcript.onEvent(frame.persona_id, frame.event),
    error_card: (frame) => state.transcript.appendErrorCard(frame.persona_id, frame),
    // Canonical-id contract: refetch on end-of-turn to reconcile optimistic
    // bubbles (which have no persisted id/run_id) to the canonical messages.
    turn_complete: () => refreshMessages(),
    command_complete: () => {
      /* console handles its own completion */
    },
    error: (frame) => toast(frame.kind, frame.message),
  });
}

// -- settings buttons --------------------------------------------------------

function wireSettings() {
  $("new-room-btn").addEventListener("click", () =>
    createRoom(async (room) => {
      await loadRooms();
      selectRoom(room.id);
    })
  );
  $("open-personas").addEventListener("click", () =>
    openPersonaManager({
      onChange: async () => {
        if (state.activeRoom) {
          state.members = await api.listMembers(state.activeRoom.id);
          indexPersonas(state.members);
          state.composer?.setMembers(state.members);
          renderHeader();
        }
      },
    })
  );
  $("open-authors").addEventListener("click", () =>
    openAuthorManager({
      onChange: async () => {
        await loadAuthors();
        state.composer?.setAuthors(state.authors);
      },
    })
  );
}

// -- bootstrap ---------------------------------------------------------------

async function boot() {
  wireSettings();
  await loadHealth();
  try {
    await loadAuthors();
    await loadRooms();
  } catch (e) {
    toast(e.kind, "Failed to load: " + e.message);
    return;
  }
  const first = state.rooms.find((r) => !r.archived) || state.rooms[0];
  if (first) {
    selectRoom(first.id);
  } else {
    $("room-header").append(
      el("span", { class: "topic", text: "No rooms yet — create one to begin." })
    );
  }
}

boot();
