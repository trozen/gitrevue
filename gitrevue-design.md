# gitrevue — Design Document

## Overview

`gitrevue` is a lightweight Git diff viewer and code review annotation tool.
It fills the gap between `gitk` (great for history browsing, no branch diff) and
full Git GUIs (too heavy) or web-based review tools (require a server).

Primary use case: reviewing LLM-agent-generated code across one or more branches,
with the ability to attach persistent local comments to specific diff lines.

---

## Versioning / Roadmap

### V1 — Diff Viewer
- Launch from repo dir with a ref arg: `gitrevue <ref>`
- Pipe support: `git diff ... | gitrevue`
- `--first-parent` flag: show only commits made on this branch, ignoring merges from parent
- Two-panel layout: diff on left, file list on right (gitk-style)
- File list with status badge (A/M/D/R) and per-file `+N -N` counts
- Diff view: standard unified diff with coloured +/- lines
- Toggle: compact (hunks only) vs whole file view
- Refresh button

### V2 — Annotations + Word-level Diff
- Word-level diff within changed lines (highlight changed words, not just whole lines)
- Inline annotations: double-click a line → input box → saved comment rendered inline
- Comments stored in `.gitrevue/comments.json` (repo-local, not committed)
- Commented files marked in file list

### V3 — Side-by-side + Robust Line Tracking
- Side-by-side diff view (toggle between unified and split)
- Robust comment line anchoring: survive rebases/merges by anchoring to source
  file line number + surrounding context, not diff line number

### V4+ — Polish & Integration
- Syntax highlighting per language
- Jump to `$EDITOR` at specific line
- Search within diff
- Export diff as HTML
- Keyboard navigation (next/prev hunk, next/prev file)

---

## Usage

```bash
# Compare current branch vs ref (three-dot diff: changes since branch diverged)
gitrevue master
gitrevue main
gitrevue HEAD~1
gitrevue origin/main
gitrevue abc1234

# Only show changes made on this branch, ignoring merges from parent branch
gitrevue --first-parent master

# Pipe any diff output directly into gitrevue
git diff master...HEAD | gitrevue
git show HEAD | gitrevue
git diff HEAD~3..HEAD~1 | gitrevue

# Default ref if none given: master
gitrevue
```

> `gitrevue <ref>` uses three-dot diff (`git diff <ref>...HEAD`), meaning:
> changes on the current branch since it diverged from `<ref>`, excluding
> anything that came in via merges from `<ref>`.

No UI controls for switching ref in V1 — just relaunch with a different arg.

---

## Technology

- **Language:** Python 3.10+
- **GUI:** Tkinter (stdlib only, zero extra deps)
- **Launcher:** `python3 gitrevue.py master` or copy to `~/bin/gitrevue` and run directly
- **Platform:** Linux-first (GNOME/X11), should work on macOS/Windows as-is
- **Distribution:** single file — easy to copy to `~/bin/`

---

## Layout

Two-panel horizontal split (resizable via sash), **diff on left, file list on right**
(consistent with gitk conventions):

```
┌─────────────────────────────────────────────────────────────────────┐
│  ⎇ feature-branch  ←  master          3 files, +42 -7    [≡] [↻]  │  ← top bar
├───────────────────────────────────────┬─────────────────────────────┤
│  --- a/src/foo.py                     │  M  src/foo.py    +8  -3   │
│  +++ b/src/foo.py                     │  A  src/bar.py    +21      │
│  @@ -10,6 +10,8 @@                   │  D  tests/old.py  -18      │
│   def hello():                        │                             │
│ -     return "hi"                     │                             │
│ +     return "hello world"            │                             │
│ +     # changed by agent              │                             │
│                                       │                             │
└───────────────────────────────────────┴─────────────────────────────┘
```

### Top bar

- Current branch name + ref being compared
- Short diffstat (`N files changed, +X -Y`)
- Compact/full toggle button `[≡]`
- Refresh button `[↻]`

### Left panel — diff view

- Standard unified diff rendering
- Colour scheme:
  - Added lines: green fg, dark green bg
  - Removed lines: red fg, dark red bg
  - Hunk headers (`@@`): yellow/amber
  - File headers (`---`/`+++`, `diff`, `index`): blue/muted
  - Context lines: default text colour
- Scrollable both axes
- In **compact mode** (default): hunks only, with surrounding context lines
- In **full file mode**: entire file content shown, with changes highlighted

### Right panel — file list

- One file per row
- Status badge: `M` (modified), `A` (added), `D` (deleted), `R` (renamed)
- Badge coloured: green=A, blue=M, red=D, purple=R
- Per-file `+N -N` line count shown after filename
- Clicking a file jumps to that file's diff in the left panel
- Selected file highlighted

---

## Git integration

All git operations via subprocess shell-out (no libgit2/pygit2 dependency):

```bash
git diff --name-status -M <ref>...HEAD        # file list with status
git diff --stat -M <ref>...HEAD               # per-file +/- counts
git diff -M <ref>...HEAD                      # full diff
git diff --first-parent -M <ref>...HEAD       # branch-only diff
git diff --shortstat <ref>...HEAD             # summary stat
git rev-parse --abbrev-ref HEAD               # current branch name
git rev-parse --short HEAD                    # current commit hash
git rev-parse --show-toplevel                 # repo root
git merge-base <ref> HEAD                     # branch-off point
```

When reading from stdin (pipe mode), git commands are skipped and the raw diff
is parsed directly. File list is derived from the diff headers.

---

## Annotations — V2

### Interaction

- **Double-click** any line in the diff → inline input box appears below that line
- **Enter** to save, **Escape** to cancel
- **Single-click** an existing comment → opens for editing
- Delete all text + Enter → removes the comment
- Commented files marked with `●` in the file list

### Comment display

```
  ┌──────────────────────────────────────────────────────┐
  │ ● this logic seems wrong, agent missed the edge      │
  │   case where x=0                          [line 42]  │
  └──────────────────────────────────────────────────────┘
```

### Storage

`<repo-root>/.gitrevue/comments.json`

```json
{
  "version": 1,
  "comments": [
    {
      "file": "src/foo.py",
      "line": 42,
      "text": "this logic seems wrong",
      "ref": "abc1234",
      "created_at": "2025-04-22T10:00:00"
    }
  ]
}
```

> V2 anchors comments to diff line numbers (simple, may shift after rebase).
> Robust anchoring deferred to V3.

### `.gitrevue/` setup

On first run:
1. Create `<repo-root>/.gitrevue/` if missing
2. Append `.gitrevue/` to `<repo-root>/.git/info/exclude` if not already present

---

## File structure

```
gitrevue.py    # single-file implementation, copy to ~/bin/gitrevue
```
