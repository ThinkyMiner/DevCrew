// modal.js — a tiny reusable modal built on the #overlay/#modal shell.
// All content is supplied as DOM nodes by callers (built XSS-safely via dom.js).
//
// Close paths (Close button, ✕, backdrop click, Escape) ALL funnel through
// closeModal(), which invokes the active modal's onClose callback exactly once.
// This lets callers (e.g. the session console) reliably release resources —
// such as WS frame subscriptions — no matter how the modal is dismissed.
// a11y (Fix #7): role="dialog" aria-modal, Escape-to-close, initial focus.

import { el, clear } from "./dom.js";

const overlay = () => document.getElementById("overlay");
const modal = () => document.getElementById("modal");

// Active modal's onClose + keydown listener, tracked so closeModal can clean up.
let activeOnClose = null;
let keyListener = null;

export function openModal({ title, body, footer, onClose }) {
  // If a modal is already open, close it cleanly first (fires its onClose).
  if (activeOnClose || keyListener) closeModal();

  const m = modal();
  clear(m);
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-modal", "true");
  if (title) m.setAttribute("aria-label", String(title));
  m.append(
    el("div", { class: "modal-head" }, [
      el("h2", { text: title }),
      el("span", {
        class: "close-x",
        text: "✕",
        role: "button",
        tabindex: "0",
        "aria-label": "Close dialog",
        onClick: closeModal,
      }),
    ]),
    el("div", { class: "modal-body" }, [].concat(body || [])),
    footer ? el("div", { class: "modal-foot" }, [].concat(footer)) : null
  );

  activeOnClose = typeof onClose === "function" ? onClose : null;

  const o = overlay();
  o.classList.add("show");
  o.onclick = (e) => {
    if (e.target === o) closeModal();
  };

  keyListener = (e) => {
    if (e.key === "Escape") closeModal();
  };
  document.addEventListener("keydown", keyListener);

  // Focus the first form field in the body (NOT the header ✕) for keyboard/AT
  // users; fall back to the first focusable element anywhere in the modal.
  const bodyEl = m.querySelector(".modal-body");
  const sel = "input, select, textarea, button, [tabindex]:not([tabindex='-1'])";
  const first = (bodyEl && bodyEl.querySelector(sel)) || m.querySelector(sel);
  if (first) first.focus();
}

export function closeModal() {
  overlay().classList.remove("show");
  clear(modal());
  if (keyListener) {
    document.removeEventListener("keydown", keyListener);
    keyListener = null;
  }
  const cb = activeOnClose;
  activeOnClose = null;
  if (cb) cb();
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
