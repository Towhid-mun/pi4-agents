# perch — User Guide

A practical, task-oriented walkthrough: configure SSH, create a project,
write a C++ and a Python "hello world," run `perch doctor`, and use every
command. If you want the *why* behind any of this, see
[`architecture/ARCHITECTURE.md`](../architecture/ARCHITECTURE.md); this
document is the *how*.

Everything below was run for real against a Raspberry Pi 4 while writing
this guide - every command block is real, verified output, not a guess at
what it should print.

## 0. Before you start

- macOS host, Python 3.11+, `ssh` and `rsync` (both ship with macOS).
- `perch` installed (`pip install -e .` from a clone of this repo - see
  the [README](../README.md) if you haven't done this yet).
- A Raspberry Pi (or any SSH-reachable Linux box) on the same network,
  with its own toolchain already on it (`gcc`/`g++`/`make`/`python3` -
  whatever your project needs). **Nothing perch-specific is ever
  installed on the target** - no daemon, no agent, nothing.

## 1. Configure SSH

perch never touches usernames, passwords, ports or keys itself - it
delegates all of that to your own `~/.ssh/config` and asks ssh for
exactly one thing: an **alias**. Setting this up once is most of the
work.

### 1.1 Set up key authentication (skip if you already have this)

```sh
ssh-copy-id youruser@10.0.0.131
```

This copies your public key to the target so ssh never asks for a
password. If you don't have a key pair yet, `ssh-keygen -t ed25519`
first.

### 1.2 Add an alias to `~/.ssh/config`

```
Host pi
    HostName 10.0.0.131
    User youruser
    ServerAliveInterval 30
```

Use a **literal IP address**, not the target's `.local` mDNS name, for
`HostName`. mDNS resolution turned out to be unreliable on the network
this tool was actually developed on - a literal address sidesteps that
failure mode entirely. Find the target's IP with `hostname -I` run on the
target itself, or check your router.

You don't strictly need a `~/.ssh/config` entry at all: if your local
username matches your account on the target and the default port/key
work, you can skip straight to using the bare IP address as the alias
(`host = "10.0.0.131"` in `.perch.toml`, no config file needed) - ssh
falls back to its own defaults. An alias is just less to type and lets
you change the underlying address, user or key later without touching
any project file.

### 1.3 Confirm it works with no prompt

```sh
ssh pi true
```

This should return **immediately and silently** - no password prompt, no
host-key confirmation (accept it once first if this is the very first
connection: `ssh pi` interactively, type `yes`, then re-run `ssh pi
true`). If this doesn't come back clean, nothing perch-specific will
work either - fix this first.

## 2. Create a project

### 2.1 A brand-new project

```sh
mkdir my-project && cd my-project
perch init pi
```

`perch init` writes three files and finishes by running `perch doctor`
against the target, so your very first result is either "it works" or a
precise statement of what's missing:

```
perch: wrote .perch.toml
perch: wrote .vscode/tasks.json
perch: wrote CLAUDE.md
perch: running `perch doctor` against 'pi'...
target           pi
reachable        yes
...
```

Omit the alias (`perch init`) and, if you have more than one `Host` entry
in `~/.ssh/config`, it lists them and asks you to pick one - typing the
alias directly (`perch init pi`) skips that and is what you'll want in a
script or an agent session, since the interactive prompt needs a real
terminal.

### 2.2 Adding perch to an existing codebase

`perch init` doesn't need an empty directory - run it from the root of
whatever you already have:

```sh
cd ~/projects/my-existing-thing
perch init pi
```

It writes the same three files alongside your existing source, and if it
finds a `Makefile` it pre-fills `commands.build = "make"` automatically
(a `pyproject.toml` gets `python3 -m build`). It never touches or deletes
any of your existing files, and never overwrites `.perch.toml` /
`.vscode/tasks.json` / `CLAUDE.md` if they're already there - re-run with
`--force` if you deliberately want fresh copies.

## 3. Understand `.perch.toml`

This is the one file that matters. Every field, with what it actually
does:

```toml
# The ssh alias to build/run on - resolved entirely by ssh via your own
# ~/.ssh/config. Never a hostname, user, port or key directly (those
# belong in ~/.ssh/config, never in this file).
host = "pi"

# Where the project's mirror lives on the TARGET, relative to $HOME there
# (or an absolute path, if you start it with "/"). perch creates this
# directory the first time it's needed.
remote_root = "perch/my-project"

# Extra glob patterns to exclude from the mirror, beyond the built-in
# list (.git/, .venv/, __pycache__/, node_modules/, *.o, *.pyc,
# .DS_Store, .perch.toml, .perch/).
exclude = ["build/", "*.bin"]

# Globs auto-retrieved from the target into .perch/artifacts/ after every
# successful build/test/run. See §7 below.
artifacts = ["hello", "*.log"]

[commands]
build = "make"
test  = "make test"
run   = "make -s && ./hello"
```

You only need to fill in the verbs you're actually going to use - if
`[commands]` has no `test` line, `perch test` fails with a clear message
naming exactly what to add; it doesn't need to exist for `perch build` to
work.

### Where does the code actually go?

- **Your working directory (wherever `.perch.toml` lives) is the only
  real copy.** Edit here, in your normal editor.
- **`host:remote_root`** (e.g. `pi:~/perch/my-project`) is a full mirror
  of it on the target, created and kept in sync automatically before
  every command. It is **entirely disposable** - never edit anything
  there directly over a separate `ssh` session, and never expect a file
  you only created on the target to survive the next command (see §7 for
  the one way to get it back). If you delete a file locally, the next
  sync deletes it there too.
- **`.perch/`** (in your local working directory) holds perch's own
  bookkeeping: `sync-cache.json` (a fingerprint used to skip redundant
  syncs) and `artifacts/` (where pulled files land). It's excluded from
  the mirror automatically - it never gets pushed to the target, and if
  you're using git, add `.perch/` to your own `.gitignore`.

## 4. `perch doctor`

Run this any time you want to check the target without touching your
project's build:

```sh
perch doctor
```

Real output, from this actual target:

```
target           pi
reachable        yes
control socket   /Users/towhid/.ssh/perch-85b42e1702877c85.sock (already live)

os               Debian GNU/Linux 13 (trixie)
kernel           6.18.34+rpt-rpi-v8
architecture     aarch64
memory           1.8Gi total, 1.3Gi available
disk (/)         21G free of 29G

gcc              gcc (Debian 14.2.0-19) 14.2.0
make             GNU Make 4.4.1
python3          Python 3.13.5
rsync            rsync  version 3.4.1  protocol version 32
libgpiod         present
/dev/i2c-*       /dev/i2c-20 /dev/i2c-21
/dev/gpiochip*   /dev/gpiochip0 /dev/gpiochip1 /dev/gpiochip4

host rsync       openrsync: protocol version 29   (this Mac, from S0-1)
```

It's entirely **read-only** - it never syncs your workspace, never needs
`[commands]` to exist, and is safe to run at any time, including before
`.perch.toml` even has real build commands filled in. A missing tool
shows as `MISSING` rather than making the whole command fail, so you can
see exactly what to install on the target (e.g. `sudo apt install g++`)
without perch guessing at it for you.

Run it whenever: you're not sure the target is reachable, you just
changed something on the target's OS, or a build is failing in a way that
smells like a missing tool rather than a bug in your code.

## 5. Hello World in C++

```sh
mkdir hello-cpp && cd hello-cpp
```

```cpp
// main.cpp
#include <iostream>

int main() {
    std::cout << "Hello from C++ on the Pi!" << std::endl;
    return 0;
}
```

```toml
# .perch.toml
host = "pi"
remote_root = "perch/hello-cpp"

[commands]
build = "g++ -Wall -O2 -o hello main.cpp"
run   = "g++ -Wall -O2 -o hello main.cpp && ./hello"
```

**Why `run` compiles again instead of just running `./hello`:** every
verb syncs first, and the mirror deletes anything on the target that
isn't in your local workspace - including a binary a *previous*
`perch build` produced, since that binary only ever existed on the
target. Rebuilding inside `run` itself is the only way `run` can rely on
having a fresh binary. (If you want a "just run, don't rebuild" verb for
a slow build, use `perch exec ./hello` right after a successful
`perch build` **in the same shell command** - not as a separate
invocation, for the same reason.)

```sh
perch build
```

```
.d..tp... ./
<f+++++++ main.cpp
```

```sh
perch run
```

```
*deleting hello

.d..t.... ./
Hello from C++ on the Pi!
```

Now break it on purpose:

```sh
sed -i '' 's/std::endl;/std::endl/' main.cpp   # drop the semicolon
perch build; echo "exit: $?"
```

```
*deleting hello

<fcst.... main.cpp
main.cpp: In function ‘int main()’:
/Users/you/hello-cpp/main.cpp:4:58: error: expected ‘;’ before ‘return’
    4 |     std::cout << "Hello from C++ on the Pi!" << std::endl
      |                                                          ^
      |                                                          ;
    5 |     return 0;
      |     ~~~~~~
exit: 1
```

That's the Pi's own `g++` (Debian 14.2.0), and the path in the error is
already rewritten to **your Mac's own path** to `main.cpp` - open it,
fix the semicolon, `perch build` again.

## 6. Hello World in Python

```sh
mkdir hello-py && cd hello-py
```

```python
# hello.py
print("Hello from Python on the Pi!")
```

```toml
# .perch.toml
host = "pi"
remote_root = "perch/hello-py"

[commands]
build = "python3 -m py_compile hello.py"   # a cheap "does it parse" sanity check
run   = "python3 hello.py"
```

Python has no real "build" step - `build` here is a syntax check, not
required. Unlike the C++ example, `run` does **not** need to rebuild
anything first: `python3 hello.py` runs your source directly every time,
and the source is exactly what the sync step just put there, so there is
no target-only artifact to lose.

```sh
perch run
```

```
.d...p... ./
<f+++++++ hello.py
Hello from Python on the Pi!
```

Break it on purpose, this time with a runtime error instead of a syntax
one:

```python
# hello.py
def greet():
    return 1 / 0

print("Hello from Python on the Pi!")
greet()
```

```sh
perch run
```

```
<fcst.... hello.py
Hello from Python on the Pi!
Traceback (most recent call last):
  File "/Users/you/hello-py/hello.py", line 5, in <module>
    greet()
    ~~~~~^^
  File "/Users/you/hello-py/hello.py", line 2, in greet
    return 1 / 0
           ~~^~~
ZeroDivisionError: division by zero
exit: 1
```

Same idea as the C++ case: both `File "..."` lines already name your
local path, at the right line, not the Pi's.

## 7. Getting files back from the target - `pull` and `[artifacts]`

The mirror only ever pushes host → target. Anything the target itself
*produces* (a compiled binary you want to keep, a log file, a sensor
capture) is gone on the next sync unless you retrieve it first.

### 7.1 Auto-pull

Add it to `[artifacts]` (top-level key, not inside `[commands]`) and it's
retrieved automatically after every successful `build`/`test`/`run`:

```toml
artifacts = ["*.log"]

[commands]
run = "python3 hello.py > output.log 2>&1; cat output.log"
```

```sh
perch run
```

```
Hello from Python on the Pi!
perch: pulled 1 file(s) matching '*.log' into /Users/you/hello-py/.perch/artifacts: output.log
```

It lands in `.perch/artifacts/`, which - not by accident - is already
excluded from the mirror, so a pulled file is never pushed back to the
target and can never overwrite a fresher target-side build with a stale
copy.

**Use a real glob (`*.log`, `output.*`), not a bare exact filename
(`output.log`), in `artifacts`.** A bare filename with no wildcard
character behaves differently from a real glob when the file doesn't
exist on the target at pull time: a real glob just matches nothing and
silently does nothing (the intended "not every run produces every
artifact" behavior); a literal filename with no wildcard is not
subject to that same-empty-match handling and can make the whole command
fail instead. Found while writing this guide - a real gap in the current
implementation, worth keeping in mind until it's tightened up.

### 7.2 Manual pull

```sh
perch pull "*.log" /tmp/my-logs
```

```
pulled 1 file(s) into /tmp/my-logs:
  output.log
```

Omit the destination and it goes to the same `.perch/artifacts/` default.
`pull` is read-only - it never syncs first, so it only sees whatever is
already on the target from the last thing that ran there.

## 8. Every command

```
perch init [alias] [--force]
perch doctor
perch sync [--force-sync]
perch build [args...] [--tty|--json] [--replace] [--force-sync]
perch test  [args...] [--tty|--json] [--replace] [--force-sync]
perch run   [args...] [--tty|--json] [--replace] [--force-sync]
perch exec  <cmd...>  [--tty|--json] [--replace] [--force-sync]
perch pull  <glob> [dest]
```

| Command | What it does | Notes |
|---|---|---|
| `perch init [alias]` | Scaffolds `.perch.toml`, `.vscode/tasks.json`, `CLAUDE.md`, then runs `doctor` | §2 above. `--force` overwrites files that already exist. |
| `perch doctor` | Read-only target probe: OS, arch, memory, disk, toolchain versions, device files | §4 above. Never syncs. |
| `perch sync` | Mirrors the workspace to the target and stops - nothing runs | Useful to pre-warm the target, or to check what would transfer without running anything. |
| `perch build [args...]` | Syncs, then runs `commands.build` | Extra `args` are appended to the configured command. |
| `perch test [args...]` | Syncs, then runs `commands.test` | Same shape as `build`. |
| `perch run [args...]` | Syncs, then runs `commands.run` | Same shape as `build`. Remember the rebuild note in §5. |
| `perch exec <cmd...>` | Syncs, then runs an arbitrary command in `remote_root` | For one-off things you don't want to add to `.perch.toml` - `perch exec ls -la`, `perch exec ./hello`, `perch exec uname -m`. |
| `perch pull <glob> [dest]` | Retrieves files matching `glob` (expanded on the **target's** shell) into `dest` | §7.2 above. Read-only, no run lock. |

### Flags

| Flag | Applies to | Effect |
|---|---|---|
| `--tty` | `build`/`test`/`run`/`exec` | Allocates a real pty, for a program that needs one (an interactive prompt, a full-screen TUI). Never combine with `--json` - a pty merges stdout/stderr into one stream that can't be classified into structured events. |
| `--json` | `build`/`test`/`run`/`exec` | Structured JSON events on stdout (`stdout`/`stderr`/`diag`/`exit` lines) instead of plain text - for scripts and agents, not for a human at a terminal. |
| `--replace` | `build`/`test`/`run`/`exec` | Takes the run lock from a still-running invocation of the same project (kills its process group first, confirmed dead). Without it, a held lock fails fast (exit 75) naming the holder's process group - useful if a previous run is genuinely stuck. |
| `--force-sync` | `sync`/`build`/`test`/`run`/`exec` | Forces a real rsync even if perch thinks nothing changed. Use this if you suspect the target has drifted out of band (e.g. you edited something over a direct `ssh` session) - perch's own change-detection can't see that. |
| `--force` | `init` only | Overwrites `.perch.toml` / `.vscode/tasks.json` / `CLAUDE.md` if they already exist. |

## 9. Editor and agent integration (brief)

- **VS Code:** `.vscode/tasks.json` (written by `perch init`) gives you
  build/test/run/doctor tasks with `Cmd+Shift+B` mapped to build, and
  compiler errors clickable straight from the Problems panel.
  **Never open the project through Remote-SSH connected to the
  target** - the task's shell would then run ON the target, so `perch`
  (which itself shells out to `ssh <alias>`) would try to SSH from the
  target to itself. `perch` also refuses to run at all on a non-macOS
  host as a safety net against exactly this.
- **Claude Code / an agent:** `CLAUDE.md` (also written by `perch init`)
  tells an agent to build/test/run only through perch, never locally.
  If you're working *on* this repo itself rather than a project that
  merely *uses* perch, this repository's own `.claude/settings.json`
  pre-approves perch's ordinary verbs so an agent loop doesn't stop for
  permission prompts on every build - `perch exec` is deliberately left
  out of that list, since it runs an arbitrary command on the target and
  there's no safe way to allow it except by always prompting.

## 10. Troubleshooting quick list

| Symptom | Likely cause |
|---|---|
| `ssh pi true` hangs or asks for a password | Fix this before anything perch-related - see §1.3 |
| `perch: no .perch.toml found...` | You're not inside a directory `perch init` (or a hand-written `.perch.toml`) ran in - it's found by walking up from your current directory |
| A build error's path isn't clickable / doesn't `ls` | Usually a compiler output shape the diagnostic mapper doesn't recognize yet |
| `perch run` can't find a binary a previous `perch build` made | §5 above - rebuild inside `run` itself |
| A build/test/run exits 75 | Another invocation (maybe from a different machine) holds the run lock - the message names its process group; `--replace` if you're sure it shouldn't be there |
| `perch build` seems to run against stale content | It printed "sync skipped" - re-run with `--force-sync`, especially if you edited something directly on the target over a separate `ssh` session |
| An `artifacts` entry sometimes fails the whole command | Use a real glob, not a bare filename - see §7.1 |
| `perch` refuses immediately, saying "not macOS" | You're running it on the target itself, most likely a VS Code Remote-SSH window |

## Where to go next

- [`README.md`](../README.md) - the short version, if you skipped it.
- [`architecture/ARCHITECTURE.md`](../architecture/ARCHITECTURE.md) - the
  full contract: invariants, exit codes, the failure matrix.
