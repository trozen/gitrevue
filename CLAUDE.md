# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`gitr` is a single-file (`gitr.py`) pipe-only Git diff viewer using Python 3.10+ and Tkinter (stdlib only, zero extra dependencies). It is installed as a CLI tool via `uv`.

## Commands

```bash
# Install / reinstall after changes
uv tool install --reinstall .

# Run without installing
uv run gitr

# Use it
git diff main...HEAD | gitr
git diff HEAD | gitr
git diff --cached | gitr
git show HEAD | gitr
```

No test suite yet. Syntax check:
```bash
python3 -c "import ast; ast.parse(open('gitr.py').read())"
```

## Architecture

Everything lives in `gitr.py`. The flow is:

1. `main()` reads all of stdin; if stdin is a tty, prints usage and exits.
2. `parse_diff(text)` splits the raw diff into `DiffFile` objects, each holding a list of `DiffLine(text, kind)` where `kind` is one of `added | removed | context | hunk | fileheader`.
3. `entries_from_diff()` derives `FileEntry` (path, status A/M/D/R, +/- counts) from the parsed diff — no extra git calls needed.
4. `try_current_branch()` is a best-effort git call for the top bar label; failure is silently ignored.
5. `App` builds the Tkinter UI: a top bar + `PanedWindow` with a diff `Text` widget (left, 70%) and a file-list `Text` widget (right, 30%). Both panels use tag-based colouring. Clicking a file row in the right panel scrolls the diff to that file's position.

## Constraints

- **No non-ASCII characters** anywhere in the source (no Unicode arrows, box-drawing chars, etc.).
- Single file, no dependencies beyond stdlib. Do not add packages.
- Colour scheme is Catppuccin Mocha (hex values in the `C` dict).
