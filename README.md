# gitr

Lightweight terminal-launched Git diff viewer. Two-panel GUI: coloured diff on the left, clickable file list on the right.

## Install

```bash
uv tool install .
```

## Usage

```bash
gitr                         # git diff (unstaged changes)
gitr master                  # git diff master (to working tree)
gitr --merge-base master     # diff from common ancestor to working tree
gitr master HEAD             # git diff master HEAD (committed only)

git diff | gitr              # pipe a patch
gitr -                       # read stdin explicitly
gitr -p patch.diff           # read from a patch file
```

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
