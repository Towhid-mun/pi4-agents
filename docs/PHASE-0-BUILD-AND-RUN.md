# Phase 0 — Build and Run

Record of what the Phase 0 walking skeleton actually built, and how to run it.
Written after the fact, from the tickets that shipped (P0-1 through P0-5) and
the gate transcript that closed the phase. If you're looking for the spec that
came *before* the code, that's `DEVELOPMENT-PLAN.md`; this is the record of
what landed.

## What Phase 0 is

Five verbs, wired straight through, no fidelity guarantees:

```
perch sync            mirror the workspace to the target, nothing else
perch exec <cmd...>   sync, then run an arbitrary command in the remote root
perch build           sync, then run commands.build from .perch.toml
perch test            sync, then run commands.test from .perch.toml
perch run             sync, then run commands.run   from .perch.toml
```

That's it. No `doctor`, `pull`, `logs`, `shell`. No `--tty`, `--json`,
`--replace`, `--no-sync`.

## What it deliberately does not do yet

- **Output is buffered**, not streamed (I5 is not implemented). `executor.py`
  is marked `# REPLACED IN P2`.
- **No signal handling.** Ctrl-C does not forward to the target; it returns
  exit 74 (indeterminate), not 130, because 130 would falsely claim the remote
  process group was confirmed dead (I7). P2-3 makes 130 truthful.
- **No run lock.** Two concurrent invocations will collide uncontrolled.
- **No SSH multiplexing.** Every invocation pays a full SSH handshake — there
  is no `ControlMaster`/`ControlPersist` yet. That's P1-1.
- **No artifact retrieval (C6).** The mirror deletes on every sync, so a
  binary built on the target and not covered by the command you're about to
  run next is gone before you can retrieve it. Chain `build` and `run` in one
  configured command (see below) until P4-2 lands.
- **Config errors, sync failures, and remote exit codes** are the only things
  that map to specific exit codes right now (64, 73, and passthrough
  respectively). See `ARCHITECTURE.md` §7 for the full contract.

## Prerequisites

- macOS with Python 3.11+.
- An ssh alias in `~/.ssh/config` that connects with no password prompt:

  ```sh
  ssh pi true   # should return silently and immediately
  ```

- `gcc` and `make` on the target (default on Raspberry Pi OS).

## Install

From the repo root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/perch --version
```

Put `.venv/bin` on `PATH` for the rest of this, or prefix every command with
`.venv/bin/`:

```sh
export PATH="$PWD/.venv/bin:$PATH"
```

## Make a scratch project

`perch` finds `.perch.toml` by walking up from the current directory, so this
can live anywhere outside this repo:

```sh
mkdir -p ~/perch-tryit/src && cd ~/perch-tryit

cat > .perch.toml <<'TOML'
host = "pi"
remote_root = "perch-tryit"

[commands]
build = "make"
run   = "make -s && ./hello"
TOML

cat > Makefile <<'MAKE'
hello: src/main.c
	gcc -Wall -o hello src/main.c
MAKE

cat > src/main.c <<'C'
#include <stdio.h>

int main(void) {
    printf("hello from the pi\n");
    return 0;
}
C
```

`host` must be a bare ssh alias — never a `user@host` string; connection
detail stays in `~/.ssh/config` (I10). `remote_root` is a path under `$HOME`
on the target; `perch` creates it on first use.

## Run it

```sh
perch sync          # mirror the workspace to the target, nothing else
perch exec uname -m # sync, then run an arbitrary command remotely -> aarch64
perch build         # sync, then run commands.build (gcc runs ON THE PI)
perch run           # sync, then run commands.run
```

Verified output, this exact sequence against the real Pi:

```
$ perch sync
.d..tp... ./
<f+++++++ Makefile
cd+++++++ src/
<f+++++++ src/main.c
exit: 0

$ perch exec uname -m
aarch64
exit: 0

$ perch build
gcc -Wall -o hello src/main.c
exit: 0

$ perch run
*deleting hello

.d..t.... ./
hello from the pi
exit: 0
```

Note the `*deleting hello` line on `run` — that's the mirror deleting the
binary `build` just produced, before `run`'s own `make -s && ./hello`
rebuilds it. This is the C6 gap described above, not a bug.

Each of `sync`/`exec`/`build`/`test`/`run` syncs first. The mirror deletes, so
anything built on the target and not covered by the command you're running
next is gone on the next invocation.

## See the point of the whole thing: break it on purpose

```sh
sed -i '' 's/;$//' src/main.c   # remove the trailing semicolon
perch build; echo "exit: $?"
```

Verified output:

```
$ cat -n src/main.c
     1	#include <stdio.h>
     2	
     3	int main(void) {
     4	    printf("hello from %s\n", "the pi")
     5	    return 0;
     6	}

$ perch build
gcc -Wall -o hello src/main.c
src/main.c: In function ‘main’:
src/main.c:4:40: error: expected ‘;’ before ‘return’
    4 |     printf("hello from %s\n", "the pi")
      |                                        ^
      |                                        ;
    5 |     return 0;
      |     ~~~~~~
make: *** [Makefile:2: hello] Error 1
### exit status: 2
```

The diagnostic is the Pi's own `gcc` (Debian 14.2.0), not the host's Clang —
confirmed by contrast:

```
$ perch exec gcc --version
gcc (Debian 14.2.0-19) 14.2.0

$ cc --version   # the host compiler, for contrast
Apple clang version 17.0.0 (clang-1700.6.3.2)
```

Fix the semicolon and `perch run` again to close the loop:

```
$ perch run
hello from the pi
exit: 0
```

This is the Phase 0 gate: a local syntax error makes `perch build` exit
non-zero, with the Pi's own compiler diagnostic printed on the host terminal.

## Clean up

```sh
ssh pi 'rm -rf ~/perch-tryit'
rm -rf ~/perch-tryit
```

## Offline test suite

No target needed — this is the fast feedback loop and stays that way on
purpose:

```sh
cd /path/to/this/repo
.venv/bin/python -m unittest discover -s tests -t .
```

83 tests as of the end of Phase 0, all passing with `ssh` and `rsync` removed
from `PATH` (proving nothing in the suite reaches the network).

## Exit codes you'll actually see in Phase 0

| Code | When |
|---|---|
| `0` | Remote command succeeded |
| `1–63` | Remote command's own exit status, passed through unchanged (I6) |
| `64` | Config error — missing/malformed `.perch.toml`, unknown key, bad verb |
| `73` | Sync failed — nothing was executed on the target (I4) |
| `74` | Ctrl-C during a run — indeterminate, *not* confirmed-dead (honest, not yet correct; P2-3 fixes this) |

`69` (target unreachable), `70` (internal error), `75` (run lock held) and
`130` (interrupted-and-confirmed-dead) are all defined in `errors.py` but
nothing in Phase 0 raises them yet in a meaningful way — there's no connect
timeout, no run lock, and no confirmed signal delivery. That's Phase 1 and
Phase 2.
