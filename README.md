# perch

A remote build-and-run bridge: author source on your Mac, and `perch
build`/`test`/`run` compiles, runs and fails it on a Raspberry Pi over
SSH, streaming output and exit codes back as if the build had been local.

It exists because the two machines can't trade places: the Pi is too
small (2 GB RAM, SD card) to host an editor and a coding agent, and the
Mac has no ARM toolchain, I2C bus or GPIO lines to build or run against.

## Requirements

- macOS host (Apple silicon), Python 3.11+
- `ssh` and `rsync` on the host (both ship with macOS)
- A target reachable over SSH with key authentication - an ssh alias in
  `~/.ssh/config` that connects with no password prompt

## Install

```sh
git clone <this-repo-url> && cd pi4-agents && pip install -e .
```

```sh
perch --version
```

## Quickstart

1. Add an ssh alias for your target to `~/.ssh/config` (a literal
   `HostName` is more reliable than mDNS - see
   [docs/PHASE-1-BUILD-AND-RUN.md](docs/PHASE-1-BUILD-AND-RUN.md)):

   ```
   Host pi
       HostName 10.0.0.131
       User <your-username-on-the-target>
   ```

   Confirm it connects with no prompt: `ssh pi true`.

2. In your project's directory:

   ```sh
   perch init pi
   ```

   This writes `.perch.toml` (commented, and pre-filled if it finds a
   `Makefile` or `pyproject.toml`), `.vscode/tasks.json`, and `CLAUDE.md`,
   then runs `perch doctor` against the target - your first result is
   either "it works" or a precise statement of what's missing.

3. Build:

   ```sh
   perch build
   ```

   (`perch doctor` also works stand-alone at any point, read-only, to
   re-check the target's reachability, toolchain and disk.)

## See it work

<!--
  Hand-written transcript, not an actual asciinema recording (asciinema
  wasn't available when this was written) - but every line below is real,
  verified output from this exact sequence run against a live Raspberry Pi
  (see docs/PHASE-0-BUILD-AND-RUN.md and docs/PHASE-5-BUILD-AND-RUN.md for
  the full, dated transcripts this is drawn from).
-->

```
$ sed -i '' 's/;$//' src/main.c   # break it: remove a semicolon
$ perch build; echo "exit: $?"
gcc -Wall -o hello src/main.c
src/main.c: In function ‘main’:
/Users/towhid/perch-tryit/src/main.c:4:40: error: expected ‘;’ before ‘return’
    4 |     printf("hello from %s\n", "the pi")
      |                                        ^
      |                                        ;
    5 |     return 0;
      |     ~~~~~~
make: *** [Makefile:2: hello] Error 1
exit: 2

$ sed -i '' 's/"the pi")/"the pi");/' src/main.c   # fix it
$ perch run
hello from the pi
exit: 0
```

The compiler is the Pi's own `gcc`, running on the Pi - but the path in
the error (`/Users/towhid/perch-tryit/src/main.c`) is the file on *your*
Mac, already rewritten so an editor or an agent can open it directly at
line 4, column 40.

## Further reading

- [`architecture/ARCHITECTURE.md`](architecture/ARCHITECTURE.md) - the
  authoritative contract: invariants, components, the exit-code contract,
  the failure matrix.
- [`architecture/DEVELOPMENT-PLAN.md`](architecture/DEVELOPMENT-PLAN.md) -
  how it was built, phase by phase, and why.
- `docs/PHASE-0` through `docs/PHASE-5-BUILD-AND-RUN.md` - a from-scratch
  setup guide (Phase 0) plus what each later phase actually verified live
  against the target, including known gaps and troubleshooting tables.
- [`docs/PROJECT-BRIEF.md`](docs/PROJECT-BRIEF.md) - the original brief
  that motivated the project, kept as historical record.
- `CLAUDE.md` - the whole agent contract, in five lines.
