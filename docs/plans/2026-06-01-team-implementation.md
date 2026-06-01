# Team Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local, Python web group-chat app where one human collaborates with AI personas, each backed by a resumable Claude Code / Codex CLI harness session, with a shared transcript, quote-replies, weighted authors (Me/Boss), per-message sequential/parallel replies, full streaming transparency, and durable restartable sessions.

**Architecture:** Inward-pointing layers — `domain` (pure Pydantic models + normalized `StreamEvent`), `harness` (the only subprocess layer: `ClaudeHarness`/`CodexHarness`/`MockHarness` behind one `AgentBackend` protocol), `persistence` (stdlib `sqlite3` repos + on-disk run logs), `services` (orchestration: routing, delta/quote/weight prompt assembly, sequential/parallel), `api` (FastAPI REST + WebSocket), `web` (buildless vanilla-JS frontend). A persona is one resumable harness session **per room**; the shared transcript is reconstructed via delta + quote injection.

**Tech Stack:** Python 3.12+ (local 3.14), FastAPI, uvicorn, Pydantic v2, pydantic-settings, stdlib `sqlite3`, asyncio subprocesses, buildless ES-module JS + WebSocket. Tooling: pytest, pytest-asyncio, ruff, mypy. **`MockHarness` is the default test backend — no real CLI, no token spend, ever, in unit/CI tests.**

**Read before starting:** [`goal.md`](../../goal.md), [`docs/PRD.md`](../PRD.md), [`docs/plans/2026-06-01-team-chat-design.md`](2026-06-01-team-chat-design.md), [`AGENTS.md`](../../AGENTS.md). Obey AGENTS.md §4 (fail loud/located, typed boundaries, one `run_id`) and §5 (TDD mandatory).

**Conventions for every task below:**
- Write the test first; run it; watch it fail for the *expected* reason; implement the minimum; run it; watch it pass; commit.
- Exact commands: `pytest <path> -v`, then `ruff check . && ruff format . && mypy app`, then `git add -A && git commit`.
- Commit messages use Conventional Commits. End each commit body with the `Co-Authored-By` trailer from AGENTS.md is **not** required for the engineer; keep messages clean.

---

## Phase 0 — Scaffolding & quality gates

### Task 0.1: `pyproject.toml` with deps + tool config

**Files:**
- Create: `pyproject.toml`

**Step 1: Write the file**

```toml
[project]
name = "team"
version = "0.1.0"
description = "Local group chat with AI personas backed by Claude/Codex CLI harnesses"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",          # FastAPI TestClient / async client
    "ruff>=0.5",
    "mypy>=1.10",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["app"]
```

**Step 2: Set up env & verify install**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```
Expected: clean install. If a wheel is missing on 3.14, pin that dep down a minor version and note it in the commit.

**Step 3: Commit**

```bash
git add pyproject.toml && git commit -m "build: project metadata, deps, ruff/mypy/pytest config"
```

### Task 0.2: Package skeleton + smoke test

**Files:**
- Create: `app/__init__.py`, `app/domain/__init__.py`, `app/harness/__init__.py`, `app/services/__init__.py`, `app/persistence/__init__.py`, `app/api/__init__.py`, `app/config/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`
- Create: `tests/unit/test_smoke.py`

**Step 1: Write failing test**

```python
# tests/unit/test_smoke.py
def test_app_package_imports():
    import app
    assert app is not None
```

**Step 2: Run** `pytest tests/unit/test_smoke.py -v` → FAIL (no `app` package).

**Step 3:** Create the empty `__init__.py` files listed above.

**Step 4: Run** `pytest tests/unit/test_smoke.py -v` → PASS.

**Step 5: Commit** `git add -A && git commit -m "chore: package skeleton + smoke test"`

### Task 0.3: Structured logging with run_id correlation

**Files:**
- Create: `app/config/logging.py`
- Test: `tests/unit/test_logging.py`

**Step 1: Write failing test**

```python
# tests/unit/test_logging.py
import json
import logging
from app.config.logging import configure_logging, run_id_var

def test_log_line_is_json_with_run_id(capsys):
    configure_logging(json_to_stderr=True)
    token = run_id_var.set("run-123")
    try:
        logging.getLogger("team.test").info("hello", extra={"event": "unit"})
    finally:
        run_id_var.reset(token)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["run_id"] == "run-123"
    assert payload["event"] == "unit"
```

**Step 2: Run** → FAIL (module missing).

**Step 3: Implement**

```python
# app/config/logging.py
from __future__ import annotations
import json, logging, sys
from contextvars import ContextVar

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": run_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

def configure_logging(*, json_to_stderr: bool = True, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_to_stderr else logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
```

**Step 4: Run** → PASS. Then `ruff check . && mypy app`.

**Step 5: Commit** `git add -A && git commit -m "feat(config): structured JSON logging with run_id contextvar"`

---

## Phase 1 — Domain models (pure, no I/O)

### Task 1.1: `StreamEvent` normalized union

**Files:**
- Create: `app/domain/events.py`
- Test: `tests/unit/domain/test_events.py` (+ `tests/unit/domain/__init__.py`)

**Step 1: Write failing test**

```python
# tests/unit/domain/test_events.py
from app.domain.events import (
    TextDelta, ThinkingDelta, ToolUse, ToolResult, Usage, RunError, RunDone, parse_event,
)

def test_text_delta_roundtrips():
    e = TextDelta(text="hi")
    assert e.kind == "text"
    assert parse_event(e.model_dump()) == e

def test_parse_dispatches_on_kind():
    assert isinstance(parse_event({"kind": "thinking", "text": "..."}), ThinkingDelta)
    assert isinstance(parse_event({"kind": "tool_use", "name": "grep", "input": {}}), ToolUse)
    assert isinstance(parse_event({"kind": "usage", "input_tokens": 1, "output_tokens": 2}), Usage)
    assert isinstance(parse_event({"kind": "error", "error_kind": "HarnessError", "message": "x"}), RunError)
    assert isinstance(parse_event({"kind": "done", "session_id": "s1"}), RunDone)
```

**Step 2: Run** → FAIL.

**Step 3: Implement** — a discriminated union keyed on `kind`. Each persona run emits a sequence of these; the harness layer normalizes both CLIs into them.

```python
# app/domain/events.py
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field, TypeAdapter

class TextDelta(BaseModel):
    kind: Literal["text"] = "text"
    text: str

class ThinkingDelta(BaseModel):
    kind: Literal["thinking"] = "thinking"
    text: str

class ToolUse(BaseModel):
    kind: Literal["tool_use"] = "tool_use"
    name: str
    input: dict = Field(default_factory=dict)
    tool_id: str | None = None

class ToolResult(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    tool_id: str | None = None
    content: str = ""
    is_error: bool = False

class Usage(BaseModel):
    kind: Literal["usage"] = "usage"
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int | None = None  # powers the per-persona context indicator (FR-C3)

class RunError(BaseModel):
    kind: Literal["error"] = "error"
    error_kind: str
    message: str

class RunDone(BaseModel):
    kind: Literal["done"] = "done"
    session_id: str | None = None  # harness session id to persist for resume

StreamEvent = Annotated[
    Union[TextDelta, ThinkingDelta, ToolUse, ToolResult, Usage, RunError, RunDone],
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)

def parse_event(data: dict) -> StreamEvent:
    return _ADAPTER.validate_python(data)
```

**Step 4: Run** → PASS. `ruff check . && mypy app`.

**Step 5: Commit** `git add -A && git commit -m "feat(domain): normalized StreamEvent union"`

### Task 1.2: Core entities

**Files:**
- Create: `app/domain/models.py`
- Test: `tests/unit/domain/test_models.py`

**Step 1: Write failing test**

```python
# tests/unit/domain/test_models.py
import pytest
from pydantic import ValidationError
from app.domain.models import (
    Provider, PermissionMode, ReplyMode, Persona, HumanAuthor, Room, Message, AuthorKind,
)

def test_persona_handle_normalised_and_validated():
    p = Persona(name="Architect", handle="Architect", provider=Provider.CLAUDE, model="opus")
    assert p.handle == "architect"           # lowercased, no leading @
    with pytest.raises(ValidationError):
        Persona(name="x", handle="has space", provider=Provider.CLAUDE, model="m")

def test_boss_author_weight_enabled_by_default():
    boss = HumanAuthor(name="Boss", weight_note="Leadership input — weight heavily.")
    assert boss.weight_enabled is True

def test_message_requires_known_author_kind():
    m = Message(room_id="r1", author_kind=AuthorKind.HUMAN, author_ref="me", content="hi")
    assert m.run_id is None
```

**Step 2: Run** → FAIL.

**Step 3: Implement** — IDs are app-generated strings (use `uuid4().hex`; do not import `uuid` into pure logic that needs determinism — generate IDs in services, default-factory here is fine for models). Handle validator strips a leading `@`, lowercases, and rejects whitespace.

```python
# app/domain/models.py
from __future__ import annotations
import re
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4
from pydantic import BaseModel, Field, field_validator

def _id() -> str: return uuid4().hex
def _now() -> datetime: return datetime.now(timezone.utc)

class Provider(str, Enum): CLAUDE = "claude"; CODEX = "codex"; MOCK = "mock"
class PermissionMode(str, Enum): READ_ONLY = "read-only"; ASK = "ask"; AUTO = "auto"
class ReplyMode(str, Enum): SEQUENTIAL = "sequential"; PARALLEL = "parallel"
class AuthorKind(str, Enum): HUMAN = "human"; PERSONA = "persona"

_HANDLE_RE = re.compile(r"^[a-z0-9_-]+$")

class Persona(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    handle: str
    color: str = "#6aa0ff"
    provider: Provider
    model: str
    effort: str | None = None                 # maps to claude --effort / codex reasoning effort
    system_prompt: str = ""
    mcp_servers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    is_template: bool = False
    created_at: datetime = Field(default_factory=_now)

    @field_validator("handle")
    @classmethod
    def _norm_handle(cls, v: str) -> str:
        v = v.lstrip("@").strip().lower()
        if not _HANDLE_RE.match(v):
            raise ValueError("handle must match [a-z0-9_-]+ (no spaces)")
        return v

class HumanAuthor(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    color: str = "#9ee37d"
    weight_note: str = ""
    weight_enabled: bool = True

class Room(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    topic: str = ""
    default_reply_mode: ReplyMode = ReplyMode.SEQUENTIAL
    archived: bool = False
    created_at: datetime = Field(default_factory=_now)

class Message(BaseModel):
    id: str = Field(default_factory=_id)
    room_id: str
    author_kind: AuthorKind
    author_ref: str                  # human_author.id or persona.id
    content: str
    quoted_message_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_now)

class PersonaSession(BaseModel):
    room_id: str
    persona_id: str
    provider: Provider
    harness_session_id: str | None = None
    last_seen_message_id: str | None = None
    status: str = "idle"

class RunRecord(BaseModel):
    run_id: str = Field(default_factory=_id)
    room_id: str
    persona_id: str
    command_redacted: str
    exit_code: int | None = None
    usage: dict = Field(default_factory=dict)
    log_path: str | None = None
    error_kind: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
```

**Step 4: Run** → PASS. `ruff check . && mypy app`.

**Step 5: Commit** `git add -A && git commit -m "feat(domain): core entities (persona, author, room, message, session, run)"`

### Task 1.3: Error taxonomy

**Files:**
- Create: `app/domain/errors.py`
- Test: `tests/unit/domain/test_errors.py`

**Step 1: Write failing test**

```python
# tests/unit/domain/test_errors.py
from app.domain.errors import (
    TeamError, HarnessError, HarnessTimeout, HarnessAuthError, SessionNotFound, ProviderUnavailable,
)
def test_hierarchy_and_kind():
    for exc in (HarnessError, HarnessTimeout, HarnessAuthError, SessionNotFound, ProviderUnavailable):
        assert issubclass(exc, TeamError)
    assert HarnessTimeout("x").kind == "HarnessTimeout"
```

**Step 2: Run** → FAIL.

**Step 3: Implement**

```python
# app/domain/errors.py
from __future__ import annotations
class TeamError(Exception):
    @property
    def kind(self) -> str: return type(self).__name__
class HarnessError(TeamError): ...
class HarnessTimeout(HarnessError): ...
class HarnessAuthError(HarnessError): ...
class SessionNotFound(TeamError): ...
class ProviderUnavailable(TeamError): ...
```

**Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(domain): typed error taxonomy"`

---

## Phase 2 — Persistence (stdlib sqlite3)

### Task 2.1: Connection + schema

**Files:**
- Create: `app/persistence/db.py`, `app/persistence/schema.sql`
- Test: `tests/unit/persistence/test_db.py` (+ `__init__.py`)

**Step 1: Write failing test**

```python
# tests/unit/persistence/test_db.py
from app.persistence.db import Database

def test_schema_creates_all_tables(tmp_path):
    db = Database(tmp_path / "t.db"); db.init_schema()
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"persona","human_author","room","room_persona","message",
            "message_quote","persona_session","run_record"} <= tables

def test_foreign_keys_enabled(tmp_path):
    db = Database(tmp_path / "t.db"); db.init_schema()
    assert db.query("PRAGMA foreign_keys")[0]["foreign_keys"] == 1
```

**Step 2: Run** → FAIL.

**Step 3: Implement** `db.py` (thin typed wrapper: `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, `query`/`execute`/`executemany`/`transaction` helpers) and `schema.sql` mirroring PRD §7 (all columns, JSON stored as TEXT, FKs with `ON DELETE` rules, `message_quote(message_id, quoted_message_id)`, `persona_session` PK `(room_id, persona_id)`). Store datetimes as ISO strings, JSON via `json.dumps`.

```python
# app/persistence/db.py
from __future__ import annotations
import sqlite3
from pathlib import Path
from collections.abc import Iterable, Sequence
from contextlib import contextmanager

class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        sql = (Path(__file__).parent / "schema.sql").read_text()
        self._conn.executescript(sql); self._conn.commit()

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

    def execute(self, sql: str, params: Sequence = ()) -> None:
        self._conn.execute(sql, params); self._conn.commit()

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        self._conn.executemany(sql, rows); self._conn.commit()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn; self._conn.commit()
        except Exception:
            self._conn.rollback(); raise
```

**Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(persistence): sqlite Database wrapper + schema"`

### Task 2.2: Repositories

**Files:**
- Create: `app/persistence/repositories.py`
- Test: `tests/unit/persistence/test_repositories.py`

Build one repo per aggregate: `PersonaRepo`, `AuthorRepo`, `RoomRepo` (incl. membership in `room_persona`), `MessageRepo` (incl. `message_quote` writes + ordered fetch), `SessionRepo` (upsert by `(room_id, persona_id)`), `RunRepo`. Each repo takes a `Database` and converts `sqlite3.Row` ⇄ the `domain.models` Pydantic objects (JSON columns via `json.loads/dumps`).

**TDD loop per repo:** write a test that creates → fetches → asserts equality, plus list/update/delete, plus one edge (e.g. `MessageRepo.list_for_room` returns chronological order; `SessionRepo.upsert` overwrites; deleting a room cascades). Implement the minimum to pass. Representative test:

```python
# tests/unit/persistence/test_repositories.py (excerpt)
def test_message_repo_orders_chronologically(db):
    rooms, msgs = RoomRepo(db), MessageRepo(db)
    r = rooms.create(Room(name="A"))
    a = msgs.create(Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="1"))
    b = msgs.create(Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="2"))
    got = msgs.list_for_room(r.id)
    assert [m.id for m in got] == [a.id, b.id]

def test_message_quotes_persist(db):
    ...  # create message with quoted_message_ids, refetch, assert preserved
```

Add a `conftest.py` providing a `db` fixture (`Database(tmp_path/"t.db")` + `init_schema()`).

**Commit per repo or per logical group:** `git commit -am "feat(persistence): <X>Repo with tests"`

### Task 2.3: Run-log store (filesystem JSONL)

**Files:**
- Create: `app/persistence/run_log.py`
- Test: `tests/unit/persistence/test_run_log.py`

**Behavior:** `RunLogStore(base_dir).open(room_id, persona_id, run_id)` returns a writer whose `.write_event(StreamEvent)` appends one JSON line, and `.path` is `base_dir/<room>/<persona>/<ISO-ts>-<run_id>.jsonl`. Timestamp comes from an injected clock (default `datetime.now`) so tests are deterministic. Test: write 3 events → file has 3 lines → each parses back via `parse_event`.

**Commit** `git commit -am "feat(persistence): per-run JSONL log store"`

---

## Phase 3 — Harness layer (the only subprocess layer)

### Task 3.1: `AgentBackend` protocol + `RunSpec`

**Files:**
- Create: `app/harness/base.py`
- Test: `tests/unit/harness/test_base.py`

**Step 1: Write failing test** — assert `RunSpec` carries everything an adapter needs and that a trivial class satisfying the protocol type-checks at runtime via `isinstance` against a `runtime_checkable` Protocol.

```python
# tests/unit/harness/test_base.py
from app.harness.base import RunSpec, AgentBackend
from app.domain.models import Provider, PermissionMode

def test_runspec_holds_persona_config():
    spec = RunSpec(prompt="hi", model="opus", provider=Provider.CLAUDE,
                   resume_session_id=None, system_prompt="be terse",
                   effort="high", mcp_servers=[], allowed_tools=[],
                   working_dir=None, permission_mode=PermissionMode.READ_ONLY)
    assert spec.prompt == "hi"

def test_protocol_is_runtime_checkable():
    class Dummy:
        name = "dummy"; supported_commands = set()
        async def run(self, spec): yield  # pragma: no cover
        async def send_command(self, session_id, command): yield  # pragma: no cover
    assert isinstance(Dummy(), AgentBackend)
```

**Step 3: Implement**

```python
# app/harness/base.py
from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable
from pydantic import BaseModel, Field
from app.domain.events import StreamEvent
from app.domain.models import Provider, PermissionMode

class RunSpec(BaseModel):
    prompt: str
    provider: Provider
    model: str
    system_prompt: str = ""
    effort: str | None = None
    resume_session_id: str | None = None
    mcp_servers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY

@runtime_checkable
class AgentBackend(Protocol):
    name: str
    supported_commands: set[str]
    def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]: ...
    def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]: ...
```

**Commit** `git commit -am "feat(harness): AgentBackend protocol + RunSpec"`

### Task 3.2: `MockHarness` (the test backbone)

**Files:**
- Create: `app/harness/mock.py`
- Test: `tests/unit/harness/test_mock.py`

**Behavior:** deterministic, scriptable. Constructed with an optional script (a dict `prompt-substring → list[StreamEvent]`) or defaults to echoing: emits a couple of `TextDelta`s referencing the prompt, a `Usage`, then `RunDone(session_id=...)`. It records every `RunSpec` it received (so tests assert on prompt assembly). `supported_commands = {"/compact", "/clear"}`; `send_command` emits a `TextDelta("[compacted]")` + `RunDone`. Generates session ids deterministically (e.g. `f"mock-{n}"`).

```python
# tests/unit/harness/test_mock.py
import pytest
from app.harness.mock import MockHarness
from app.harness.base import RunSpec
from app.domain.models import Provider
from app.domain.events import RunDone

@pytest.mark.asyncio
async def test_mock_emits_text_and_done_and_records_spec():
    h = MockHarness()
    spec = RunSpec(prompt="hello world", provider=Provider.MOCK, model="m")
    events = [e async for e in h.run(spec)]
    assert any(getattr(e, "text", "").strip() for e in events)
    assert isinstance(events[-1], RunDone) and events[-1].session_id
    assert h.calls[0].prompt == "hello world"

@pytest.mark.asyncio
async def test_mock_resumes_same_session():
    h = MockHarness()
    s1 = [e async for e in h.run(RunSpec(prompt="a", provider=Provider.MOCK, model="m"))][-1].session_id
    s2 = [e async for e in h.run(RunSpec(prompt="b", provider=Provider.MOCK, model="m", resume_session_id=s1))][-1].session_id
    assert s2 == s1
```

Implement `mock.py` to satisfy these. **Commit** `git commit -am "feat(harness): MockHarness deterministic backend for tests"`

### Task 3.3: Claude stream-json parser (fixtures, no live calls)

**Files:**
- Create: `app/harness/claude_parser.py`
- Create fixtures: `tests/fixtures/claude_stream_basic.jsonl` (hand-author a realistic `claude --output-format stream-json` capture: a `system`/init line carrying `session_id`, `assistant` lines with `text` and `thinking` blocks, a `tool_use`, a `tool_result`, a final `result` line with usage)
- Test: `tests/unit/harness/test_claude_parser.py`

**Behavior:** `parse_claude_line(obj: dict) -> list[StreamEvent]` maps each raw stream-json object to zero or more normalized `StreamEvent`s; capture `session_id` from the init/system line and surface it on the terminal `RunDone`. Test feeds each fixture line and asserts the normalized sequence (text/thinking/tool_use/tool_result/usage/done with the right `session_id`).

> Schema note: confirm the exact field names against a real capture during Task 3.4; keep mapping in this one function so drift is a one-file fix (AGENTS.md §8). Add a fixture line that's unknown → parser yields `[]` (ignored, not crash).

**Commit** `git commit -am "feat(harness): claude stream-json parser + fixtures"`

### Task 3.4: `ClaudeHarness` adapter

**Files:**
- Create: `app/harness/claude.py`
- Test: `tests/unit/harness/test_claude_harness.py`

**Step 1: Write failing test** — inject a fake subprocess runner so no real CLI is spawned. The adapter takes a `spawn` callable (default = real asyncio subprocess) returning an async line iterator + a wait()-able exit code. Test passes a fake that yields the fixture lines and exit 0; assert the adapter (a) builds the expected argv, (b) yields normalized events, (c) raises `HarnessError` on nonzero exit, `HarnessTimeout` on timeout.

```python
# tests/unit/harness/test_claude_harness.py (excerpt)
@pytest.mark.asyncio
async def test_builds_resume_and_model_args(fixture_lines):
    captured = {}
    async def fake_spawn(argv, cwd):
        captured["argv"] = argv
        return FakeProc(fixture_lines, exit_code=0)
    h = ClaudeHarness(spawn=fake_spawn)
    spec = RunSpec(prompt="hi", provider=Provider.CLAUDE, model="opus",
                   resume_session_id="sess-1", effort="high", system_prompt="be terse")
    _ = [e async for e in h.run(spec)]
    argv = captured["argv"]
    assert "--print" in argv and "--output-format" in argv and "stream-json" in argv
    assert "--resume" in argv and "sess-1" in argv
    assert "--model" in argv and "opus" in argv
    assert "--append-system-prompt" in argv and "--effort" in argv

@pytest.mark.asyncio
async def test_nonzero_exit_raises_harness_error(fixture_lines):
    async def fake_spawn(argv, cwd): return FakeProc(fixture_lines, exit_code=2, stderr="boom")
    with pytest.raises(HarnessError):
        _ = [e async for e in ClaudeHarness(spawn=fake_spawn).run(_basic_spec())]
```

**Step 3: Implement** `claude.py`:
- Build argv: `["claude", "--print", "--output-format", "stream-json", "--verbose", "--model", spec.model]`; add `--resume <id>` if resuming; `--append-system-prompt <sp>` if set; `--effort <e>` if set; `--permission-mode <mode>`; `--add-dir <wd>` and set cwd if `working_dir`; `--mcp-config <json>` if `mcp_servers` (write a temp MCP config, or pass through — keep behind a helper); the prompt is the final positional arg.
- Spawn via injected `spawn`; iterate stdout lines → `json.loads` → `parse_claude_line` → yield events.
- On nonzero exit, yield/raise `HarnessError` carrying stderr (redacted). On timeout, `HarnessTimeout`.
- `supported_commands = {"/compact", "/clear"}`; `send_command` resumes the session with the command string as the prompt (verify mechanics — OQ-1; if unsupported, raise with a clear message).

**Step 4/5:** Run, lint, type, commit `git commit -am "feat(harness): ClaudeHarness adapter with injectable spawn"`

### Task 3.5: Codex parser + `CodexHarness`

**Files:**
- Create: `app/harness/codex_parser.py`, `app/harness/codex.py`
- Fixtures: `tests/fixtures/codex_stream_basic.jsonl`
- Tests: `tests/unit/harness/test_codex_parser.py`, `test_codex_harness.py`

Same shape as 3.3–3.4 but for `codex exec` (resume flag + JSON event mode). Normalize Codex's event names into the shared `StreamEvent`. Inject `spawn`. Mark anything uncertain in the schema with a `# TODO(OQ-3): verify against live capture` and keep it isolated to the parser. **Commit** `git commit -am "feat(harness): CodexHarness adapter + parser + fixtures"`

### Task 3.6: Backend registry

**Files:**
- Create: `app/harness/registry.py`
- Test: `tests/unit/harness/test_registry.py`

`get_backend(provider: Provider) -> AgentBackend`, with a way to register the `MockHarness` for `Provider.MOCK` and in tests. Raise `ProviderUnavailable` for unknown providers. **Commit** `git commit -am "feat(harness): backend registry"`

---

## Phase 4 — Services / orchestration (the heart)

### Task 4.1: Transcript delta builder

**Files:**
- Create: `app/services/transcript.py`
- Test: `tests/unit/services/test_transcript.py`

**Behavior:** `build_delta(messages, last_seen_id, *, persona_handle, name_of)` returns the attributed text block of messages strictly after `last_seen_id` (all of them if `None`), formatted `[<author display>]: <content>` and, when a message targets this persona, `[<author> → @<handle>]:`. `name_of(author_kind, author_ref) -> display` is injected so the builder stays pure. Tests cover: empty delta when nothing new; correct slice after pointer; attribution format; persona's own past messages are still shown (so it sees what it said). 

```python
# tests/unit/services/test_transcript.py (excerpt)
def test_delta_after_pointer_only():
    msgs = [_human("m1","hi"), _persona("m2","p1","yo"), _human("m3","next")]
    out = build_delta(msgs, last_seen_id="m1", persona_handle="arch", name_of=_names)
    assert "hi" not in out and "yo" in out and "next" in out
```

**Commit** `git commit -am "feat(services): transcript delta builder"`

### Task 4.2: Quote injection

**Files:**
- Modify: `app/services/transcript.py`
- Test: extend `test_transcript.py`

**Behavior:** `render_quotes(quoted_messages, name_of)` returns a block formatting each quoted message as a `>`-prefixed blockquote with attribution, regardless of seen-state. Test asserts format and that an empty list → empty string. **Commit** `git commit -am "feat(services): quote rendering for context injection"`

### Task 4.3 + 4.4: Prompt assembly (delta + quotes + author weighting)

**Files:**
- Create: `app/services/prompt.py`
- Test: `tests/unit/services/test_prompt.py`

**Behavior:** `assemble_prompt(*, delta, quotes, author, new_text, persona_handle)` composes the final prompt string a persona receives:
1. If `author` is a `HumanAuthor` with `weight_enabled` and a `weight_note`, prepend it as a marked **[Author note]** block.
2. The quotes block (if any).
3. The delta block.
4. The new directed line `[<author> → @<handle>]: <new_text>`.

Tests: Boss weight note appears only when enabled; ordering is weight → quotes → delta → new line; with no quotes/delta the prompt is just the directed line (+ optional weight). **Commit** `git commit -am "feat(services): prompt assembly with author weighting"`

### Task 4.5: Routing

**Files:**
- Create: `app/services/routing.py`
- Test: `tests/unit/services/test_routing.py`

**Behavior:** `resolve_targets(text, room_personas) -> list[Persona]` parses `@handle` mentions and `@everyone`. Unknown handles are ignored (not errors). No mentions → `[]` (FR-M3: message stored as context, no run). Preserves room order for `@everyone` and mention order otherwise; de-dupes. Tests cover each case. **Commit** `git commit -am "feat(services): @tag / @everyone routing"`

### Task 4.6: `ChatOrchestrator` (sequential + parallel)

**Files:**
- Create: `app/services/orchestrator.py`
- Test: `tests/unit/services/test_orchestrator.py` (uses `MockHarness` + in-memory/temp repos — **no real CLI**)

**Behavior:** `post_message(room_id, author, text, *, reply_mode, tagged_override=None, quoted_ids=())`:
1. Persist the human message (with quotes).
2. Resolve targets (Task 4.5) over room personas.
3. For each target persona, load its `PersonaSession`; build delta (after `last_seen_id`) + quotes + prompt (Tasks 4.1–4.4); construct `RunSpec` (resume with stored `harness_session_id`); start a `RunRecord` + open a `RunLogStore` writer; run the backend; collect/stream events; on `RunDone` persist new `session_id`; persist the persona's reply `Message`; advance the persona's `last_seen_id` to the latest message; finalize the `RunRecord`.
4. **Sequential:** do targets in order, and after each reply, the *next* persona's delta includes it (re-query messages). **Parallel:** snapshot the message list once and run targets concurrently against that snapshot via `asyncio.gather`; advance each pointer to the snapshot tail.
5. Yield a stream of `(persona_id, StreamEvent)` so the API can fan out to WebSocket.
6. On backend error: emit a `RunError` event, write `error_kind` to the `RunRecord`, persist an error placeholder message — never swallow (AGENTS.md §4).

**Tests (all via `MockHarness`):**
- Untagged message → zero runs, message stored.
- `@arch` → one run; persona reply persisted; `harness_session_id` saved; pointer advanced.
- Second message resumes the same session id (assert the `RunSpec.resume_session_id` the mock recorded).
- Sequential: tag `@a @b`; script the mock so `b`'s recorded prompt **contains `a`'s reply text** (proves same-turn cross-talk).
- Parallel: tag `@a @b`; assert neither recorded prompt contains the other's reply.
- Quote: post quoting an earlier message; assert the targeted persona's recorded prompt contains the quoted text as a blockquote.
- Boss weighting: author=Boss(enabled) → recorded prompt contains the weight note; disabled → it doesn't.
- Backend raises → `RunError` emitted, `RunRecord.error_kind` set, no crash.

```python
# tests/unit/services/test_orchestrator.py (excerpt)
@pytest.mark.asyncio
async def test_sequential_b_sees_a_reply(orchestrator, room, a, b, mock):
    mock.script_reply(a.handle, "ALPHA-SAYS-X")
    events = [ev async for ev in orchestrator.post_message(
        room.id, author=ME, text=f"@{a.handle} @{b.handle} discuss", reply_mode=ReplyMode.SEQUENTIAL)]
    b_prompt = mock.last_prompt_for(b.id)
    assert "ALPHA-SAYS-X" in b_prompt
```

**Commit** `git commit -am "feat(services): ChatOrchestrator sequential+parallel with delta/quote/weight"`

### Task 4.7: Service facades + `SessionStore` lifecycle

**Files:**
- Create: `app/services/personas.py`, `app/services/rooms.py`, `app/services/authors.py`, `app/services/session_store.py`
- Tests: one per service

CRUD facades wrapping the repos (validation, default Boss/Me authors seeded on first run via `AuthorService.ensure_defaults()`), and `SessionStore` to track live runs and expose reset (FR-C4). TDD each. **Commit** per service.

### Task 4.8: Control commands (compact/clear)

**Files:**
- Modify: `app/services/orchestrator.py`
- Test: extend `test_orchestrator.py`

**Behavior:** `send_control(room_id, persona_id, command)` → checks `backend.supported_commands`; if unsupported raise/emit a clear error (FR-C2); else resume the session, stream events, log a `RunRecord`. Test via `MockHarness` (`/compact` supported → events; `/bogus` → clear error). **Commit** `git commit -am "feat(services): persona session control commands"`

---

## Phase 5 — API + WebSocket

### Task 5.1: App factory + health check

**Files:**
- Create: `app/config/settings.py`, `app/api/app_factory.py`, `app/api/health.py`
- Test: `tests/unit/api/test_health.py`

`Settings` (pydantic-settings): host/port, data dir, db path, `claude_bin`, `codex_bin`. `health` verifies both CLIs resolve on PATH and (best-effort) auth, returning a structured report. App factory wires settings → db → repos → services → routers (composition root). Test the health endpoint with both CLIs faked present/absent. **Commit** `git commit -am "feat(api): app factory, settings, health check"`

### Task 5.2: REST routes (CRUD)

**Files:**
- Create: `app/api/routes_personas.py`, `routes_authors.py`, `routes_rooms.py`, `routes_messages.py`
- Test: `tests/integration/test_rest.py` (FastAPI `TestClient`)

CRUD for personas/authors/rooms; rooms include membership; messages list endpoint (paginated, chronological) and a run-log fetch endpoint (`GET /runs/{run_id}/log` → streams the JSONL). TDD with `TestClient`. **Commit** per resource or grouped.

### Task 5.3: WebSocket streaming endpoint

**Files:**
- Create: `app/api/ws.py`
- Test: `tests/integration/test_ws.py` (TestClient websocket, `MockHarness` backend)

**Behavior:** client connects to `/ws/rooms/{room_id}`; sends `{type:"post", author_id, text, reply_mode, quoted_ids}` or `{type:"command", persona_id, command}`; server drives the orchestrator and streams `{persona_id, event}` frames (each `event` a serialized `StreamEvent`), plus a final `{type:"message", ...}` per persisted reply. Bind a fresh `run_id` per run (set `run_id_var`). Test: post over WS with mock backend → receive text frames then a done frame; assert reply persisted. **Commit** `git commit -am "feat(api): websocket streaming endpoint"`

### Task 5.4: Error-card contract

**Files:**
- Modify: `app/api/ws.py`
- Test: extend `test_ws.py`

On orchestrator-emitted `RunError`, send an `{type:"error_card", run_id, error_kind, message, command_redacted, log_url}` frame. Test that a failing mock backend produces exactly this frame and that `log_url` points at the run-log route. **Commit** `git commit -am "feat(api): structured error-card frames"`

---

## Phase 6 — Frontend (buildless vanilla JS)

> No bundler. `app/web/` served as static files by FastAPI. ES modules. Keep DOM logic small and split by concern. Each task: build it, then verify in the browser against the running app with `MockHarness` personas (add a quick manual-check note), plus a lightweight DOM test only where logic is non-trivial (e.g. mention parsing in the composer mirrors `routing.py` — unit-test it in JS via a tiny `node --test`, optional).

### Task 6.1: Static shell + serving
- Create: `app/web/index.html`, `app/web/css/app.css`, `app/web/js/main.js`; mount `StaticFiles` in app factory. Manual check: page loads at `/`. **Commit.**

### Task 6.2: WebSocket client + transcript rendering
- Create: `app/web/js/ws.js`, `js/transcript.js`. Render messages with author color/handle; live-append `text` deltas; render `thinking` in a collapsible block; render `tool_use`/`tool_result` as inline cards; show `usage`/context indicator per persona. **Commit.**

### Task 6.3: Composer
- Create: `app/web/js/composer.js`. Writer selector (Me/Boss/…), `@`-mention autocomplete from room personas, `@everyone`, quote chips (click a message → "Quote" adds a chip), reply-mode toggle (sequential/parallel). Emits the WS `post` frame. **Commit.**

### Task 6.4: Room list + persona config + session console
- Create: `app/web/js/rooms.js`, `js/personas.js`, `js/console.js`. Room switcher; persona editor form (all FR-P2 fields, templates); per-persona session console with `/compact`, `/clear`, raw command, and the context indicator. **Commit.**

### Task 6.5: Error cards + log links
- Create: `app/web/js/errors.js`. Render `error_card` frames as a distinct red card with the redacted command and a link to `log_url`. **Commit.**

---

## Phase 7 — Integration, run, polish

### Task 7.1: End-to-end integration test (MockHarness)
- Create: `tests/integration/test_e2e.py`. Boot the app (factory + temp db + mock backend), create a room + two personas + Boss author over REST, drive a full WS conversation: untagged context message → tagged sequential debate (assert cross-talk) → quote-reply → parallel fan-out → `/compact` → simulate restart (new app instance, same db) and assert a persona resumes with its stored `session_id`. This is the keystone test proving the success criteria. **Commit.**

### Task 7.2: Startup health banner wiring
- Frontend reads `/health` on load; shows a clear banner if `claude`/`codex` missing or unauthenticated (FR-E1). **Commit.**

### Task 7.3: README + run docs
- Create: `README.md` with setup, run (`python -m app.main`), how to add personas/authors, the error-finding playbook pointer (AGENTS.md §7), and the "no real CLI in tests" rule. **Commit.**

### Task 7.4: Opt-in real-CLI smoke test (marked, excluded from CI)
- Create: `tests/live/test_real_cli_smoke.py` marked `@pytest.mark.live` (deselected by default). Spawns a real `claude`/`codex` once to validate the parser against a true capture; refresh the fixtures from its output. Document running it manually. **Commit.**

---

## Definition of done (per AGENTS.md §9)

Every task: failing test first → passing → `ruff check . && ruff format --check . && mypy app` clean → no AGENTS.md §4 rule violated → docs updated if behavior changed → claims backed by observed command output. Final E2E (Task 7.1) green proves the seven success criteria in `goal.md`.

## Build order / dependencies

Phases are sequential, but within them: Phase 1 has no deps; Phase 2 needs Phase 1; Phase 3 needs Phase 1; Phase 4 needs 1–3; Phase 5 needs 4; Phase 6 needs 5; Phase 7 needs all. The `MockHarness` (3.2) unblocks all of Phase 4–7 testing without touching real CLIs — build it early and lean on it.
