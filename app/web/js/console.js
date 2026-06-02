// console.js — per-persona session console (Task 6.4 / FR-C1..C4).
//
// Opens a modal for one persona in the current room exposing harness control:
//   * /compact and /clear buttons (FR-C1) — sent as WS `command` frames.
//   * a raw-command field for any other supported command (FR-C1/C2). Unsupported
//     commands degrade gracefully: the server replies with an {type:error} frame
//     which we render in the console output.
//   * a reset-session action (FR-C4) via POST /rooms/{id}/personas/{pid}/reset-session.
//   * a per-persona context/usage indicator (FR-C3) updated from usage events.
//
// The console borrows the live RoomSocket; command output (events/error cards)
// is streamed back here while the console is open. All sinks are textContent.

import { el, clear } from "./dom.js";
import { openModal, closeModal } from "./modal.js";
import { api } from "./api.js";
import { toast } from "./errors.js";

export function openConsole({ persona, roomId, socket, onReset }) {
  const out = el("div", { class: "console-out", text: "" });
  const ctx = el("div", { class: "console-ctx", text: "context: (run a command to sample usage)" });

  const log = (line) => {
    out.append(document.createTextNode(line + "\n"));
    out.scrollTop = out.scrollHeight;
  };

  // Route this persona's command output into the console while it is open.
  const prevEvent = socket.handlers.event;
  const prevCard = socket.handlers.error_card;
  const prevDone = socket.handlers.command_complete;
  const prevErr = socket.handlers.error;

  const restore = () => {
    socket.handlers.event = prevEvent;
    socket.handlers.error_card = prevCard;
    socket.handlers.command_complete = prevDone;
    socket.handlers.error = prevErr;
  };

  socket.handlers.event = (frame) => {
    prevEvent?.(frame);
    if (frame.persona_id !== persona.id) return;
    const e = frame.event || {};
    if (e.kind === "text") log(e.text || "");
    else if (e.kind === "usage") {
      const c = e.context_tokens != null ? `ctx ${e.context_tokens} · ` : "";
      ctx.textContent = `context: ${c}in ${e.input_tokens ?? 0} / out ${e.output_tokens ?? 0}`;
    } else if (e.kind === "done") log("— done —");
  };
  socket.handlers.error_card = (frame) => {
    prevCard?.(frame);
    if (frame.persona_id === persona.id) log(`[${frame.error_kind}] ${frame.message}`);
  };
  socket.handlers.command_complete = (frame) => {
    prevDone?.(frame);
    log("✓ command complete");
  };
  socket.handlers.error = (frame) => {
    prevErr?.(frame);
    log(`[${frame.kind}] ${frame.message}`);
  };

  const sendCommand = (command) => {
    if (!command) return;
    log("$ " + command);
    const ok = socket.command({ personaId: persona.id, command });
    if (!ok) {
      log("[Disconnected] socket not open");
      toast("Disconnected", "WebSocket not connected");
    }
  };

  const raw = el("input", {
    placeholder: "raw command, e.g. /compact",
    onKeydown: (e) => {
      if (e.key === "Enter") {
        sendCommand(raw.value.trim());
        raw.value = "";
      }
    },
  });

  const body = [
    el("div", { class: "console-cmds" }, [
      el("button", { class: "btn", text: "/compact", onClick: () => sendCommand("/compact") }),
      el("button", { class: "btn", text: "/clear", onClick: () => sendCommand("/clear") }),
      el("button", {
        class: "btn danger",
        text: "Reset session",
        onClick: async () => {
          if (!confirm(`Reset @${persona.handle}'s session in this room? Starts fresh.`)) return;
          try {
            await api.resetSession(roomId, persona.id);
            log("✓ session reset — next turn starts a new harness session");
            onReset?.();
          } catch (err) {
            toast(err.kind, err.message);
          }
        },
      }),
    ]),
    el("div", { class: "console-raw" }, [
      raw,
      el("button", {
        class: "btn",
        text: "Send",
        onClick: () => {
          sendCommand(raw.value.trim());
          raw.value = "";
        },
      }),
    ]),
    ctx,
    out,
  ];

  openModal({
    title: `Console · @${persona.handle}`,
    body,
    footer: [
      el("button", {
        class: "btn",
        text: "Close",
        onClick: () => {
          restore();
          closeModal();
        },
      }),
    ],
  });
}
