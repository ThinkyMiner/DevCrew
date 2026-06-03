// Logic test for the persona system-prompt boilerplate — run: node --test tests/js/
//
// The persona editor pre-fills a research-backed scaffold into the system-prompt
// box on every NEW persona (the operator then edits it). The scaffold lives in a
// DOM-free module so it is testable here and reusable. We assert it carries the
// layered structure (Role → Objective → How you operate → Output style →
// Constraints) that the prompt-design research settled on, so a future edit
// can't silently gut it.

import { test } from "node:test";
import assert from "node:assert/strict";
import { PERSONA_PROMPT_BOILERPLATE } from "../../app/web/js/persona_templates.js";

test("boilerplate is a non-trivial string", () => {
  assert.equal(typeof PERSONA_PROMPT_BOILERPLATE, "string");
  assert.ok(PERSONA_PROMPT_BOILERPLATE.length > 200, "should be a real scaffold, not a stub");
});

test("boilerplate carries every layered section heading", () => {
  for (const heading of ["# Role", "# Objective", "# How you operate", "# Output style", "# Constraints"]) {
    assert.ok(
      PERSONA_PROMPT_BOILERPLATE.includes(heading),
      `missing section heading: ${heading}`
    );
  }
});

test("sections appear in the recommended order", () => {
  const order = ["# Role", "# Objective", "# How you operate", "# Output style", "# Constraints"];
  let cursor = -1;
  for (const heading of order) {
    const at = PERSONA_PROMPT_BOILERPLATE.indexOf(heading);
    assert.ok(at > cursor, `${heading} is out of order`);
    cursor = at;
  }
});
