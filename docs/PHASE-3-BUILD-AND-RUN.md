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
- **ANSI escape sequences (decided, P3-1)**: stripped, both for matching AND
  for what the user sees - never kept. `-fdiagnostics-color=always` produces
  real escapes sitting directly before the filename (confirmed by raw byte
  capture, `single_error_color.stderr.txt`), so stripping before matching is
  mandatory regardless. Stripping the *displayed* text too, rather than only
  the copy used for matching, was chosen because: our own pipe-mode wrapper
  never allocates a remote tty, so gcc only emits colour at all when
  explicitly forced - there is no legitimate "the user's real terminal wants
  this colour" case to preserve; a JSON event's `message` field (P3-3)
  carrying raw control bytes would be actively harmful to any consumer; and
  keeping colour would mean reconstructing a rewritten diagnostic line (P3-2)
  around embedded escape codes without corrupting them, for no real benefit.
  Verified live through the full pipeline, not just offline: `perch exec gcc
  -fdiagnostics-color=always ...` produces plain text (`cat -v` shows no
  `^[` sequences).
- **`make -C subdir` cwd tracking (confirmed empirically, P3-4)**: GNU Make
  4.4.1 on this target DOES print `make: Entering directory '/abs/path'` /
  `Leaving directory` on **stdout** by default under `-C`, no flag needed -
  see `tests/fixtures/make_subdir.stdout.txt`. P3-2's cwd tracking watches
  for exactly this line shape.

## Offline test suite

Unchanged expectation from Phases 0-2 - runs with no target reachable:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```
