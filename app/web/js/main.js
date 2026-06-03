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
  // Monotonic room-switch token (Fix #4): bumped on each selectRoom; a slow
  // earlier switch checks it after every await and bails if a newer switch won.
  switchToken: 0,
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
  // Fix #4: claim a switch token; after every await, bail if a newer switch
  // started. This prevents a slow earlier selectRoom from overwriting
  // state.socket/activeRoom with the previous room's data.
  const token = ++state.switchToken;
  const stale = () => token !== state.switchToken;

  if (state.socket) {
    state.socket.close(); // closedByUs → no reconnect for the old room
    state.socket = null;
  }
  const activeRoom = await api.getRoom(roomId);
  if (stale()) return;
  state.activeRoom = activeRoom;
  const members = await api.listMembers(roomId);
  if (stale()) return;
  state.members = members;
  indexPersonas(state.members);
  // also index all personas so historical authors render correctly
  try {
    const all = await api.listPersonas();
    if (stale()) return;
    indexPersonas(all);
  } catch {
    /* non-fatal */
  }
  if (stale()) return;

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
  if (stale()) return;
  openSocket(roomId, token);
}

function openMemberConsole(m, r) {
  openConsole({
    persona: m,
    roomId: r.id,
    socket: state.socket,
    onReset: () => toast("Session", `@${m.handle} session reset`, 3000),
  });
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
          role: "button",
          tabindex: "0",
          "aria-label": `Open session console for @${m.handle}`,
          onClick: () => openMemberConsole(m, r),
          onKeydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openMemberConsole(m, r);
            }
          },
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
    return;
  }
  // Delegation can pull new personas into the room mid-turn; refresh membership
  // so the header chips and @-mention autocomplete include them. Only re-render
  // when the member set actually changed (avoids churn on every reconcile).
  try {
    const members = await api.listMembers(state.activeRoom.id);
    const changed =
      members.length !== state.members.length ||
      members.some((m, i) => m.id !== state.members[i]?.id);
    if (changed) {
      state.members = members;
      indexPersonas(members);
      renderHeader();
      state.composer?.setMembers(members);
    }
  } catch {
    /* non-fatal: rendering already used the all-personas index */
  }
}

// -- websocket ---------------------------------------------------------------

// Reconcile = refetch canonical messages and repaint, which clears ALL
// optimistic-* echoes and partial streaming bubbles (renderMessages clears the
// streaming map and replaces children) — so ghosts left by a turn that never
// completed cannot persist or pollute quote lookups.
//
// Reconcile triggers (Fix #2):
//   * turn_complete    — normal end of a human turn (canonical-id contract)
//   * command_complete — end of a console command turn
//   * error            — pre-yield failure with no turn_complete
//   * reconnect (open) — socket re-established after a drop mid-turn
function openSocket(roomId, token) {
  const isCurrent = () => token === state.switchToken && state.activeRoom?.id === roomId;
  let everOpened = false;

  state.socket = new RoomSocket(roomId, {
    onStatus: (s) => {
      if (isCurrent()) renderConnBanner(s);
    },
    handlers: {
      open: () => {
        // On a RE-open (reconnect), reconcile so stale optimistic/partial
        // bubbles are replaced by canonical state.
        if (everOpened && isCurrent()) refreshMessages();
        everOpened = true;
      },
      event: (frame) => state.transcript.onEvent(frame.persona_id, frame.event),
      error_card: (frame) => state.transcript.appendErrorCard(frame.persona_id, frame),
      // Canonical-id contract: refetch on end-of-turn to reconcile optimistic
      // bubbles (which have no persisted id/run_id) to the canonical messages.
      turn_complete: () => refreshMessages(),
      // A console command turn also persists/changes state; reconcile so the
      // transcript reflects it and no partial bubble lingers.
      command_complete: () => refreshMessages(),
      // Pre-yield error (no turn_complete): reconcile to drop the orphaned
      // optimistic echo + any partial persona bubble, then surface the error.
      error: (frame) => {
        toast(frame.kind, frame.message);
        refreshMessages();
      },
    },
  });
}

// -- connection-state banner (Fix #3) ----------------------------------------

function renderConnBanner(stateName) {
  const b = $("conn-banner");
  clear(b);
  b.className = "conn-banner";
  if (stateName === "open" || stateName === "connecting") {
    // open: hidden. connecting (first attempt): stay quiet to avoid flicker.
    return;
  }
  if (stateName === "reconnecting") {
    b.classList.add("show");
    b.append(
      el("span", { class: "conn-spinner" }),
      el("span", { text: "Reconnecting…" })
    );
  } else if (stateName === "closed") {
    b.classList.add("show", "down");
    b.append(
      el("span", { class: "conn-spinner" }),
      el("span", { text: "Disconnected — connection lost." }),
      el("button", {
        class: "btn tiny conn-retry",
        text: "Reconnect",
        onClick: () => {
          renderConnBanner("reconnecting");
          state.socket?.reconnect();
        },
      })
    );
  }
}

// -- settings buttons --------------------------------------------------------

// Let role="button" divs activate on Enter/Space like real buttons (a11y).
function onActivate(node, fn) {
  node.addEventListener("click", fn);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  });
}

function wireSettings() {
  $("new-room-btn").addEventListener("click", () =>
    createRoom(async (room) => {
      await loadRooms();
      selectRoom(room.id);
    })
  );
  onActivate($("open-personas"), () =>
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
  onActivate($("open-authors"), () =>
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
