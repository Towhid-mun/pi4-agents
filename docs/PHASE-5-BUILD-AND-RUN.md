# Phase 5 — Integration

> **Provenance.** Same convention as the earlier phase docs: filled in from
> real, live behavior, not written up front from what the code was expected
> to do. Anywhere this document fixes something `ARCHITECTURE.md` and
> `DEVELOPMENT-PLAN.md` leave open, that is this document's own decision,
> marked **(chosen here)**.

Phase 5 gate, verbatim from the working prompt:

1. `Cmd+Shift+B` in a LOCAL VS Code window builds on the Pi. A compile error
   appears in the Problems panel and clicking it opens the right local file
   at the right line.
2. An unattended agent session completes edit → build → read error → fix →
   build with ZERO permission prompts and no questions asked.
3. `perch init` in an empty directory produces a working project, ending in
   a `perch doctor` that passes.
4. The cold-start test: clone the repo into a fresh directory and follow the
   from-scratch setup docs exactly as written, as if new to this project.

## ⚠ Before anything else: never open this project over Remote-SSH to the target

This is the single most likely way a new user breaks Phase 5, so it goes
first, not buried in troubleshooting.

`.vscode/tasks.json`'s tasks assume they run on the **host** (your Mac). If
this project is opened in an editor window connected to the target over
Remote-SSH, the task's shell runs **on the target**, and `perch` - which
itself shells out to `ssh <alias>` - tries to SSH from the target to
itself. This does not fail cleanly. Provoked live, from an ssh session
into the Pi with a copy of this repo checked out there:

```sh
ssh pi
cd perch  # a checkout on the target itself, not the host
python3 -m perch doctor
```

Observed: `ssh: Could not resolve hostname pi: Name or service not known`,
then `perch: pi: unreachable - check the target is powered on and reachable
on the network`, exit **69** - because the Pi's own `~/.ssh/config` has no
`Host pi` entry (that alias only exists in the developer's `~/.ssh/config`
on the **host**). This is exactly the "confusing, working-looking failure"
this phase's own prompt warned about: the error text is a perfectly normal
*target unreachable* message, identical to what a genuinely powered-off Pi
would produce - nothing about it says "you're running this in the wrong
place." A user chasing it would check the Pi's power and network before
ever suspecting the editor window.

**Mitigation (chosen here):** `perch` now refuses immediately, before
touching config or the network, whenever `platform.system() != "Darwin"`
(`cli._refuse_if_running_on_the_target`, P5-1) - ARCHITECTURE.md §1 fixes
Host as macOS, so this is checking a real invariant, not a guess. Confirmed
live on the target itself:

```sh
ssh pi
cd perch && python3 -m perch doctor
```

Now prints, before any ssh attempt:

```
perch: this looks like Linux (aarch64), not macOS - perch must run on the
HOST, never the target. If this is a VS Code window connected to the Pi
over Remote-SSH, close it and reopen the project as a plain LOCAL window
(see docs/PHASE-5-BUILD-AND-RUN.md).
```

exit **64**, in well under a second, with the *actual* cause named instead
of a plausible-but-wrong one. This check applies to every verb, not just
`doctor` - the first command a confused Remote-SSH window runs is far more
likely to be `build` (via `Cmd+Shift+B`) than someone typing `doctor` by
hand, and the risk (a resulting ssh loop or a doubly-confusing error) is
identical either way. It is a friendly guard, not a security boundary -
trivially bypassed by anyone who wants to run perch on a non-macOS host on
purpose - and does not attempt to distinguish "on the target over
Remote-SSH" from "on some other Linux/Windows box"; ARCHITECTURE.md §1
does not distinguish those either.

**Practically:** before opening this project in an editor, confirm the
window is local - VS Code's Remote-SSH windows show `SSH: <host>` in the
bottom-left status bar; a plain local window shows nothing there (or your
Mac's own hostname). If you only need an interactive shell on the target,
`ssh pi` directly stays available and always will - ARCHITECTURE.md §11
is explicit that this tool is not a replacement for that.

## What Phase 5 adds

```
.vscode/tasks.json      C8 - build/test/run/doctor tasks, explicit problem matcher
CLAUDE.md                agent contract, points at ARCHITECTURE.md
.claude/settings.json    permission allowlist for perch's own commands
perch init [alias]       C8 - scaffolds a new project (P5-3)
```

`perch/cli.py` gains `_refuse_if_running_on_the_target()` (called at the
top of every verb's dispatch, P5-1) and the `init` verb (P5-3). Nothing
else in `perch/` changed this phase - Phase 5 is thin by design (C8:
"consumes C7's contract, adds no behaviour").

## Design decisions this document fixes

**The problem matcher is explicit, not `$gcc` (P5-1).** Read C5
(`perch/diagnostics.py`) before writing anything: P3-2 already settled that
a rewritten diagnostic's file field is always an **absolute** local path
(`local_root / workspace-relative-part`), never workspace-relative - so
`fileLocation` must be `"absolute"`, not `["relative", "${workspaceFolder}"]`.
Confirmed against real output rather than assumed - see "Verifying each
guarantee" below. The matcher pattern itself
(`^(.*?):(\d+):(\d+):\s+(error|warning):\s+(.*)$`) was checked against the
exact live gcc lines this project's own C5 produces, and separately
against Node's real regex engine (the same family VS Code's matcher uses),
not just Python's `re` - both agreed. It targets gcc/clang's
`file:line:col: severity: message` shape only, matching P3's own primary
target; a Python traceback's `File "path", line N` shares no line with its
own error text (the exception message is a separate line with no location
prefix at all), so a single-line VS Code pattern cannot also cover it -
out of scope for "a GCC problem matcher" (DEVELOPMENT-PLAN P5-1), not a
gap in this one.

**`perch` refuses to run on non-macOS (P5-1).** See the warning section
above for the full reasoning and the live provocation. Applies to every
verb, checked before config resolution or any network activity, using the
tool's existing `ConfigError`/exit 64 rather than a new exception class.

**CLAUDE.md points at ARCHITECTURE.md, never duplicates it (P5-2).** Five
rules, under a page: never build/run locally, everything goes through
`perch`, the host is the source of truth, `perch pull` retrieves target
output, read ARCHITECTURE.md before a structural change. Duplicated rules
drift; the copy an agent actually reads ends up being the stale one.

**The permission allowlist pre-approves perch's own verbs, never
`perch exec` (P5-2).** Verified against the current Claude Code permissions
docs (fetched live, not recalled): a rule like `Bash(perch build *)`
matches the bare command too (a trailing `" *"` with the space is part of
the match), and compound commands are split and matched subcommand by
subcommand before allow rules are checked, so this cannot be defeated by
chaining `&&`/`;`/`|` onto an approved call. `perch exec <cmd>` is
deliberately left off: it runs arbitrary text as a command on the target,
and the docs single out exactly this shape - an "environment runner" like
`devbox run`, which the docs use as their own worked example - as
something a Bash-rule pattern cannot safely narrow ("a rule like
`Bash(devbox run *)` matches whatever comes after `run`, including
`devbox run rm -rf .`"). There is no rule syntax that expresses "allow
`perch exec X` unless X contains `sudo`" - so "sudo through it deserves a
prompt" is only actually true if *every* `perch exec` invocation prompts,
which is what leaving it off the allowlist gives you, for free, with the
existing default permission mode. `Edit` and `Write` are allowed
unconditionally - editing the project's own checked-out files is the
ordinary, safe activity an agent working on this repo does continuously,
and gating it defeats the point of an unattended loop.

**`perch init` guesses `build` only, never `test`/`run` (P5-3).** A
`Makefile` or `pyproject.toml` names a real, conventional build command
(`make`, `python3 -m build`); there is no equally reliable signal for what
running tests or the program itself looks like, and a wrong guess baked
into a generated file is worse than an honest, commented placeholder the
user fills in themselves.

**The generated `run` example rebuilds in the same command (P5-3), found
while closing this gate.** The first draft's example was `run = "./main"`
- wrong, discovered live: `run`'s own sync step is exactly as unconditional
as every other verb's, and a binary built by an earlier, separate
`perch build` exists only on the target, so that sync deletes it (I2)
*before* `run` ever gets to use it. Confirmed by watching it happen -
`perch build` (succeeds) followed by a separate `perch run` invocation
printed `*deleting main` from the sync, then `sh: 1: ./main: not found`,
exit 127. The generated example is now `run = "make && ./main"`, which
rebuilds fresh inside the one command that also runs it - confirmed live
to work. Anyone hand-writing a `run` command for a target-built artifact
needs to know this; it is not specific to `perch init`'s own example.

## Verifying each guarantee

### 1. `fileLocation` and the problem matcher pattern (gate item 1, offline half)

```sh
mkdir -p /tmp/perch-gate1 && cd /tmp/perch-gate1
cat > .perch.toml <<'EOF'
host = "pi"
remote_root = "perch-gate1-test"

[commands]
build = "gcc -Wall -o main main.c"
EOF
printf '#include <stdio.h>\nint main() {\n    int x = \n    return 0;\n}\n' > main.c
perch build
```

Live output included:

```
/private/tmp/perch-gate1/main.c:4:5: error: expected expression before ‘return’
/private/tmp/perch-gate1/main.c:3:9: warning: unused variable ‘x’ [-Wunused-variable]
```

`ls -la /private/tmp/perch-gate1/main.c` succeeds - an absolute, `ls`-able
host path, as P3-2 requires. Both lines matched the tasks.json pattern
under Node's real regex engine, capturing file/line/col/severity/message
exactly.

**The interactive half of gate item 1 (`Cmd+Shift+B` in an actual VS Code
window, confirming the Problems panel entry is clickable) needs a human at
a GUI** - there is no VS Code or browser-automation surface available to
drive this from an agent session. Confirmed instead: the exact regex
against the exact live output, and the JSON shape of `.vscode/tasks.json`
(`python3 -c "import json; json.loads(...)"` after stripping `//`
comments). If you run the manual half and it does not work as described
here, that is new information this document does not yet have - the
matcher pattern is the most likely thing to have drifted from a compiler
version change.

### 2. On-target refusal (the warning section above)

Provoked for real over ssh into the target itself - see the warning
section for the exact commands and output, both before and after the fix.

### 3. The permission allowlist (gate item 2)

Confirmed live with a genuinely separate, unattended process - not this
session, not a mocked permission layer:

```sh
mkdir -p /tmp/perch-gate2 && cd /tmp/perch-gate2
mkdir .claude
cp <this-repo>/.claude/settings.json .claude/settings.json  # cwd-scoped, not inherited from a parent - see below
cat > .perch.toml <<'EOF'
host = "pi"
remote_root = "perch-gate2-test"
[commands]
build = "gcc -Wall -o main main.c"
EOF
printf '#include <stdio.h>\nint main(void) {\n    int x = 5\n    printf("x = %%d\\n", x);\n    return 0;\n}\n' > main.c
claude -p "Run 'perch build'. It will fail. Read the error, fix main.c, run 'perch build' again to confirm success. Never invoke gcc directly." \
  --permission-mode manual --output-format stream-json --verbose
```

`--permission-mode manual` (Claude Code's normal prompting mode - `manual`
is accepted as an alias for `default`) with **no** human able to answer:
in this mode, anything not covered by an allow rule is denied rather than
hung on. Observed transcript: `perch build` (fails, real gcc diagnostic,
exit 1) → `Read main.c` → `Edit` (adds the missing semicolon) →
`perch build` (succeeds, exit 0). Zero denials for anything in the
allowlist's scope - the run completed the whole edit-build-fix-build cycle
unattended. Total cost for that run: $0.057.

**Scoping gotcha, found while setting this up:** `.claude/settings.json` is
resolved from the actual working directory, not inherited from a parent
directory's `.claude/`. Confirmed by testing first from a subdirectory of
this repo with no settings file of its own (`perch build` was denied
outright) and then again after copying the settings file directly into
that subdirectory (it worked). Not a problem for this repository itself -
an agent session here runs with the repo root as its working directory,
where `.claude/settings.json` already lives - but relevant if you ever
`cd` somewhere else first.

### 4. `perch init` (gate item 3)

```sh
mkdir -p /tmp/perch-gate3 && cd /tmp/perch-gate3
cat > main.c <<'EOF'
#include <stdio.h>
int main(void) { printf("hello from perch init test\n"); return 0; }
EOF
cat > Makefile <<'EOF'
main: main.c
	gcc -O0 -o main main.c
EOF
perch init pi
```

Observed: `wrote .perch.toml` / `wrote .vscode/tasks.json` / `wrote
CLAUDE.md`, then a full `perch doctor` report ending in the target's real
identity (Debian 13/trixie, aarch64, gcc 14.2.0, ...), exit **0** - the
"it works" outcome, one command, no editing needed for this project shape.
The generated `.perch.toml` had `build = "make"` (detected from the
`Makefile`) filled in automatically. `perch build` immediately afterward,
using nothing but what `init` wrote, compiled for real
(`gcc -O0 -o main main.c`, exit 0).

### 5. The cold-start test (gate item 4)

**A note on the instruction itself first:** the working prompt for this
gate said to follow `docs/PHASE-3-BUILD-AND-RUN.md` "from §2" - that
document has no `§2` (no phase doc in this repository numbers its
sections; `§N` only ever appears in prose citing `ARCHITECTURE.md`'s own
numbered sections). Flagging this rather than silently guessing which
section was meant. What was actually run instead, against a genuinely
fresh `git clone` of this repository into `/tmp` (not this working copy):
`docs/PHASE-0-BUILD-AND-RUN.md`'s Install and "Run it"/"break it on
purpose" sections (the actual from-scratch install steps - `PHASE-3`'s own
document assumes `perch` is already installed), followed by every
numbered step in `PHASE-3-BUILD-AND-RUN.md`'s "Verifying each guarantee"
- the part of that document that is genuinely "the from-scratch setup
guide... you will re-run it," as this phase's own working prompt
describes it.

Every command ran exactly as documented and produced the documented
result, **with one exception, fixed rather than explained around**:
`docs/PHASE-0-BUILD-AND-RUN.md`'s "break it on purpose" transcript showed
`src/main.c:4:40: error: ...` (a workspace-relative path) as the current
expected output. Re-running that exact sequence today produced
`/Users/towhid/perch-tryit/src/main.c:4:40: error: ...` instead - an
absolute path. Not a transcription mistake: that document was written
before C5 (Phase 3's diagnostic mapper) existed, and its transcript was
never revisited when C5 started rewriting every diagnostic's path, in
plain-text mode too, not just `--json`. Fixed in place in
`docs/PHASE-0-BUILD-AND-RUN.md`, with both the original and current
transcripts shown and dated, rather than silently replacing the historical
record.

Everything else re-verified clean in the fresh clone: `pip install -e .`
+ `perch --version`, the full `perch sync` / `exec` / `build` / `run`
sequence against the real Pi with byte-for-byte matching output, the
break-it-on-purpose loop (once corrected as above), all five
`PHASE-3-BUILD-AND-RUN.md` gate items (workspace-relative-turned-absolute
paths and `ls`-ability, the noise fixture's byte-identical round trip,
`--json`'s event schema, `make -C subdir`'s path resolution, and the
offline suite with `ssh`/`rsync` removed from `PATH`), and the full
offline test suite (352 tests, 0 failures) both from the fresh clone's own
`.venv` and with the network tools absent.

## `perch exec` and the permission allowlist - the boundary drawn (P5-2)

Restated plainly, since this was flagged as a decision worth a second look
rather than a silent default:

- **Allowed, unattended:** `perch build`, `perch test`, `perch run`,
  `perch sync`, `perch doctor`, `perch pull` - every verb whose surface is
  fixed by this tool's own CLI, none of which can be redirected into an
  arbitrary command.
- **Always prompts:** `perch exec <cmd>`, sudo or not. Claude Code's own
  permission-rule syntax cannot express "approve the runner, deny only a
  dangerous inner command" for a tool that executes its trailing text
  verbatim (their own documented example, `devbox run`, is the same
  shape). The alternative - a rule that only *looks* like it blocks
  `sudo` (e.g. `Bash(perch exec sudo *)`) - would not actually hold as a
  boundary (`perch exec bash -c "sudo reboot"` does not contain that
  literal text) and is worse than no rule at all, since it invites false
  confidence. If a specific, frequently-used exec command turns out to be
  safe and repetitive, the documented fix is a narrow rule naming both the
  runner and that exact inner command (`Bash(perch exec pgrep *)`), never
  a blanket one.

## What still doesn't work

| Gap | Phase that fixes it |
|---|---|
| `ARCHITECTURE.md` §5/C7 lists `perch logs [unit]` and `perch shell`, and a `--no-sync`/`-n` flag - none of these were ever turned into a ticket in `DEVELOPMENT-PLAN.md`, and none exist in `perch/cli.py` | not phase-gated; a pre-existing gap between the architecture doc's aspirational contract and what was actually built, noticed while re-reading C7 for this phase, not introduced by it |
| The interactive half of gate item 1 (`Cmd+Shift+B` in a real VS Code window) needs a human at a GUI to confirm | n/a - no VS Code or browser-automation surface reachable from an agent session; see "Verifying each guarantee" §1 |
| A Python traceback's exception message (no file:line prefix of its own) is not clickable via the tasks.json matcher, only the `File "path", line N` line above it | not planned - a single-line VS Code pattern cannot join two unrelated lines; out of scope for "a GCC problem matcher" |
| `docs/FUTURE-WORK.md`, named in this phase's own working prompt as required reading, does not exist in this repository | not created by this phase - `ARCHITECTURE.md` §11 ("Out of scope") is the closest existing equivalent and was used instead |

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| `perch` refuses immediately with "this looks like Linux/Windows, not macOS" | Running on the target itself (most likely a VS Code Remote-SSH window) or deliberately on a non-macOS host | See the warning section at the top of this document |
| A VS Code task's Problems panel entry does nothing when clicked | `fileLocation` doesn't match what C5 actually emits, or the compiler/C5 output shape changed | Re-run "Verifying each guarantee" §1 above and compare the live line against `.vscode/tasks.json`'s `pattern.regexp` |
| An agent session still gets a permission prompt for `perch build`/`test`/`run`/`sync`/`doctor`/`pull` | `.claude/settings.json` isn't in the directory the session's working directory actually resolves to (it is NOT inherited from a parent's `.claude/`) | Confirm the file exists at `<cwd>/.claude/settings.json`, not just somewhere above it |
| `perch exec ...` always prompts, even for something harmless and repeated often | Working as designed - see "`perch exec` and the permission allowlist" above | Add a narrow rule naming the exact inner command, e.g. `Bash(perch exec pgrep *)`, rather than asking for `perch exec` itself to be allowlisted |
| `perch run`'s command can't find a binary a previous `perch build` just produced | `run`'s own sync deletes a target-only artifact before the command runs (I2) - this is not a bug | Rebuild inside the `run` command itself, e.g. `run = "make && ./main"`, never assume a separate build's output survives |
| `perch init` didn't guess a `build` command | Neither a `Makefile`/`makefile` nor a `pyproject.toml` was found in the current directory | Fill in the commented `# build = "make"` line by hand - `perch init` deliberately does not guess beyond those two shapes |
