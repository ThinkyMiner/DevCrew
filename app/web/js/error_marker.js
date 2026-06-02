// error_marker.js — pure parsing of the persisted persona error-marker.
//
// When a persona run fails, the orchestrator persists a real persona Message
// whose content is `"[error: <kind>] <message>"` (see _emit_error in
// app/services/orchestrator.py). The canonical transcript render must show that
// persisted marker as a STYLED error card (matching the transient error_card WS
// frame) instead of a plain persona bubble, so a failure is visible exactly
// once after the turn_complete repaint.
//
// This module holds ONLY the pure string parsing so it is unit-testable without
// a DOM (see tests/js/error_marker.test.mjs). Rendering lives in transcript.js.

// Leading `[error:` (case-insensitive, tolerant of inner whitespace) marks the
// persisted failure marker.
const MARKER_RE = /^\s*\[error:\s*([^\]]*)\]\s*([\s\S]*)$/i;

/**
 * Detect whether a message content string is a persisted error marker.
 * @param {string} content
 * @returns {boolean}
 */
export function isErrorMarker(content) {
  return typeof content === "string" && MARKER_RE.test(content);
}

/**
 * Parse a persisted error-marker string into its kind + message.
 * Returns null when `content` is not an error marker.
 * @param {string} content e.g. "[error: HarnessTimeout] claude run exceeded 600s"
 * @returns {{kind: string, message: string} | null}
 */
export function parseErrorMarker(content) {
  if (typeof content !== "string") return null;
  const m = MARKER_RE.exec(content);
  if (!m) return null;
  return { kind: m[1].trim() || "error", message: m[2].trim() };
}
