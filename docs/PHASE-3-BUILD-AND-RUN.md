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
> depend on that shape were filled in *as* each ticket landed, not guessed up
> front, and are now the settled record of what was actually decided and why.
> Anywhere this document fixes something `ARCHITECTURE.md` and
> `DEVELOPMENT-PLAN.md` leave open, that is this document's own decision,
> marked **(chosen here)**.

Phase 3 gate, verbatim from the working prompt, all five run for real against
the target while finalizing this document:

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

`executor.py`'s per-line `emit()` gains a C5 pass before a line reaches
anywhere: strip ANSI, track the compiler's real remote working directory,
recognize gcc/clang/linker/Python diagnostic shapes, rewrite a recognized
file to a local path. In plain-text mode that (possibly rewritten) line is
what gets printed; in `--json` mode it becomes a `stdout`/`stderr` event, and
a primary error or warning also gets its own `diag` event. `mirror.py`'s
rsync invocation gained a `quiet` flag so `--json` mode's stdout carries
nothing but JSON.

## The event schema (ARCHITECTURE.md §5/C5, plus one addition)

```json
{"t": "stdout", "line": "..."}
{"t": "stderr", "line": "..."}
{"t": "diag", "file": "src/main.c", "line": 12, "col": 5, "severity": "error", "message": "..."}
{"t": "exit", "code": 1, "interrupted": false, "indeterminate": false}
```

The first three lines are quoted verbatim from `ARCHITECTURE.md` - not this
document's decision. The `exit` event's `indeterminate` field **(chosen
here)** is an addition: that schema predates P2-6, which is where
"indeterminate" as a distinct outcome (not success, not failure, not merely
"interrupted") was designed. Without it a `--json` consumer would have to
know that exit code 74 specifically means indeterminate - exactly the kind
of implicit knowledge structured events exist to avoid. `code` is the raw
remote/local exit value (matching `RunResult.exit_code`), not the tool's own
mapped process exit status (`errors.exit_code_for_run`'s job, a separate
concern).

One object per line, on stdout, in `--json` mode only. In the default
(non-JSON) mode, C5 still runs - path rewriting applies to the plain text a
human sees too, per gate item 1, which does not mention `--json`.

## Design decisions this document fixes

- **Rewritten path form (P3-2)**: always an absolute local path
  (`local_root / workspace-relative-part`), never relative to the caller's
  own cwd. Unambiguous regardless of where `perch build` was actually run
  from, needs no extra context beyond what C1 already provides, and makes
  gate item 1's `ls` check trivially true by construction.
- **Resolution mechanism (P3-2)**: a diagnostic's raw file text is spliced
  out of the line at its exact regex match span and replaced - never a
  reconstruct-the-whole-line-from-parsed-parts, which risks silently getting
  some OTHER piece (spacing, punctuation) wrong even when file/line/col
  themselves are correct. A file that cannot be placed inside the workspace
  (a system header, a `/tmp/ccXXXXXX.o` linker scratch file) is left
  completely untouched, never given a fabricated local path.
- **ANSI escape sequences (P3-1)**: stripped, both for matching AND for what
  the user sees - never kept. `-fdiagnostics-color=always` produces real
  escapes sitting directly before the filename (confirmed by raw byte
  capture, `single_error_color.stderr.txt`), so stripping before matching is
  mandatory regardless. Stripping the *displayed* text too was chosen
  because: our own pipe-mode wrapper never allocates a remote tty, so gcc
  only emits colour at all when explicitly forced - there is no legitimate
  "the user's real terminal wants this colour" case to preserve; a JSON
  event's `message` field carrying raw control bytes would be actively
  harmful to any consumer; and keeping colour would mean reconstructing a
  rewritten diagnostic line around embedded escape codes without corrupting
  them, for no real benefit.
- **`make -C subdir` cwd tracking (P3-4/P3-2)**: GNU Make 4.4.1 on this
  target DOES print `make: Entering directory '/abs/path'` / `Leaving
  directory` on **stdout** by default under `-C`, no flag needed - confirmed
  empirically, not assumed (`tests/fixtures/make_subdir.stdout.txt`).
  `PathResolver` tracks this as a LIFO stack of directories relative to
  `remote_root`, not a single value, because nested `-C` must pop back to
  the intermediate level, not straight to `remote_root` - untested for real
  on this target (only one level was ever observed) but cheap and safe to
  implement correctly regardless, since it only affects the nested case.
- **`--json` and the mirror (P3-3)**: rsync's own itemized change list is
  tool progress, not part of the remote command's diagnostic stream - in
  `--json` mode it is dropped (`mirror.push(..., quiet=True)`) rather than
  leaking raw text onto a stdout that's supposed to carry only JSON.
- **Deliberately NOT recognized, to protect pass-through fidelity**: gcc's
  `file: In function 'X':` context line (same "file: text" shape as real
  noise); `/usr/bin/ld: ...` and `collect2: error: ...` (neither names a
  workspace file - a system binary path and a program name); GNU Make's
  `make: *** [Makefile:2: target] Error 1` (a real file:line pair, but
  embedded inside a larger sentence, and redundant with the compiler's own
  more precise error right above it). Each is checked against a specific
  fixture line in `tests/test_diagnostics.py`, not just described here.

## Verifying each guarantee

### 1. Workspace-relative paths, `ls`-able (gate item 1)

```sh
cd integration/fixtures/diag-sources
perch exec gcc -Wall -o /tmp/single_error single_error.c 2>&1 | tee /tmp/out.txt
DIAG_LINE=$(grep ": error:" /tmp/out.txt)
PRINTED_PATH=$(echo "$DIAG_LINE" | sed -E 's/^([^:]+):[0-9]+:[0-9]+:.*/\1/')
ls -la "$PRINTED_PATH"
```

Expect the printed path to be this machine's own absolute path to
`single_error.c`, and `ls` to succeed.

### 2. Noise round-trips byte-identical (gate item 2)

```sh
perch exec ./noise.sh > /tmp/live_noise_out.txt
diff /tmp/live_noise_out.txt ../../../tests/fixtures/noise.stdout.txt && echo IDENTICAL
```

Expect `IDENTICAL` - no diff output, including the fixture's deliberately
missing trailing newline on its last line.

### 3. `--json` (gate item 3)

```sh
cd /tmp && mkdir -p gate3 && cd gate3
cp /path/to/repo/integration/fixtures/diag-sources/multi_error.c .
cat > .perch.toml <<'EOF'
host = "pi"
remote_root = "perch-gate3"

[commands]
build = "gcc -Wall -o multi_error multi_error.c"
EOF
perch build --json > out.jsonl; echo "exit: $?"
python3 -c "
import json
events = [json.loads(l) for l in open('out.jsonl')]
print(len(events), 'events, all valid JSON')
print(sum(1 for e in events if e['t']=='exit'), 'exit event(s)')
print([e for e in events if e['t']=='diag'])
"
```

Expect every line to parse, exactly one `exit` event, and `diag` events
carrying this machine's own absolute paths.

### 4. `make -C subdir` (gate item 4)

```sh
cd integration/fixtures/diag-sources
perch exec make -C subdir 2>&1 | grep ": error:"
```

Expect the printed path to end in `subdir/sub_error.c`, not
`sub_error.c` at the top level.

### 5. Offline suite, target unreachable (gate item 5)

```sh
env PATH=/usr/bin:/bin .venv/bin/python -c "
import os, unittest, sys
os.environ['PATH'] = '/nonexistent'
r = unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests', top_level_dir='.'))
sys.exit(0 if r.wasSuccessful() else 1)
"
```

Expect every test to pass with `ssh` and `rsync` both entirely absent from
`PATH`.

## What still doesn't work

| Gap | Phase that fixes it |
|---|---|
| The run lock - two concurrent `perch run` invocations for the same project will still collide | Phase 4 (P4-1) |
| Artifact retrieval | Phase 4 (P4-2) |
| The sync fast path (every `perch build` re-syncs in full even with nothing changed) | Phase 4 (P4-4) |
| `.vscode/tasks.json`, `CLAUDE.md` | Phase 5 |
| Multi-level `make -C` directory nesting is implemented (LIFO stack) but never exercised against a real nested build on this target - only single-level `-C` was ever captured | not phase-gated; would need a fixture with genuine recursive `make` to confirm |

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| A diagnostic's path did not get rewritten | The line did not match any recognized shape - by design, an unrecognized line passes through completely untouched (I8) rather than risk a wrong rewrite | Compare against `tests/fixtures/CAPTURE.md`'s table; if it's a genuinely new gcc/clang shape, it needs a new pattern, not a change to an existing one |
| A `make -C subdir` error resolved to the wrong directory | `PathResolver`'s cwd stack depends on seeing the `Entering directory` line BEFORE the error that follows it - if some intermediate line got dropped or reordered, the stack is wrong | Check that `make` is not being run with `--no-print-directory` or `-s` (silent), either of which suppresses the very announcements P3-2 depends on |
| `--json` output has a non-JSON line mixed in | A `print()` somewhere in `perch/` wrote directly to stdout instead of stderr, or `mirror.push` was called without `quiet=True` | Every `print()` in `perch/` outside `doctor`'s own report (`_run_doctor` - a separate, `--json`-agnostic verb) must carry `file=sys.stderr`, checked across the whole call including continuation lines, not just the opening `print(`; confirm `cli.py`'s `_dispatch` passes `quiet=json_sink` into `mirror.push` |
| A `diag` event's `file` field is still a remote-looking path | `PathResolver` could not place the file inside the workspace (correct behavior for a system header or a linker scratch file) - falls back to the diagnostic's raw, unrewritten file text rather than fabricating a local path | Confirm the file is genuinely outside `local_root`/`remote_root`; if it should have resolved, check `remote_root` in `.perch.toml` matches what the compiler actually saw |

## Offline test suite

Unchanged expectation from Phases 0-2 - runs with no target reachable, and
with `ssh`/`rsync` absent from `PATH`:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```
