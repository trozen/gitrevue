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
- [x] Inline annotations: hover button, `a` or the context menu opens an inline
      editor; the saved comment renders as a bar below the line
- [x] Comments stored in `.gitr/review.json` (repo-local) with file snapshots in
      `.gitr/snapshots/`
- [x] Comment kinds: note / good / bad, each with a colour and a text marker
      (`>>`, `++`, `!!`) used on screen, in the panels and in the terminal dump;
      one-click presets and a kind submenu in the context menu, a kind button on
      the comment and in the editor (Ctrl+1/2/3)
- [x] Comment markers on an overview strip beside the scrollbar
- [ ] Commented files marked in file list
- [x] Robust comment anchoring: each comment is anchored to a snapshot of the
      file taken when it was written; on load the snapshot is diffed against the
      current file to remap the line, so comments survive edits, rebases and
      merges (moved ones are flagged with `~`)

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

`<repo-root>/.gitr/review.json`, snapshots as plain text in `.gitr/snapshots/<sha1>`:

```json
{
  "files": {
    "src/foo.py": [
      {
        "snapshot": "<sha1 of the file when the comment was written>",
        "line_no": 42,
        "side": "+",
        "line_text": "+    return x",
        "comment": "this logic seems wrong",
        "kind": "bad"
      }
    ]
  }
}
```

`side` is `+`, `-` or a space (the diff line kind); `kind` is `note`, `good`
or `bad` and defaults to `note` when absent.

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
