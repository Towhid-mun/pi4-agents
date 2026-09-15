# Phase 3 — Build and Run

> **Provenance.** This document did not exist before the code, unlike the
> convention `docs/PHASE-0/1/2-BUILD-AND-RUN.md` set and unlike what the
> working prompt for this phase assumed. No separate author supplied it, so
> it is agent-authored from `ARCHITECTURE.md` (§5/C5, §7) and
> `DEVELOPMENT-PLAN.md` (P3-1..P3-4), drafted before any Phase 3 code so it
> could still serve as a spec-first target - the same convention established
> in `docs/PHASE-1-BUILD-AND-RUN.md`. This phase's own instruction is to build
> the fixture corpus (P3-4) *before* the parser (P3-1), specifically so the
> parser is written against real recorded gcc/clang/Python output rather than
> a remembered guess at their shape - so the parts of this document that
> depend on that shape (the exact regex patterns, the ANSI-handling decision,
> which lines get rewritten) are filled in *as* each ticket lands, not
> guessed up front. Anywhere this document fixes something ARCHITECTURE.md
> and DEVELOPMENT-PLAN.md leave open, that is this document's own decision,
> marked **(chosen here)**.

Phase 3 gate, verbatim from the working prompt:

1. A failing build produces diagnostics naming workspace-relative paths, and
   `ls` on the printed path succeeds.
2. The noise fixture round-trips byte-identical through the full pipeline.
3. `perch build --json` on a failing build: every line parses as JSON, `diag`
   events carry local paths, and exactly one `exit` event terminates it.
4. A `make -C subdir` build produces correct paths.
5. The offline suite passes with the Pi unreachable.

Plus the real gate: an unattended agent session, given only "run `perch
build`, fix what it reports, repeat until clean," closes the loop with no
questions asked and no compiler invoked on the Mac.

## What Phase 3 adds

```
perch/diagnostics.py   C5 - diagnostic parsing, path rewriting, JSON events
tests/fixtures/         real recorded gcc/clang/Python output (P3-4)
--json                  now does something (Phase 2 only recognized the flag)
```

`executor.py`'s per-line `emit()` gains a C5 pass before a line reaches the
user: parse, rewrite, decide what (if anything) becomes a `diag` event, then
either print the (possibly path-rewritten) line normally or, in `--json`
mode, emit only structured events on stdout.

## The event schema (ARCHITECTURE.md §5/C5 - not decided here, quoted)

```json
{"t": "stdout", "line": "..."}
{"t": "stderr", "line": "..."}
{"t": "diag", "file": "src/main.c", "line": 12, "col": 5, "severity": "error", "message": "..."}
{"t": "exit", "code": 1, "interrupted": false}
```

One object per line, on stdout, in `--json` mode only. In the default
(non-JSON) mode, C5 still runs - path rewriting applies to the plain text a
human sees too, per gate item 1, which does not mention `--json`.

## Design decisions this document fixes

**(chosen here, pending P3-1's real fixtures)** — placeholders below are
filled in as each ticket lands; this section is the running record.

- **Rewritten path form**: TBD at P3-2 - candidates are an absolute local
  path (unambiguous regardless of the caller's cwd, guarantees gate item 1's
  `ls` check trivially) vs. a path relative to the caller's own cwd (matches
  what a local compiler invocation would print, but needs an extra piece of
  context C1 does not currently provide). Decided in the P3-2 commit.
- **ANSI escape sequences**: TBD at P3-1 - strip-before-matching is
  required regardless (colour codes inside a path defeat the regex); whether
  the *emitted* text keeps or drops them is decided once real coloured
  output (if any) is seen from this target's gcc.
- **`make -C subdir` cwd tracking**: TBD at P3-2 - depends on whether GNU
  Make on this target actually prints `Entering directory`/`Leaving
  directory` announcements by default, checked empirically in the P3-4
  fixture capture rather than assumed from memory.

## Offline test suite

Unchanged expectation from Phases 0-2 - runs with no target reachable:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```
