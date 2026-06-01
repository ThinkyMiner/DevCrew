# Team — Goal

## One sentence

A locally-run group chat where one human (you) collaborates with a configurable
set of AI personas — each powered by the Claude Code or Codex CLI harness — to
validate ideas and get architectural and research support.

## The problem

When thinking through a hard decision you want more than one perspective: an
architect to pressure-test the design, a researcher to find prior art, a
reviewer to poke holes, a sounding board for what leadership actually wants.
Today that means juggling several separate CLI/chat sessions, copy-pasting
context between them by hand, and losing all of it when a session ends.

## What we are building

A single chat application that feels like a group chat (Slack-style rooms), where
the other "people" are AI personas you define. You drive the conversation: you
post messages, you tag the personas who should respond, and you can speak as
different human authors (yourself, or your Boss) to weight input correctly. The
personas can debate each other, inspect code, and do research, while the app
keeps every session durable and restartable.

## Why it can work well

Each persona is backed by a real CLI harness (Claude Code or Codex) running as a
managed subprocess. The harness — not us — owns that persona's context window,
thinking, tool use, and MCP servers, and it already persists a resumable session.
We orchestrate these sessions into one shared conversation and make everything
visible and durable. We reuse the harnesses' strengths instead of reimplementing
an agent loop.

## Success criteria

The project succeeds when:

1. **Multi-persona group chat works.** You can create rooms, define personas with
   distinct models/providers/personalities, tag them, and get coherent replies in
   a shared transcript.
2. **Personas can genuinely collaborate.** Sequential replies let a later persona
   respond to an earlier one in the same turn (real debate), and quote-replies let
   you inject a specific earlier message into a persona's context on demand.
3. **Authorship carries weight.** You can switch the message author between Me and
   Boss (and other authors you define), and Boss input is weighted higher in the
   personas' context.
4. **Sessions are durable and restartable.** Closing and reopening the app loses
   nothing: rooms, messages, persona sessions, and harness session IDs all reload,
   and any persona can resume exactly where it left off.
5. **Context is manageable.** You can see when a persona's context is getting heavy
   and run control commands (e.g. compact, clear) against that persona's session.
6. **Everything is transparent.** Live token streaming, collapsible thinking
   blocks, and inline tool/MCP calls are visible per persona, and every harness run
   is logged to disk.
7. **Errors are trivial to locate.** A failed run never fails silently: it surfaces
   a clear error in the chat with the redacted command and a one-click link to that
   run's full log.

## Explicit non-goals (for now)

- **No multi-human accounts / login.** One human operator. "Authors" (Me, Boss) are
  attribution identities you type as, not separate logged-in users.
- **No API-key / per-token mode.** Auth is your existing CLI subscriptions only.
- **No cloud hosting.** Runs locally, for you.
- **No mobile app.** Browser on your machine.
- **Personas do not act autonomously.** Nothing happens unless you send a message;
  there are no background agents or schedules.

## Guiding principles

- **Reuse the harness, don't reinvent it.** Context, resume, thinking, and MCP are
  the harness's job.
- **Make the invisible visible.** If the app does something, you can see it and find
  the log for it.
- **Fail loud, fail located.** Every error points to its cause.
- **Quality over surface area.** A small, correct, well-structured app beats a broad
  flaky one.
