// modal.js — a tiny reusable modal built on the #overlay/#modal shell.
// All content is supplied as DOM nodes by callers (built XSS-safely via dom.js).

import { el, clear } from "./dom.js";

const overlay = () => document.getElementById("overlay");
const modal = () => document.getElementById("modal");

export function openModal({ title, body, footer }) {
  const m = modal();
  clear(m);
  m.append(
    el("div", { class: "modal-head" }, [
      el("h2", { text: title }),
      el("span", { class: "close-x", text: "✕", onClick: closeModal }),
    ]),
    el("div", { class: "modal-body" }, [].concat(body || [])),
    footer ? el("div", { class: "modal-foot" }, [].concat(footer)) : null
  );
  const o = overlay();
  o.classList.add("show");
  o.onclick = (e) => {
    if (e.target === o) closeModal();
  };
}

export function closeModal() {
  overlay().classList.remove("show");
  clear(modal());
}

/** Build a labeled form field; returns { field, input }. */
export function field(label, input, hint) {
  const f = el("div", { class: "field" }, [
    el("label", { text: label }),
    input,
    hint ? el("div", { class: "hint", text: hint }) : null,
  ]);
  return { field: f, input };
}
