// JS logic test for the persisted error-marker parser — run with: node --test tests/js/
//
// Verifies app/web/js/error_marker.js parses the orchestrator's persisted
// "[error: <kind>] <message>" marker (see _emit_error in
// app/services/orchestrator.py) so the canonical transcript render can paint it
// as a styled error card instead of a plain persona bubble (Fix #1).

import { test } from "node:test";
import assert from "node:assert/strict";
import { isErrorMarker, parseErrorMarker } from "../../app/web/js/error_marker.js";

test("parses a typical HarnessTimeout marker", () => {
  const r = parseErrorMarker("[error: HarnessTimeout] claude run exceeded 600s timeout");
  assert.deepEqual(r, { kind: "HarnessTimeout", message: "claude run exceeded 600s timeout" });
});

test("tolerates inner whitespace and empty message", () => {
  assert.deepEqual(parseErrorMarker("[error:HarnessError]"), {
    kind: "HarnessError",
    message: "",
  });
  assert.deepEqual(parseErrorMarker("  [error:  Foo ]  bar baz "), {
    kind: "Foo",
    message: "bar baz",
  });
});

test("keeps a bracket inside the message body", () => {
  const r = parseErrorMarker("[error: X] failed at step [3] of run");
  assert.deepEqual(r, { kind: "X", message: "failed at step [3] of run" });
});

test("empty kind falls back to 'error'", () => {
  assert.deepEqual(parseErrorMarker("[error: ] boom"), { kind: "error", message: "boom" });
});

test("isErrorMarker only matches the marker prefix", () => {
  assert.equal(isErrorMarker("[error: X] y"), true);
  assert.equal(isErrorMarker("hello world"), false);
  assert.equal(isErrorMarker("the [error: X] was mid-sentence"), false);
  assert.equal(isErrorMarker(null), false);
  assert.equal(isErrorMarker(undefined), false);
  assert.equal(isErrorMarker(123), false);
});

test("non-markers return null", () => {
  assert.equal(parseErrorMarker("just a normal message"), null);
  assert.equal(parseErrorMarker(""), null);
  assert.equal(parseErrorMarker(42), null);
});
