# Agent instructions — Perch

Read `architecture/ARCHITECTURE.md` before any structural change.

- **Never build or run project code locally.** The host is the wrong
  architecture and has no hardware; only the target can compile and run it.
- **Every build/test/run goes through `perch`** (`perch build`, `perch test`,
  `perch run`, `perch exec <cmd>`), never a direct `ssh`.
- **The host workspace is the source of truth.** The target's copy is
  derived and disposable — the mirror deletes, so a file removed locally is
  removed there too.
- **Target-side output that matters comes back with `perch pull`.** Anything
  generated on the target and not pulled (or listed in `[artifacts]`) is
  lost on the next sync.
