// ws.js — WebSocket client for one room (/ws/rooms/{room_id}).
//
// Connects, sends `post`/`command` frames, and dispatches inbound frames to
// per-type handlers. Frame protocol mirrors app/api/ws.py:
//   inbound (we send):  {type:"post", author_id, text, reply_mode, quoted_ids}
//                       {type:"command", persona_id, command}
//   outbound (we recv): {type:"event", persona_id, event:{kind:...}}
//                       {type:"error_card", persona_id, run_id, error_kind,
//                                message, command_redacted, log_url}
//                       {type:"turn_complete"} | {type:"command_complete"}
//                       {type:"error", kind, message}
//
// Reconnect is best-effort (optional per brief): on an unclean close we retry
// with a short backoff while the same room is selected.

export class RoomSocket {
  constructor(roomId, handlers = {}) {
    this.roomId = roomId;
    this.handlers = handlers; // { event, error_card, turn_complete, command_complete, error, open, close }
    this.ws = null;
    this.closedByUs = false;
    this._retry = 0;
    this._connect();
  }

  _url() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/rooms/${encodeURIComponent(this.roomId)}`;
  }

  _connect() {
    const ws = new WebSocket(this._url());
    this.ws = ws;
    ws.addEventListener("open", () => {
      this._retry = 0;
      this.handlers.open?.();
    });
    ws.addEventListener("message", (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      const fn = this.handlers[frame.type];
      if (fn) fn(frame);
    });
    ws.addEventListener("close", () => {
      this.handlers.close?.();
      if (this.closedByUs) return;
      // best-effort reconnect with capped backoff
      const delay = Math.min(1000 * 2 ** this._retry, 8000);
      this._retry += 1;
      setTimeout(() => {
        if (!this.closedByUs) this._connect();
      }, delay);
    });
    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    });
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  post({ authorId, text, replyMode, quotedIds = [] }) {
    return this._send({
      type: "post",
      author_id: authorId,
      text,
      reply_mode: replyMode,
      quoted_ids: quotedIds,
    });
  }

  command({ personaId, command }) {
    return this._send({ type: "command", persona_id: personaId, command });
  }

  close() {
    this.closedByUs = true;
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
  }
}
