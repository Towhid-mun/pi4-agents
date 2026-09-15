# Fixture provenance (P3-4)

Every `*.stdout.txt`/`*.stderr.txt` pair here is a **real, unedited capture**
from the actual target (`gcc (Debian 14.2.0-19) 14.2.0`, `GNU Make 4.4.1`,
`Python 3.13.5` on Raspberry Pi OS Trixie) — none of this was hand-written
from memory. `invalid_utf8.stderr.txt` is the one exception: synthetic bytes,
noted below, since coaxing a real compiler into emitting genuinely invalid
UTF-8 on purpose is byte construction, not a capture.

Trigger sources lived at `integration/fixtures/diag-sources/` (pushed to a
scratch project, `remote_root = "perch-diag-capture"`) and each capture was
`ssh pi "cd perch-diag-capture && <command>" > X.stdout.txt 2> X.stderr.txt`.

| Fixture | Command | Exit | What it's for |
|---|---|---|---|
| `single_error` | `gcc -Wall -o single_error single_error.c` | 1 | One error, with column |
| `single_error_color` | `gcc -Wall -fdiagnostics-color=always -o single_error single_error.c` | 1 | Same error, ANSI-coloured - real escape sequences (`\033[01m\033[K...`) sitting directly before the filename, confirmed by raw byte dump |
| `multi_error` | `gcc -Wall -o multi_error multi_error.c` | 1 | Several diagnostics in one run, including a `note:` that carries its own file:line:col |
| `warnings_only` | `gcc -Wall -Wextra -o warnings_only warnings_only.c` | 0 | Warnings, build still succeeds |
| `linker_error` | `gcc -Wall -o linker_error linker_main.c` | 1 | `/usr/bin/ld: ...` + `<file>:(.text+offset): undefined reference to ...` + `collect2: error: ...` - no line/col at all, and references a `/tmp/ccXXXXXX.o` path that must NOT be treated as a workspace file |
| `include_chain` | `gcc -Wall -o include_chain include_main.c` | 1 | `In file included from include_main.c:2:` before the real error, which is in a DIFFERENT file (`include_util.h`) than the one named in the chain line |
| `clean` | `gcc -Wall -o clean clean.c` | 0 | Nothing on either stream |
| `traceback` | `python3 traceback.py` | 1 | Three stack frames (absolute paths), terminated by a bare `ZeroDivisionError: division by zero` line with no file:line shape at all |
| `make_subdir` | `make -C subdir` | 2 | `make: Entering directory '/abs/path'` / `Leaving directory` on **stdout**; the gcc error itself on stderr names only `sub_error.c` - relative to `subdir`, not to `remote_root`. Confirms empirically that GNU Make prints these announcements by default under `-C`, which P3-2's cwd-tracking depends on. Also contains `make: *** [Makefile:2: sub_error] Error 1` - deliberately left unrecognized (see P3-1's report) |
| `noise` | `./noise.sh` | 0 | Diagnostic-*shaped* text that is not a diagnostic - colons, digits, absolute-looking paths, a line with no trailing newline. This is the I8 fixture |
| `invalid_utf8` | *(synthetic - see above)* | — | A gcc-shaped line with a bad byte sequence spliced in, to exercise `errors="replace"` decoding |

To re-capture after a target OS/toolchain upgrade: `integration/fixtures/diag-sources/.perch.toml` still points at `remote_root = "perch-diag-capture"` - `cd` there, `perch sync`, then re-run the commands in the table above over `ssh pi` and overwrite these files.
