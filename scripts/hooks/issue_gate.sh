#!/usr/bin/env bash
# Issue-first change gate (Claude Code hooks; see AGENTS.md §"Issue-first changes").
#
# Two modes, both reading the hook JSON payload on stdin:
#
#   log-prompt    UserPromptSubmit: append the prompt to this session's log.
#   ensure-issue  PreToolUse (Edit|Write|NotebookEdit): before the FIRST file
#                 modification inside the repo, create a GitHub issue carrying
#                 the session's prompts; on later modifications, sync any new
#                 prompts to the issue as a comment.
#
# Policy (operator requirement):
#   - gh authenticated + issue creation succeeds -> proceed (issue url shown).
#   - gh authenticated but creation FAILS       -> exit 2 = BLOCK the edit.
#   - gh missing/unauthenticated                -> allow silently (noted once).
#
# Only paths inside the repo are gated: scratchpads, plan files, and memory
# writes are none of the tracker's business. State lives under the system temp
# dir, keyed by session id, so parallel sessions get their own issues.
set -u

MODE="${1:-}"
INPUT="$(cat)"

command -v jq >/dev/null 2>&1 || exit 0 # cannot parse the payload: never block

SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"')"
STATE_DIR="${TMPDIR:-/tmp}/team-issue-gate/${SESSION_ID}"
mkdir -p "$STATE_DIR"
PROMPTS="$STATE_DIR/prompts.log"
ISSUE_FILE="$STATE_DIR/issue"
SKIP_FILE="$STATE_DIR/skipped"
SYNC_FILE="$STATE_DIR/synced_lines"

case "$MODE" in
log-prompt)
  {
    printf '%s' "$INPUT" | jq -r '.prompt // empty'
    printf -- '\n---\n'
  } >>"$PROMPTS"
  exit 0
  ;;
ensure-issue) ;;
*)
  echo "usage: issue_gate.sh {log-prompt|ensure-issue}" >&2
  exit 0
  ;;
esac

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')"
case "$FILE_PATH" in
"$REPO_ROOT"/*) ;;
*) exit 0 ;; # outside the repo: not the tracker's business
esac

[ -f "$SKIP_FILE" ] && exit 0 # session already noted as gh-less

if [ -f "$ISSUE_FILE" ]; then
  # Issue exists: best-effort sync of prompts that arrived since, never block.
  TOTAL=$(wc -l <"$PROMPTS" 2>/dev/null | tr -d ' ' || echo 0)
  SYNCED=$(cat "$SYNC_FILE" 2>/dev/null || echo 0)
  if [ "${TOTAL:-0}" -gt "${SYNCED:-0}" ]; then
    NEW=$(tail -n +"$((SYNCED + 1))" "$PROMPTS")
    if gh issue comment "$(cat "$ISSUE_FILE")" --body "Additional prompt(s) in this session:

\`\`\`
$NEW
\`\`\`" >/dev/null 2>&1; then
      echo "$TOTAL" >"$SYNC_FILE"
    fi
  fi
  exit 0
fi

if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  touch "$SKIP_FILE"
  printf '{"systemMessage":"issue-gate: gh unavailable/unauthenticated — proceeding WITHOUT a tracking issue for this session."}\n'
  exit 0
fi

BRANCH=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "?")
PROMPT_TEXT=$(cat "$PROMPTS" 2>/dev/null || true)
FIRST_LINE=$(printf '%s' "$PROMPT_TEXT" | grep -m1 -vE '^[[:space:]]*$' | cut -c1-80)
TITLE="${FIRST_LINE:-Agent change session on $BRANCH ($(date +%Y-%m-%d))}"
BODY="Auto-filed by the issue-first change gate before this session's first file modification.

- branch: \`$BRANCH\`
- session: \`$SESSION_ID\`
- first gated file: \`$FILE_PATH\`

## Prompts so far

\`\`\`
${PROMPT_TEXT:-(none captured — session predates the prompt log)}
\`\`\`"

gh label create agent-session --color BFD4F2 \
  --description "Auto-filed by the issue-first change gate" >/dev/null 2>&1 || true
URL=$(gh issue create --title "$TITLE" --body "$BODY" --label agent-session 2>/dev/null) ||
  URL=$(gh issue create --title "$TITLE" --body "$BODY" 2>/dev/null) || URL=""
if [ -z "$URL" ]; then
  echo "issue-first gate: gh is authenticated but 'gh issue create' failed — refusing to \
modify repo files without a tracking issue. Fix gh (network? scopes?) or create the issue \
manually with 'gh issue create', then retry." >&2
  exit 2
fi
NUM=$(printf '%s' "$URL" | grep -oE '[0-9]+$' || true)
printf '%s' "${NUM:-0}" >"$ISSUE_FILE"
wc -l <"$PROMPTS" 2>/dev/null | tr -d ' ' >"$SYNC_FILE" || echo 0 >"$SYNC_FILE"
printf '{"systemMessage":"issue-gate: filed issue #%s for this session — %s"}\n' "${NUM:-?}" "$URL"
exit 0
