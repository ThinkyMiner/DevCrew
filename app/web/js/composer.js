// composer.js — message composer (Task 6.3 / FR-A1, M1/M2, M5, MR1).
//
// Builds: a "Writing as" author selector (Boss shows a weighted indicator,
// FR-A1/A3), @-mention autocomplete from the room's member personas + @everyone
// (FR-M2, mirrors mentions.py), removable quote chips (FR-M5), a sequential /
// parallel reply-mode toggle defaulting to the room's default (FR-MR1), and a
// textarea. Send emits a WS `post` frame via the injected callback.

import { el, clear, truncate } from "./dom.js";
import { activeMention } from "./mentions.js";

export class Composer {
  /**
   * @param root container
   * @param ctx { authors:[], members:[], defaultReplyMode, onSend({authorId,text,replyMode,quotedIds}) }
   */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.quoted = []; // [{id, label}]
    this.replyMode = ctx.defaultReplyMode || "sequential";
    this.authorId = null;
    this._mentionIdx = 0;
    this._mentionMatches = [];
    this._build();
  }

  _build() {
    clear(this.root);

    // bar: writing-as + weight flag + mode toggle
    this.authorSelect = el("select", { "aria-label": "Writing as (author)" });
    this.weightFlag = el("span", { class: "weight-flag", text: "✦ weighted input" });
    const writingAs = el("div", { class: "writing-as" }, [
      el("span", { text: "Writing as" }),
      this.authorSelect,
      this.weightFlag,
    ]);

    this.modeSeq = el("button", {
      text: "sequential",
      "aria-label": "Reply mode: sequential",
      onClick: () => this._setMode("sequential"),
    });
    this.modePar = el("button", {
      text: "parallel",
      "aria-label": "Reply mode: parallel",
      onClick: () => this._setMode("parallel"),
    });
    const modeToggle = el("div", { class: "mode-toggle", title: "Reply mode (FR-MR1)" }, [
      this.modeSeq,
      this.modePar,
    ]);

    this.quoteChips = el("div", { class: "quote-chips" });

    const bar = el("div", { class: "composer-bar" }, [
      writingAs,
      el("span", { style: { flex: "1" } }),
      modeToggle,
    ]);

    // input + mention popup
    this.textarea = el("textarea", {
      rows: "1",
      "aria-label": "Message",
      placeholder: "Message  ·  @handle to address a persona, @everyone for all  ·  Enter to send",
      onInput: () => {
        this._autosize();
        this._updateMentions();
      },
      onKeydown: (e) => this._onKeydown(e),
      onClick: () => this._updateMentions(),
      onBlur: () => setTimeout(() => this._hideMentions(), 120),
    });
    this.mentionPop = el("div", { class: "mention-pop" });
    const sendBtn = el("button", { class: "btn primary", text: "Send", onClick: () => this.send() });
    const inputWrap = el("div", { class: "composer-input-wrap" }, [
      this.mentionPop,
      this.textarea,
      sendBtn,
    ]);

    this.root.append(bar, this.quoteChips, inputWrap);

    this.setAuthors(this.ctx.authors || []);
    this._setMode(this.replyMode);
    this._renderQuotes();
  }

  // -- authors --------------------------------------------------------------

  setAuthors(authors) {
    this.ctx.authors = authors;
    clear(this.authorSelect);
    for (const a of authors) {
      this.authorSelect.append(el("option", { value: a.id, text: a.name }));
    }
    // default to "Me" if present, else first
    const me = authors.find((a) => a.name.toLowerCase() === "me") || authors[0];
    if (me) this.authorId = me.id;
    if (this.authorId) this.authorSelect.value = this.authorId;
    this.authorSelect.onchange = () => {
      this.authorId = this.authorSelect.value;
      this._reflectWeight();
    };
    this._reflectWeight();
  }

  _reflectWeight() {
    const a = (this.ctx.authors || []).find((x) => x.id === this.authorId);
    const weighted = !!(a && a.weight_enabled && (a.weight_note || "").trim());
    this.weightFlag.classList.toggle("show", weighted);
  }

  // -- reply mode -----------------------------------------------------------

  setDefaultReplyMode(mode) {
    this.ctx.defaultReplyMode = mode;
    this._setMode(mode);
  }

  _setMode(mode) {
    this.replyMode = mode === "parallel" ? "parallel" : "sequential";
    this.modeSeq.classList.toggle("active", this.replyMode === "sequential");
    this.modePar.classList.toggle("active", this.replyMode === "parallel");
  }

  // -- quote chips (FR-M5) --------------------------------------------------

  setMembers(members) {
    this.ctx.members = members;
  }

  addQuote(msg, label) {
    if (this.quoted.some((q) => q.id === msg.id)) return;
    this.quoted.push({ id: msg.id, label: label || truncate(msg.content, 48) });
    this._renderQuotes();
  }

  _renderQuotes() {
    clear(this.quoteChips);
    for (const q of this.quoted) {
      this.quoteChips.append(
        el("div", { class: "quote-chip" }, [
          el("span", { class: "qc-text", text: q.label }),
          el("span", {
            class: "qc-x",
            text: "✕",
            role: "button",
            tabindex: "0",
            "aria-label": "Remove quote",
            onClick: () => {
              this.quoted = this.quoted.filter((x) => x.id !== q.id);
              this._renderQuotes();
            },
          }),
        ])
      );
    }
  }

  // -- mention autocomplete (FR-M2) -----------------------------------------

  _candidates() {
    const list = [{ handle: "everyone", name: "Everyone in room", color: "#9aa1b8" }];
    for (const p of this.ctx.members || []) {
      list.push({ handle: p.handle, name: p.name, color: p.color });
    }
    return list;
  }

  _updateMentions() {
    const caret = this.textarea.selectionStart ?? this.textarea.value.length;
    const m = activeMention(this.textarea.value, caret);
    if (!m) return this._hideMentions();
    const q = m.query;
    this._mentionMatches = this._candidates().filter((c) => c.handle.startsWith(q));
    if (!this._mentionMatches.length) return this._hideMentions();
    this._mentionStart = m.start;
    this._mentionIdx = 0;
    this._renderMentions();
  }

  _renderMentions() {
    clear(this.mentionPop);
    this._mentionMatches.forEach((c, i) => {
      this.mentionPop.append(
        el(
          "div",
          {
            class: "mention-item" + (i === this._mentionIdx ? " active" : ""),
            onMousedown: (e) => {
              e.preventDefault();
              this._applyMention(c);
            },
          },
          [
            el("span", { class: "dot", style: { background: c.color } }),
            el("span", { text: c.name }),
            el("span", { class: "mh", text: "@" + c.handle }),
          ]
        )
      );
    });
    this.mentionPop.classList.add("show");
  }

  _applyMention(c) {
    const v = this.textarea.value;
    const caret = this.textarea.selectionStart ?? v.length;
    const before = v.slice(0, this._mentionStart);
    const after = v.slice(caret);
    const insert = "@" + c.handle + " ";
    this.textarea.value = before + insert + after;
    const pos = before.length + insert.length;
    this.textarea.setSelectionRange(pos, pos);
    this._hideMentions();
    this.textarea.focus();
    this._autosize();
  }

  _hideMentions() {
    this.mentionPop.classList.remove("show");
    this._mentionMatches = [];
  }

  _onKeydown(e) {
    if (this.mentionPop.classList.contains("show") && this._mentionMatches.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        this._mentionIdx = (this._mentionIdx + 1) % this._mentionMatches.length;
        return this._renderMentions();
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        this._mentionIdx =
          (this._mentionIdx - 1 + this._mentionMatches.length) % this._mentionMatches.length;
        return this._renderMentions();
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        return this._applyMention(this._mentionMatches[this._mentionIdx]);
      }
      if (e.key === "Escape") return this._hideMentions();
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      this.send();
    }
  }

  _autosize() {
    this.textarea.style.height = "auto";
    this.textarea.style.height = Math.min(this.textarea.scrollHeight, 180) + "px";
  }

  // -- send -----------------------------------------------------------------

  send() {
    const text = this.textarea.value.trim();
    if (!text || !this.authorId) return;
    const ok = this.ctx.onSend?.({
      authorId: this.authorId,
      text,
      replyMode: this.replyMode,
      quotedIds: this.quoted.map((q) => q.id),
    });
    if (ok !== false) {
      this.textarea.value = "";
      this.quoted = [];
      this._renderQuotes();
      this._autosize();
    }
  }
}
