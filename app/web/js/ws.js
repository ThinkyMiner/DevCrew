// ws.js — WebSocket client for one room (/ws/rooms/{room_id}).
//
// Connects, sends `post`/`command` frames, and dispatches inbound frames to
// per-type SUBSCRIBERS. Frame protocol mirrors app/api/ws.py:
//   inbound (we send):  {type:"post", author_id, text, reply_mode, quoted_ids}
//                       {type:"command", persona_id, command}
//   outbound (we recv): {type:"event", persona_id, event:{kind:...}}
//                       {type:"error_card", persona_id, run_id, error_kind,
//                                message, command_redacted, log_url}
//                       {type:"turn_complete"} | {type:"command_complete"}
//                       {type:"error", kind, message}
//
// Fan-out (Fix #5): instead of a single mutable `handlers` map that callers
// monkey-patched (which clobbered each other and leaked on partial close), each
// frame type has a SET of subscribers. addHandler(type, fn) returns an
// unsubscribe(). The transcript and a session console can both receive frames
// without stomping on one another, and a console reliably detaches on ANY
// close path via its unsubscribe.
//
// Connection state (Fix #3): onStatus(state) reports
// "connecting"|"open"|"reconnecting"|"closed" so the UI can show a banner.
// Reconnect uses capped backoff and STOPS after MAX_RETRIES, emitting "closed"
// so the user gets a manual "Reconnect" affordance instead of an infinite loop.

const MAX_RETRIES = 5;

export class RoomSocket {
  /**
   * @param roomId
   * @param opts { onStatus?(state), handlers? } — `handlers` is an optional map
   *   of {type: fn} subscribed at construction (convenience for the primary
   *   transcript consumer). Use addHandler() for additional subscribers.
   */
  constructor(roomId, opts = {}) {
    this.roomId = roomId;
    this.onStatus = opts.onStatus || (() => {});
    // type -> Set<fn>
    this._subs = new Map();
    this.ws = null;
    this.closedByUs = false;
    this._retry = 0;
    this._timer = null;
    if (opts.handlers) {
      for (const [type, fn] of Object.entries(opts.handlers)) {
        if (typeof fn === "function") this.addHandler(type, fn);
      }
    }
    this._connect();
  }

  /** Subscribe `fn` to frames of `type`. Returns an unsubscribe function. */
  addHandler(type, fn) {
    let set = this._subs.get(type);
    if (!set) {
      set = new Set();
      this._subs.set(type, set);
    }
    set.add(fn);
    return () => this.removeHandler(type, fn);
  }

  removeHandler(type, fn) {
    this._subs.get(type)?.delete(fn);
  }

  _emit(type, ...args) {
    const set = this._subs.get(type);
    if (!set) return;
    // Copy so a handler unsubscribing mid-dispatch doesn't break iteration.
    for (const fn of [...set]) {
      try {
        fn(...args);
      } catch {
        /* a misbehaving subscriber must not break others */
      }
    }
  }

  _url() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/rooms/${encodeURIComponent(this.roomId)}`;
  }

  _connect() {
    this.onStatus(this._retry > 0 ? "reconnecting" : "connecting");
    const ws = new WebSocket(this._url());
    this.ws = ws;
    ws.addEventListener("open", () => {
      this._retry = 0;
      this.onStatus("open");
      this._emit("open");
    });
    ws.addEventListener("message", (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      this._emit(frame.type, frame);
    });
    ws.addEventListener("close", () => {
      this._emit("close");
      if (this.closedByUs) return;
      if (this._retry >= MAX_RETRIES) {
        // Give up auto-retrying; surface a manual-reconnect affordance.
        this.onStatus("closed");
        return;
      }
      const delay = Math.min(1000 * 2 ** this._retry, 8000);
      this._retry += 1;
      this.onStatus("reconnecting");
      this._timer = setTimeout(() => {
        this._timer = null;
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

  /** Manual reconnect after the auto-retry cap was hit (user-initiated). */
  reconnect() {
    if (this.closedByUs) return;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    this._retry = 0;
    this._connect();
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
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
  }
}
