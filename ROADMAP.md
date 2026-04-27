# gitr - Roadmap

## Overview

`gitr` is a lightweight Git diff viewer and code review annotation tool.
It fills the gap between `gitk` (great for history browsing, no branch diff) and
full Git GUIs (too heavy) or web-based review tools (require a server).

Primary use case: reviewing LLM-agent-generated code across one or more branches,
with the ability to attach persistent local comments to specific diff lines.

---

## V1 - Diff Viewer + Annotations

- [x] Pipe-only: `git diff ... | gitr`
- [x] Direct invocation: `gitr master`, `gitr master HEAD`, `gitr --merge-base master`
- [x] Read from patch file: `gitr -p patch.diff`
- [x] Two-panel layout: diff on left, file list on right
- [x] File list: flat and tree view, status badge (A/M/D/R), per-file `+N -N` counts
- [x] Diff view: unified diff with coloured +/- lines, hunk separators
- [x] Sticky file header while scrolling
- [x] Minimap with viewport indicator
- [x] Smooth scrolling, keyboard navigation (n/p/Tab for next/prev file)
- [x] Config persistence: wrap, tree view, word diff
- [x] Word-level diff: highlight changed words, dim unchanged words within changed lines
- [ ] Inline annotations: double-click a line -> input box -> saved comment rendered inline
- [ ] Comments stored in `.gitr/comments.json` (repo-local, gitignored)
- [ ] Commented files marked in file list
- [ ] Robust comment anchoring: anchor to source file line + surrounding context,
      not diff line number, so comments survive rebases and merges

## Future

- [ ] Side-by-side diff view (toggle between unified and split)
- [ ] Line numbers alongside diff content
- [ ] Search within diff (`Ctrl+F`)
- [ ] Hunk navigation (`j` / `k` to jump between `@@` hunks within a file)
- [ ] Jump to `$EDITOR` at the correct line (`o`)
- [ ] Reload / refresh (`r`) — re-run the git source without restarting
- [ ] Fold / collapse individual file diffs
- [ ] Syntax highlighting per language (no extra deps — use `re`-based tokeniser)
- [ ] Font size adjustment (`Ctrl++` / `Ctrl+-`)
- [ ] Color theme override via `config.json`
- [ ] Keyboard shortcut help (`?`)
- [ ] Export diff as HTML

### Annotation storage format

`<repo-root>/.gitr/comments.json`

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

---

## Usage

```bash
gitr                         # git diff (unstaged changes)
gitr master                  # git diff master
gitr --merge-base master     # diff from common ancestor
gitr master HEAD             # committed changes only
git diff | gitr              # pipe a patch
gitr -p patch.diff           # read from a patch file
```

---

## Technology

- **Language:** Python 3.10+
- **GUI:** Tkinter (stdlib only, zero extra deps)
- **Install:** `uv tool install .`
- **Platform:** Linux-first (X11), should work on macOS/Windows
- **Distribution:** single file (`gitr.py`)
