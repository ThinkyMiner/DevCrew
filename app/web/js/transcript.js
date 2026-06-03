// transcript.js — the message list + live streaming (Task 6.2 / FR-T1..T3, C3).
//
// Two render paths that meet at turn_complete:
//
//  1. CANONICAL render (renderMessages): paint the persisted transcript fetched
//     from GET /rooms/{id}/messages. Each message shows author name + color +
//     @handle, timestamp, content (markdown-safe), quoted-message refs, and a
//     footer linking to its run log (FR-T4). A persisted persona FAILURE is
//     stored as an error-marker Message ("[error: <kind>] <msg>"); the canonical
//     render detects it and paints the SAME styled error card as the transient
//     error_card frame (not a plain bubble) — so the failure is visible exactly
//     once after the turn_complete repaint and stays traceable on reload.
//
//  2. OPTIMISTIC streaming (onEvent): the WS stream does NOT carry the persisted
//     reply id (see app/api/ws.py "Canonical message ids"). As {type:event}
//     frames arrive we build a transient per-persona bubble and append text
//     deltas live (FR-T1), render thinking in a collapsible block (FR-T2), and
//     render tool_use/tool_result as inline cards (FR-T3); usage events update a
//     per-persona context indicator (FR-C3). On {type:turn_complete} main.js
//     REFETCHES messages and calls renderMessages — reconciling the optimistic
//     bubbles to canonical ids/run_ids. We clear streaming state then.
//
// XSS: every content sink is textContent / renderMarkdown (see dom.js,
// markdown.js). No innerHTML anywhere.

import { el, clear, fmtTime, safeHref, truncate } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { isErrorMarker, parseErrorMarker } from "./error_marker.js";

export class Transcript {
  /**
   * @param root container element
   * @param ctx  { authorsById, personasById, onQuote(msg) }
   */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    // persona_id -> { node, bodyEl, text, usageEl } for the in-progress bubble
    this.streaming = new Map();
    this.messages = [];
  }

  setContext(ctx) {
    Object.assign(this.ctx, ctx);
  }

  // -- attribution helpers (resolve author/persona display + color) ---------

  _attr(msg) {
    if (msg.author_kind === "persona") {
      const p = this.ctx.personasById.get(msg.author_ref);
      return {
        name: p ? p.name : "persona",
        handle: p ? p.handle : null,
        job: p ? p.job || "" : "",
        color: p ? p.color : "#9aa1b8",
        boss: false,
      };
    }
    const a = this.ctx.authorsById.get(msg.author_ref);
    return {
      name: a ? a.name : "author",
      handle: null,
      job: "",
      color: a ? a.color : "#9ee37d",
      boss: !!(a && a.weight_enabled && (a.weight_note || "").trim()),
    };
  }

  _quotePreview(id) {
    const q = this.messages.find((m) => m.id === id);
    if (!q) return `quoted message ${truncate(id, 8)}`;
    const a = this._attr(q);
    return `${a.name}: ${truncate(q.content, 70)}`;
  }

  // -- canonical render ------------------------------------------------------

  renderMessages(messages) {
    this.messages = messages.slice();
    // Canonical paint supersedes any optimistic bubbles (reconcile).
    this.streaming.clear();
    clear(this.root);
    if (!messages.length) {
      this.root.append(
        el("div", { class: "transcript-empty", text: "No messages yet — tag a persona to begin." })
      );
      return;
    }
    for (const msg of messages) this.root.append(this._messageNode(msg));
    this._scrollToBottom();
  }

  _messageNode(msg) {
    // A persona failure is persisted as a real Message whose content is an
    // error marker ("[error: <kind>] <msg>"; see orchestrator._emit_error).
    // Render it as the SAME styled error card the transient error_card WS frame
    // uses — NOT a plain bubble — so the failure shows exactly once after the
    // turn_complete repaint (and stays traceable via its run-log link on reload).
    if (msg.author_kind === "persona" && isErrorMarker(msg.content)) {
      const parsed = parseErrorMarker(msg.content);
      return this._errorCardNode({
        kind: parsed ? parsed.kind : "error",
        message: parsed ? parsed.message : "",
        logUrl: msg.run_id ? "/runs/" + encodeURIComponent(msg.run_id) + "/log" : null,
      });
    }
    const a = this._attr(msg);
    const head = el("div", { class: "msg-head" }, [
      el("span", { class: "msg-author", text: a.name, style: { color: a.color } }),
      a.handle ? el("span", { class: "msg-handle", text: "@" + a.handle }) : null,
      a.job ? el("span", { class: "msg-job", text: a.job }) : null,
      a.boss ? el("span", { class: "msg-boss-badge", text: "weighted" }) : null,
      el("span", { class: "msg-time", text: fmtTime(msg.created_at) }),
    ]);

    const children = [head];

    if (Array.isArray(msg.quoted_message_ids) && msg.quoted_message_ids.length) {
      const q = el("div", { class: "msg-quotes" });
      for (const qid of msg.quoted_message_ids) {
        q.append(el("div", { class: "quote-ref", text: "❝ " + this._quotePreview(qid) }));
      }
      children.push(q);
    }

    const body = el("div", { class: "msg-body" });
    renderMarkdown(body, msg.content);
    children.push(body);

    // footer: run-log link (FR-T4) + quote action
    const footer = el("div", { class: "msg-footer" });
    if (msg.run_id) {
      const href = safeHref("/runs/" + encodeURIComponent(msg.run_id) + "/log");
      if (href) {
        footer.append(
          el("a", { href, target: "_blank", rel: "noopener", text: "run log ↗" })
        );
      }
    }
    children.push(footer);

    const actions = el("div", { class: "msg-actions" }, [
      el("button", {
        class: "btn tiny ghost",
        text: "Quote",
        onClick: () => this.ctx.onQuote?.(msg),
      }),
    ]);
    children.push(actions);

    return el("div", { class: "msg", dataset: { id: msg.id } }, children);
  }

  // -- optimistic streaming --------------------------------------------------

  /** Get-or-create the live bubble for a streaming persona. */
  _bubble(personaId) {
    let b = this.streaming.get(personaId);
    if (b) return b;
    const p = this.ctx.personasById.get(personaId);
    const color = p ? p.color : "#9aa1b8";
    const body = el("div", { class: "msg-body" });
    const usage = el("span", { class: "usage-pill" });
    const node = el("div", { class: "msg streaming", dataset: { streaming: personaId } }, [
      el("div", { class: "msg-head" }, [
        el("span", { class: "msg-author", text: p ? p.name : "persona", style: { color } }),
        p ? el("span", { class: "msg-handle", text: "@" + p.handle }) : null,
        p && p.job ? el("span", { class: "msg-job", text: p.job }) : null,
        el("span", { class: "msg-time", text: "now" }),
      ]),
      body,
      el("div", { class: "msg-footer" }, [usage]),
    ]);
    b = { node, body, text: "", usage, thinkBody: null, tools: new Map() };
    this.streaming.set(personaId, b);
    this.root.append(node);
    this._scrollToBottom();
    return b;
  }

  /** Handle one {type:event} frame's inner StreamEvent. */
  onEvent(personaId, event) {
    const kind = event && event.kind;
    if (kind === "text") {
      const b = this._bubble(personaId);
      b.text += event.text || "";
      renderMarkdown(b.body, b.text); // safe
      this._scrollToBottom();
    } else if (kind === "thinking") {
      const b = this._bubble(personaId);
      if (!b.thinkBody) {
        b.thinkBody = el("div", { class: "think-body" });
        const det = el("details", { class: "think" }, [
          el("summary", { text: "thinking" }),
          b.thinkBody,
        ]);
        b.body.after(det);
      }
      b.thinkBody.textContent += event.text || ""; // safe sink
      this._scrollToBottom();
    } else if (kind === "tool_use") {
      this._renderToolUse(personaId, event);
    } else if (kind === "tool_result") {
      this._renderToolResult(personaId, event);
    } else if (kind === "usage") {
      const b = this._bubble(personaId);
      this._renderUsage(b.usage, event);
    } else if (kind === "done") {
      const b = this.streaming.get(personaId);
      if (b) b.node.classList.remove("streaming"); // stop the caret; await reconcile
    } else if (kind === "error") {
      // Surfaced as an error_card frame by the server; ignore the raw event.
    }
  }

  _renderUsage(target, event) {
    clear(target);
    const ctx = event.context_tokens;
    const parts = [];
    if (ctx != null) parts.push(["ctx", ctx]);
    parts.push(["in", event.input_tokens ?? 0]);
    parts.push(["out", event.output_tokens ?? 0]);
    target.append(document.createTextNode("◷ "));
    parts.forEach(([k, v], i) => {
      if (i) target.append(document.createTextNode(" · "));
      target.append(document.createTextNode(k + " "));
      target.append(el("b", { text: String(v) }));
    });
  }

  _renderToolUse(personaId, event) {
    const b = this._bubble(personaId);
    const pre = el("pre");
    let txt = "";
    try {
      txt = typeof event.input === "string" ? event.input : JSON.stringify(event.input, null, 2);
    } catch {
      txt = String(event.input);
    }
    pre.textContent = txt; // safe sink
    const card = el("div", { class: "tool" }, [
      el("div", { class: "tool-head" }, [
        el("span", { class: "tk", text: "⚒" }),
        el("span", { class: "tname", text: event.name || "tool" }),
      ]),
      pre,
    ]);
    b.node.append(card);
    if (event.tool_id) b.tools.set(event.tool_id, card);
    this._scrollToBottom();
  }

  _renderToolResult(personaId, event) {
    const b = this._bubble(personaId);
    const card = (event.tool_id && b.tools.get(event.tool_id)) || null;
    const pre = el("pre");
    pre.textContent = String(event.content ?? ""); // safe sink
    if (card) {
      if (event.is_error) card.classList.add("error");
      card.append(pre);
    } else {
      const standalone = el("div", { class: "tool" + (event.is_error ? " error" : "") }, [
        el("div", { class: "tool-head" }, [
          el("span", { class: "tk", text: "⮑" }),
          el("span", { class: "tname", text: event.is_error ? "tool error" : "tool result" }),
        ]),
        pre,
      ]);
      b.node.append(standalone);
    }
    this._scrollToBottom();
  }

  /**
   * Build a styled error-card node. Shared by the transient error_card WS frame
   * (appendErrorCard) and the canonical render of a persisted error-marker
   * Message (_messageNode), so a failure looks identical in both paths and
   * renders exactly once after reconcile. All sinks are textContent / safeHref.
   * @param {{kind:string, message:string, commandRedacted?:string, logUrl?:string}} info
   */
  _errorCardNode({ kind, message, commandRedacted, logUrl }) {
    const head = el("div", { class: "ec-head" }, [
      el("span", { text: "⚠ " + (kind || "error") }),
    ]);
    const children = [head, el("div", { class: "ec-msg", text: message || "" })];
    if (commandRedacted) {
      children.push(el("div", { class: "ec-cmd", text: "$ " + commandRedacted }));
    }
    const href = safeHref(logUrl);
    if (href) {
      children.push(
        el("div", { class: "ec-cmd" }, [
          el("a", { href, target: "_blank", rel: "noopener", text: "view run log ↗" }),
        ])
      );
    }
    return el("div", { class: "error-card" }, children);
  }

  /** Append an inline error card under the streaming persona (FR-E2). */
  appendErrorCard(personaId, frame) {
    const b = this.streaming.get(personaId) || null;
    const card = this._errorCardNode({
      kind: frame.error_kind || "error",
      message: frame.message || "",
      commandRedacted: frame.command_redacted,
      logUrl: frame.log_url,
    });
    if (b) b.node.append(card);
    else this.root.append(card);
    this._scrollToBottom();
  }

  clearStreaming() {
    this.streaming.clear();
  }

  _scrollToBottom() {
    this.root.scrollTop = this.root.scrollHeight;
  }
}
