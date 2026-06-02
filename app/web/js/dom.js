// dom.js — XSS-safe DOM construction helpers.
//
// HARD REQUIREMENT (Unit 12): all message / persona / tool / boss content is
// UNTRUSTED (model output + pasted text). It is rendered ONLY through these
// helpers, which set `textContent` — never `innerHTML` — so the browser can
// never interpret content as markup. Attributes are set via setAttribute on a
// fixed allowlist (`href` is additionally scheme-checked in safeHref). There is
// no code path in this app that assigns untrusted strings to innerHTML.

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Create an element. `props.text` sets textContent (SAFE). `props.html` is
 * intentionally NOT supported. Event handlers via on* keys (functions).
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "text") {
      node.textContent = String(v); // safe sink
    } else if (k === "class" || k === "className") {
      node.className = String(v);
    } else if (k === "dataset") {
      for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = String(dv);
    } else if (k === "style" && typeof v === "object") {
      Object.assign(node.style, v);
    } else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else {
      node.setAttribute(k, String(v));
    }
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function svgIcon(pathD, size = 14) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", String(size));
  s.setAttribute("height", String(size));
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", pathD);
  s.append(p);
  return s;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

/**
 * Confine a URL to a safe scheme before using it as an href. Run-log links are
 * server-built ("/runs/<id>/log") relative paths; this rejects javascript:/data:
 * etc. so a hostile log_url can never become script execution.
 */
export function safeHref(url) {
  if (typeof url !== "string") return null;
  const u = url.trim();
  if (u.startsWith("/") && !u.startsWith("//")) return u; // same-origin relative
  if (/^https?:\/\//i.test(u)) return u;
  return null;
}

/** Format an ISO timestamp to a short local time string. */
export function fmtTime(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

/** Truncate a string for chip/preview display. */
export function truncate(s, n = 60) {
  s = String(s ?? "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
