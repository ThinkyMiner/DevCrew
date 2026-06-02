// errors.js — surface backend failures (Task 6.5 / FR-E2).
//
// Two surfaces:
//  * error_card frames are rendered INLINE in the transcript by transcript.js
//    (appendErrorCard) so a failed run stays attached to its persona bubble with
//    error_kind, message, the REDACTED command, and a link to the run log.
//  * generic {type:error} frames (inbound-validation / pre-run errors — bad
//    author, unsupported command, ...) are transient and not tied to a run, so
//    we show them as a dismissible toast here.

import { el } from "./dom.js";

export function toast(kind, message, ms = 6000) {
  const wrap = document.getElementById("toasts");
  if (!wrap) return;
  const node = el("div", { class: "toast" }, [
    el("div", { class: "tk", text: "⚠ " + (kind || "error") }),
    el("div", { class: "tm", text: message || "" }),
  ]);
  wrap.append(node);
  const kill = () => node.remove();
  node.addEventListener("click", kill);
  setTimeout(kill, ms);
}
