#!/usr/bin/env python3
"""gitr - lightweight Git diff viewer"""

import argparse
import bisect
import difflib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import tkinter as tk
from collections import deque
from itertools import accumulate
from tkinter import font as tkfont
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path.home() / '.config' / 'gitr' / 'config.json'


USAGE = """\
usage:
  gitr                         # git diff (unstaged changes)
  gitr master                  # git diff master (to working tree)
  gitr --merge-base master     # diff from common ancestor to working tree
  gitr master HEAD             # git diff master HEAD (committed only)
  git diff | gitr              # pipe a patch
  gitr -                       # read stdin explicitly
  gitr -p patch.diff           # read from a patch file

  GITR_SCALE=2 gitr master   # scale UI up (HiDPI)
"""


# --data structures ----------------------------------------------------------

@dataclass
class FileEntry:
    path: str
    status: str              # A M D R
    additions: int = 0
    deletions: int = 0


@dataclass
class DiffLine:
    text: str
    kind: str                # added | removed | context | hunk | fileheader
    # Post-image line number (None for hunk/fileheader; for removed lines this
    # is the new-side position the line would occupy if restored, i.e. the
    # post-image line number of the next + or context line in the hunk).
    new_line_no: Optional[int] = None
    old_line_no: Optional[int] = None  # pre-image line number, similarly defined


@dataclass
class DiffFile:
    path: str
    lines: list[DiffLine] = field(default_factory=list)
    status: str = 'M'
    old_path: str = ''
    index: str = ''


@dataclass
class _CommentEditTarget:
    """In-flight state for the inline comment editor. Set when the editor
    opens, consumed (or discarded) when the user confirms or cancels."""
    file: str
    new_line_no: int
    side: str
    line_text: str
    # Non-None when editing an existing comment; None for a fresh comment
    # whose snapshot will be taken at confirm time.
    existing_snapshot: Optional[str] = None
    existing_snap_line_no: Optional[int] = None


@dataclass
class _ResolvedAnchor:
    """A comment entry mapped through its snapshot to a position in the
    current diff. ``target_line_no`` is the post-image line number we expect
    to find the line at (after diffing the snapshot vs. the current file);
    ``moved`` is set when the line shifted or its text changed."""
    file: str
    snapshot: str
    snap_line_no: int       # line_no stored with the entry
    target_line_no: int     # snap_line_no remapped to the current file
    side: str               # '+', '-', or ' '
    line_text: str          # diff line text including +/-/ prefix
    comment: str
    moved: bool = False
    matched: bool = False
    src_line: Optional[int] = None  # diff Text line number once rendered
    frame: Optional[tk.Frame] = None  # the rendered comment widget, once embedded
    label: Optional[tk.Label] = None  # its text, re-wrapped when the width changes


# --git helpers ------------------------------------------------------------

def try_current_branch() -> str:
    r = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


# --diff sources ------------------------------------------------------------

class PatchSource:
    """Diff text from stdin or a patch file. Cannot fetch full file contents."""
    def __init__(self, text: str, label: str = '') -> None:
        self._text = text
        self._label = label

    def diff_text(self) -> str:
        return self._text

    def label(self) -> str:
        return self._label

    def commits(self) -> list[tuple[str, str]]:
        return []

    def has_staged(self) -> bool:
        return False

    def has_unstaged(self) -> bool:
        return False

    def can_reload(self) -> bool:
        return False

class GitSource:
    """Diff text from a live git invocation. Can also fetch full file contents."""
    def __init__(self, refs: list[str], merge_base: bool = False) -> None:
        self._refs = refs
        self._merge_base = merge_base

    def diff_text(self) -> str:
        try:
            if self._merge_base:
                sha = subprocess.check_output(
                    ['git', 'merge-base', self._refs[0], 'HEAD'],
                    text=True, stderr=subprocess.PIPE).strip()
                return subprocess.check_output(
                    ['git', 'diff', '--no-color', sha], text=True, stderr=subprocess.PIPE)
            return subprocess.check_output(
                ['git', 'diff', '--no-color'] + self._refs, text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            sys.exit(f'gitr: git command failed: {e.stderr.strip()}')
        except FileNotFoundError:
            sys.exit('gitr: git not found in PATH')

    def label(self) -> str:
        if self._merge_base:
            return f'--merge-base {self._refs[0]}'
        return ' '.join(self._refs)

    @staticmethod
    def _has_changes(cmd: list[str]) -> bool:
        try:
            return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0
        except FileNotFoundError:
            return False

    def has_staged(self) -> bool:
        return self._has_changes(['git', 'diff', '--cached', '--quiet'])

    def has_unstaged(self) -> bool:
        return self._has_changes(['git', 'diff', '--quiet'])

    def can_reload(self) -> bool:
        return True

    def commits(self) -> list[tuple[str, str]]:
        try:
            if self._merge_base:
                sha = subprocess.check_output(
                    ['git', 'merge-base', self._refs[0], 'HEAD'],
                    text=True, stderr=subprocess.PIPE).strip()
                range_arg = f'{sha}..HEAD'
            elif len(self._refs) == 0:
                return []
            elif len(self._refs) == 1:
                r = self._refs[0]
                range_arg = r.replace('...', '..') if '..' in r else f'{r}..HEAD'
            elif len(self._refs) == 2:
                range_arg = f'{self._refs[0]}..{self._refs[1]}'
            else:
                return []
            out = subprocess.check_output(
                ['git', 'log', '--pretty=format:%h%x09%s', range_arg],
                text=True, stderr=subprocess.PIPE)
            return [tuple(line.split('\t', 1)) for line in out.splitlines() if '\t' in line]
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []



def _find_repo_root() -> 'Path | None':
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / '.git').is_dir() or (d / '.git').is_file():
            return d
    return None


def _find_gitr_dir() -> 'Path | None':
    root = _find_repo_root()
    return (root / '.gitr') if root else None


def _read_text_safe(path: Path) -> 'str | None':
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _load_window_state() -> 'dict | None':
    gitr_dir = _find_gitr_dir()
    if not gitr_dir:
        return None
    p = gitr_dir / 'window.json'
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _save_window_state(geometry: str, sash_ratio: float, top_line: int) -> None:
    gitr_dir = _find_gitr_dir()
    if not gitr_dir:
        return
    state = {'geometry': geometry, 'sash_ratio': sash_ratio, 'top_line': top_line}
    try:
        gitr_dir.mkdir(parents=True, exist_ok=True)
        (gitr_dir / 'window.json').write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _compute_line_map(snapshot: str, current: str) -> dict[int, int]:
    """Return a mapping {snapshot_line_no -> current_line_no} (1-based).

    For 'equal' opcode blocks the mapping is exact. For 'replace' / 'delete'
    blocks each snapshot line maps to the closest surviving current line
    (clamped to the new block's range, or to the line just before for pure
    deletes). The caller decides what to do with such mappings (typically
    flag the comment as 'moved').
    """
    a = snapshot.splitlines()
    b = current.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                out[i1 + k + 1] = j1 + k + 1
        elif tag == 'replace':
            span_b = max(j2 - j1, 1)
            for k in range(i2 - i1):
                out[i1 + k + 1] = min(j2, j1 + k * span_b // max(i2 - i1, 1)) + 1
        elif tag == 'delete':
            # Anchor each deleted snapshot line to the surviving current line
            # right before the delete. For a pure prefix delete (j1 == 0)
            # there's no surviving line above, so leave it unmapped — the
            # caller falls back to the original line_no, which won't match
            # any rendered diff line and routes the comment to the orphan
            # section instead of attaching it to unrelated code at line 1.
            if j1 == 0:
                continue
            for k in range(i2 - i1):
                out[i1 + k + 1] = j1
    return out


class ReviewStore:
    """Comments anchored to a snapshot of the file at creation time. On
    lookup the snapshot is diffed against the current working-tree file to
    remap line numbers, so a comment stays near the right line even after
    the file is edited.

    JSON layout (.gitr/review.json):
      {"files": {"<path>": [
          {"snapshot": "<sha1>", "line_no": <int>, "side": "+|-| ",
           "line_text": "<diff line text>", "comment": "<text>"}
      ]}}

    Snapshot blobs live in .gitr/snapshots/<sha1> as plain text. Multiple
    comments on the same file at the same time share one snapshot.

    TODO: GC unreferenced snapshot files periodically (e.g. on store load
    when the count exceeds a threshold).
    """

    def __init__(self) -> None:
        gitr_dir = _find_gitr_dir()
        self._gitr_dir = gitr_dir
        self._path = (gitr_dir / 'review.json') if gitr_dir else None
        self._snap_dir = (gitr_dir / 'snapshots') if gitr_dir else None
        self._data: dict[str, list[dict]] = {}
        # Cache of snapshot content keyed by sha; populated on demand.
        self._snap_cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not (self._path and self._path.exists()):
            return
        try:
            obj = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict):
            # Old list-shaped review.json from before the snapshot anchor
            # rewrite. The comments there don't have snapshots and can't be
            # remapped, so they're ignored. Tell the user once so the data
            # vanishing isn't a surprise.
            print(f'gitr: ignoring legacy {self._path} '
                  '(snapshot-based anchors required)', file=sys.stderr)
            return
        files = obj.get('files')
        if isinstance(files, dict):
            self._data = {f: list(entries) for f, entries in files.items()
                          if isinstance(entries, list)}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({'files': self._data}, indent=2))
        except OSError:
            pass

    def write_snapshot(self, content: str) -> 'str | None':
        """Persist ``content`` under .gitr/snapshots/<sha1> and return the sha,
        or ``None`` if the snapshot can't be persisted. Returning ``None``
        prevents callers from storing an entry whose snapshot won't survive
        this process — without persistence the in-memory cache is the only
        copy and remap on the next run would silently degrade."""
        sha = hashlib.sha1(content.encode('utf-8', errors='replace')).hexdigest()
        if not self._snap_dir:
            return None
        try:
            self._snap_dir.mkdir(parents=True, exist_ok=True)
            p = self._snap_dir / sha
            if not p.exists():
                p.write_text(content)
        except OSError as e:
            print(f'gitr: cannot write snapshot {sha}: {e}', file=sys.stderr)
            return None
        self._snap_cache[sha] = content
        return sha

    def read_snapshot(self, sha: str) -> 'str | None':
        if sha in self._snap_cache:
            return self._snap_cache[sha]
        if not self._snap_dir:
            return None
        text = _read_text_safe(self._snap_dir / sha)
        if text is not None:
            self._snap_cache[sha] = text
        return text

    def add(self, file: str, snapshot: str, line_no: int, side: str,
            line_text: str, comment: str) -> None:
        entries = self._data.setdefault(file, [])
        for e in entries:
            if (e.get('snapshot') == snapshot and e.get('line_no') == line_no
                    and e.get('side') == side):
                e['line_text'] = line_text
                e['comment'] = comment
                self._save()
                return
        entries.append({'snapshot': snapshot, 'line_no': line_no, 'side': side,
                        'line_text': line_text, 'comment': comment})
        self._save()

    def delete(self, file: str, snapshot: str, line_no: int, side: str) -> None:
        entries = self._data.get(file)
        if not entries:
            return
        for i, e in enumerate(entries):
            if (e.get('snapshot') == snapshot and e.get('line_no') == line_no
                    and e.get('side') == side):
                del entries[i]
                if not entries:
                    del self._data[file]
                self._save()
                return

    def all_entries(self) -> list[tuple[str, dict]]:
        return [(f, e) for f in sorted(self._data) for e in self._data[f]]

    def is_empty(self) -> bool:
        return not any(self._data.values())

    def clear(self) -> None:
        if not self._data:
            return
        self._data.clear()
        self._save()


# --diff parsing ------------------------------------------------------------

_FILEHEADER_PREFIXES = (
    'diff ', 'index ', '--- ', '+++ ',
    'new file', 'deleted file', 'old mode', 'new mode', 'rename ',
)


def _classify(line: str) -> str:
    if line.startswith(_FILEHEADER_PREFIXES):
        return 'fileheader'
    if line.startswith('@@ '):
        return 'hunk'
    if line.startswith('+'):
        return 'added'
    if line.startswith('-'):
        return 'removed'
    return 'context'


_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def parse_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: Optional[DiffFile] = None
    cur_old = cur_new = 0  # next line numbers within the active hunk

    for raw in text.splitlines():
        if raw.startswith('diff --git '):
            if current is not None:
                files.append(current)
            b_idx = raw.rfind(' b/')
            path = raw[b_idx + 3:] if b_idx != -1 else 'unknown'
            current = DiffFile(path)
            cur_old = cur_new = 0
        if current is not None:
            kind = _classify(raw)
            new_no: Optional[int] = None
            old_no: Optional[int] = None
            if kind == 'hunk':
                m = _HUNK_RE.match(raw)
                if m:
                    cur_old = int(m.group(1))
                    cur_new = int(m.group(2))
            elif kind == 'context':
                old_no, new_no = cur_old, cur_new
                cur_old += 1
                cur_new += 1
            elif kind == 'added':
                new_no = cur_new
                cur_new += 1
            elif kind == 'removed':
                old_no = cur_old
                # Anchor a removed line to the post-image position it sits
                # between (same as the next + / context line that follows it).
                new_no = cur_new
                cur_old += 1
            dl = DiffLine(raw, kind, new_line_no=new_no, old_line_no=old_no)
            current.lines.append(dl)
            if kind == 'fileheader':
                if raw.startswith('new file'):
                    current.status = 'A'
                elif raw.startswith('deleted file'):
                    current.status = 'D'
                elif raw.startswith('rename from '):
                    current.status = 'R'
                    current.old_path = raw[len('rename from '):]
                elif raw.startswith('index '):
                    current.index = raw

    if current is not None:
        files.append(current)
    return files


def entries_from_diff(diff_files: list[DiffFile]) -> list[FileEntry]:
    return [
        FileEntry(df.path, df.status,
                  sum(1 for l in df.lines if l.kind == 'added'),
                  sum(1 for l in df.lines if l.kind == 'removed'))
        for df in diff_files
    ]


def _build_tree_rows(
    entries: list[FileEntry],
) -> list[tuple[str, int, 'FileEntry | None']]:
    """Return flat render list for tree view: (label, depth, entry_or_None).

    Directories with a single subdirectory and no files are folded into their
    child, so e.g. src/ -> main/ -> foo.py becomes ('src/main/', 0, None).
    Children at each level are ordered by their earliest position in the
    original entry list so tree order matches the diff panel order.
    """
    trie: dict = {}
    for i, e in enumerate(entries):
        parts = e.path.split('/')
        node = trie
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (i, e)  # leaf stores original index for ordering

    rows: list[tuple[str, int, 'FileEntry | None']] = []
    _walk_trie(trie, rows, 0, '')
    return rows


def _trie_min_idx(node: dict) -> int:
    best = 10 ** 9
    for v in node.values():
        if isinstance(v, tuple):
            best = min(best, v[0])
        elif isinstance(v, dict):
            best = min(best, _trie_min_idx(v))
    return best


def _walk_trie(node: dict, rows: list, depth: int, dir_label: str) -> None:
    leaves = [(k, v) for k, v in node.items() if isinstance(v, tuple)]
    dirs   = [(k, v) for k, v in node.items() if isinstance(v, dict)]

    # Fold single-child dir chains that contain no files.
    if not leaves and len(dirs) == 1:
        name, child = dirs[0]
        _walk_trie(child, rows, depth, dir_label + name + '/')
        return

    if dir_label:
        rows.append((dir_label, depth, None))
        depth += 1

    # Interleave files and subdirs in original diff order.
    children: list[tuple[int, str, 'FileEntry | None', 'dict | None']] = []
    for name, (idx, entry) in leaves:
        children.append((idx, name, entry, None))
    for name, child in dirs:
        children.append((_trie_min_idx(child), name, None, child))
    children.sort(key=lambda x: x[0])

    for _, name, entry, child in children:
        if entry is not None:
            rows.append((name, depth, entry))
        else:
            _walk_trie(child, rows, depth, name + '/')


def _common_dir_prefix(prev: str, curr: str) -> str:
    """Return the directory prefix shared between prev and curr, at path boundaries."""
    prev_dirs = prev.split('/')[:-1]
    curr_dirs = curr.split('/')[:-1]
    common = []
    for a, b in zip(prev_dirs, curr_dirs):
        if a == b:
            common.append(a)
        else:
            break
    return '/'.join(common) + '/' if common else ''


# --config -------------------------------------------------------------------

class CFG:
    font_family        = 'monospace'
    font_size          = 12
    menu_font_size     = 8
    window_scale       = 0.75    # fraction of screen size on startup
    sash_ratio         = 0.70
    pane_min_w         = 180     # px; neither sash pane collapses below this
    scrollbar_w        = 16
    minimap_w          = 160
    overview_w         = 6     # comment marker strip beside the scrollbar
    overview_file_ticks = True # faint tick per file start on the strip
    scroll_speed       = 8   # lines per mouse-wheel tick
    diff_hi_blend      = 0.12   # bg intensity for changed lines / word-diff changed words
    diff_dim_blend     = 0.06   # bg intensity for word-diff unchanged words
    diff_dim_fg        = 0.50   # fg intensity for word-diff unchanged words
    word_diff_min_ratio = 0.35  # below this similarity, fall back to plain line diff
    word_diff_autojunk_tokens = 300  # longer lines use difflib's junk heuristic (much faster)
    word_diff_max_block = 40000  # removed x added lines above which pairing is sequential
    progress_interval_ms = 100  # how often the render progress label is refreshed
    sigint_poll_ms       = 200  # how often the event loop wakes to notice Ctrl+C
    comment_wrap_min_cols = 20  # narrower than this: do not wrap comment text
    render_chunk_ms    = 30   # word-diff highlighting is applied in chunks of about this long
    hover_hide_delay_ms     = 150
    hover_btn_leave_delay_ms = 80
    edit_focus_out_delay_ms = 50
    list_pane_max_lines      = 10
    menu_label_max_len       = 80
    section_collapsed_arrow  = '▶'
    section_expanded_arrow   = '▼'


# --colour scheme (dracula) --------------------------------------------------

C = {
    'bg':            '#282a36',
    'fg':            '#f8f8f2',
    'added_fg':      '#50fa7b',
    'added_bg':      '#283636',
    'removed_fg':    '#ff5555',
    'removed_bg':    '#342a36',
    'hunk_fg':       '#ffb86c',
    'fileheader_fg': '#bd93f9',
    'subdued':       '#6272a4',
    'topbar_bg':     '#44475a',
    'selected_bg':   '#44475a',
    'status_A':      '#50fa7b',
    'status_M':      '#bd93f9',
    'status_D':      '#ff5555',
    'status_R':      '#ff79c6',
    'comment_fg':    '#f1fa8c',
}



def _blend(color: str, factor: float = 0.5) -> str:
    """Blend color toward the canvas background by factor (0=bg, 1=color)."""
    bg = C['bg']
    def _p(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r0, g0, b0 = _p(bg)
    r1, g1, b1 = _p(color)
    r = int(r0 + (r1 - r0) * factor)
    g = int(g0 + (g1 - g0) * factor)
    b = int(b0 + (b1 - b0) * factor)
    return f'#{r:02x}{g:02x}{b:02x}'


def _wheel_ticks(delta: int) -> int:
    """Scroll ticks for a <MouseWheel> delta (positive delta = wheel up).

    Windows reports multiples of 120, macOS and precision touchpads report
    small values; floor division by 120 would turn those into zero ticks on
    one direction and a full tick on the other.
    """
    return (-1 if delta > 0 else 1) * max(1, abs(delta) // 120)


def _mix(c1: str, c2: str, t: float) -> str:
    """Linear interpolation between two hex colors (t=0 → c1, t=1 → c2)."""
    def _p(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r1, g1, b1 = _p(c1)
    r2, g2, b2 = _p(c2)
    return f'#{int(r1+(r2-r1)*t):02x}{int(g1+(g2-g1)*t):02x}{int(b1+(b2-b1)*t):02x}'


# non-whitespace pixel colours in the minimap; None = leave as canvas bg
_MINIMAP_COLORS: dict[str, str | None] = {
    'added':    _blend(C['added_fg'],      0.45),
    'removed':  _blend(C['removed_fg'],    0.45),
    'hunk':     _blend(C['hunk_fg'],       0.35),
    'filehdr':  C['topbar_bg'],  # same bar colour as the diff's header rows
    'fileidx':  C['topbar_bg'],
    'context':  _blend(C['fg'], 0.18),
    'reindent': _blend(C['fg'], 0.18),
    'comment':  C['bg'],  # ink; the row base is the comment bar colour
    'orphan':   _blend(C['subdued'], 0.40),
}


# --application ------------------------------------------------------------

def _detect_scale(root: tk.Tk) -> float:
    """Return UI scale factor: 1.0 = 96 DPI (standard), 2.0 = HiDPI, etc.

    GITR_SCALE env var overrides auto-detection.
    """
    env = os.environ.get('GITR_SCALE')
    if env:
        try:
            return max(0.25, float(env))
        except ValueError:
            pass
    # winfo_fpixels('1i') = pixels per inch; 96 is the baseline for scale=1.
    dpi = root.winfo_fpixels('1i')
    return dpi / 96.0


def _primary_monitor_size() -> tuple[int, int]:
    try:
        out = subprocess.run(['xrandr', '--query'], capture_output=True, text=True,
                             timeout=1).stdout
        for line in out.splitlines():
            if 'primary' in line:
                m = re.search(r'(\d+)x(\d+)', line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        # no primary keyword: use the first connected monitor
        for line in out.splitlines():
            if ' connected' in line:
                m = re.search(r'(\d+)x(\d+)', line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


_MM_LINE_H = 2  # minimap pixels per source line at 1x scale (matches VS Code)
_MM_LEVELS = (0.0, 0.4, 0.6, 0.8, 1.0)  # brightness per character density level


def _mm_density_table() -> bytes:
    """Translate table: latin-1 byte -> density level index into _MM_LEVELS.

    Stands in for VS Code's glyph rasterisation: heavier characters get
    brighter pixels so the shape of the code stays recognisable at 1px/char.
    """
    lvl = bytearray(256)
    for b in range(33, 127):
        ch = chr(b)
        if ch in ".,'`:;":
            lvl[b] = 1
        elif ch in '-_=~^"*+|/\\<>()[]{}!?':
            lvl[b] = 2
        elif ch.islower() or ch.isdigit():
            lvl[b] = 3
        else:
            lvl[b] = 4
    for b in range(128, 256):
        lvl[b] = 3
    return bytes(lvl)


def _mm_channel_tables(color: str, base: str = C['bg']) -> tuple[bytes, bytes, bytes]:
    """Per-channel translate tables: density level byte -> colour channel byte.

    Level 0 (no ink) is `base`; comment rows use a tinted base so the row
    reads as a comment even where its text is short.
    """
    levels = bytes(range(len(_MM_LEVELS)))
    chans: tuple[list[int], list[int], list[int]] = ([], [], [])
    for f in _MM_LEVELS:
        h = _mix(base, color, f).lstrip('#')
        for k in range(3):
            chans[k].append(int(h[2 * k:2 * k + 2], 16))
    r, g, b = (bytes.maketrans(levels, bytes(ch)) for ch in chans)
    return r, g, b


_MM_DENSITY = _mm_density_table()
# Drawn as full-width bars rather than text density: a one-line label is
# invisible at 1 px per character, and these rows mark boundaries.
_MM_SOLID_KINDS = frozenset(('filehdr', 'fileidx'))
# Comment rows mirror the diff's comment bar: dark text on a yellow band.
_MM_ROW_BASE = {'comment': _blend(C['comment_fg'], 0.55)}
_MM_CHANNELS = {kind: _mm_channel_tables(col, _MM_ROW_BASE.get(kind, C['bg']))
                for kind, col in _MINIMAP_COLORS.items() if col is not None}


def _use_autojunk(a: list[str], b: list[str]) -> bool:
    """Exact matching is quadratic in line length; on very long lines (prose,
    minified data) difflib's autojunk heuristic is ~25x faster at the cost of
    slightly fewer matched tokens, so it is switched on only for those."""
    return max(len(a), len(b)) > CFG.word_diff_autojunk_tokens


def _word_matcher(a: list[str], b: list[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, a, b, autojunk=_use_autojunk(a, b))


def _pair_lines_for_word_diff(
    rem_lines: list[str], add_lines: list[str]
) -> list[tuple]:
    """Order-preserving optimal matching between removed and added lines.

    Returns a list of ('pair', old, new, same_tokens), ('rem', old), or
    ('add', new); same_tokens is True when the lines differ only in whitespace.
    Uses DP to maximise total similarity, so a re-indented block mixed with
    inserted/deleted lines gets correctly paired rather than sequentially
    mis-matched.  Lines with similarity below CFG.word_diff_min_ratio are
    left unpaired and rendered as plain removed/added.
    """
    m, n = len(rem_lines), len(add_lines)

    def nws_tokens(text: str) -> list[str]:
        return [t for t in re.findall(r'\w+|[^\w\s]|\s+', text) if not t.isspace()]

    tok_rem = [nws_tokens(line) for line in rem_lines]
    tok_add = [nws_tokens(line) for line in add_lines]

    thr = CFG.word_diff_min_ratio
    if m * n > CFG.word_diff_max_block:
        # The optimal matching is quadratic (a 500x500 block takes seconds);
        # pair lines in order instead, as git's word diff does, and only
        # check each pair's similarity.
        result: list[tuple] = []
        k = min(m, n)
        for i in range(k):
            sm = _word_matcher(tok_rem[i], tok_add[i])
            if sm.real_quick_ratio() >= thr and sm.quick_ratio() >= thr and sm.ratio() >= thr:
                result.append(('pair', rem_lines[i], add_lines[i], tok_rem[i] == tok_add[i]))
            else:
                result.append(('rem', rem_lines[i]))
                result.append(('add', add_lines[i]))
        result.extend(('rem', line) for line in rem_lines[k:])
        result.extend(('add', line) for line in add_lines[k:])
        return result
    sims = [[0.0] * n for _ in range(m)]
    for j, b in enumerate(tok_add):
        # One matcher per added line: set_seq2 builds the b-side index that
        # every removed line is compared against, so reuse it instead of
        # rebuilding it m times.  The autojunk choice is per pair.
        exact = difflib.SequenceMatcher(None, autojunk=False)
        exact.set_seq2(b)
        junk: 'difflib.SequenceMatcher | None' = None
        for i, a in enumerate(tok_rem):
            if _use_autojunk(a, b):
                if junk is None:
                    junk = difflib.SequenceMatcher(None, autojunk=True)
                    junk.set_seq2(b)
                sm = junk
            else:
                sm = exact
            sm.set_seq1(a)
            # Only pairs at or above the threshold feed the DP, and most cross
            # pairs in a big block are far below it: the two cheap upper
            # bounds let us skip the expensive ratio() for those.
            if sm.real_quick_ratio() < thr or sm.quick_ratio() < thr:
                continue
            sims[i][j] = sm.ratio()

    # dp[i][j] = best total similarity pairing rem[0..i-1] with add[0..j-1]
    dp     = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [['']  * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            best, ch = dp[i - 1][j], 'rem'
            if dp[i][j - 1] >= best:          # prefer 'add' on tie → removes-before-adds when unpaired
                best, ch = dp[i][j - 1], 'add'
            if sims[i - 1][j - 1] >= CFG.word_diff_min_ratio:
                v = dp[i - 1][j - 1] + sims[i - 1][j - 1]
                if v > best:
                    best, ch = v, 'pair'
            dp[i][j] = best
            choice[i][j] = ch

    result: list[tuple] = []
    i, j = m, n
    while i > 0 or j > 0:
        if i == 0:
            result.append(('add', add_lines[j - 1]))
            j -= 1
        elif j == 0:
            result.append(('rem', rem_lines[i - 1]))
            i -= 1
        elif choice[i][j] == 'pair':
            result.append(('pair', rem_lines[i - 1], add_lines[j - 1],
                           tok_rem[i - 1] == tok_add[j - 1]))
            i -= 1
            j -= 1
        elif choice[i][j] == 'rem':
            result.append(('rem', rem_lines[i - 1]))
            i -= 1
        else:
            result.append(('add', add_lines[j - 1]))
            j -= 1
    result.reverse()
    return result


class App:
    def __init__(self, root: tk.Tk, diff_text: str,
                 commits: 'list[tuple[str, str]] | None' = None,
                 has_staged: bool = False, has_unstaged: bool = False,
                 source: 'PatchSource | GitSource | None' = None) -> None:
        self.root = root
        # Override Text/Entry class bindings so Ctrl+W/Q always close the window
        # (default Text binding for Ctrl+W is "delete previous word", which would
        # otherwise consume the event before our bind_all reaches it).
        for cls in ('Text', 'Entry'):
            root.bind_class(cls, '<Control-w>', lambda e: self._close_app())
            root.bind_class(cls, '<Control-q>', lambda e: self._close_app())
        root.bind_all('<Control-w>', lambda e: self._close_app())
        root.bind_all('<Control-q>', lambda e: self._close_app())
        root.protocol('WM_DELETE_WINDOW', self._close_app)
        self.diff_text = diff_text
        self._entries: list[FileEntry] = []
        self._diff_files: list[DiffFile] = []
        self._positions: dict[str, str] = {}
        self._pos_order: list[tuple[int, str]] = []
        # One (kind, text) per diff Text line, so row i is text line i+1.
        self._minimap_lines: list[tuple[str, str]] = []
        # Rasterised pixel row per minimap line, keyed by line index. Lines are
        # only ever appended during a render, so a row never goes stale until
        # the next render clears the cache.
        self._mm_rows: dict[int, bytes] = {}
        self._mm_rows_w: int = 0
        self._mm_base_rows: dict[tuple[str, int], bytes] = {}
        self._mm_painted: 'tuple[int, int] | None' = None  # (offset, band_h)
        # With wrapping on, a line spans ceil(len / cols) minimap rows so the
        # minimap matches what the screen shows; _mm_row_start[i] is the row
        # of line i (None when rows and lines coincide).
        self._mm_cols: 'int | None' = None
        self._mm_row_start: 'list[int] | None' = None
        self._mm_relayout_id: 'str | None' = None
        self._mm_img: 'tk.PhotoImage | None' = None
        self._mm_item: int = 0
        self._mm_drag: 'tuple[int, float] | None' = None
        # Diff panel text is queued and inserted in bulk; see _emit.
        self._buf: list[tuple[str, list[str]]] = []  # (tag, text parts)
        self._cur_line: int = 0
        # Hunk separators are embedded after the flush that creates their
        # line, so a hunk boundary does not force an extra insert call.
        self._pending_seps: list[tuple[int, tk.Canvas]] = []
        # Word-diff pairs rendered plain by the skeleton and highlighted
        # later: (removed line, added line, old text, new text).
        self._wd_pending: list[tuple[int, int, str, str]] = []
        self._wd_down: 'deque[tuple[int, int, str, str]]' = deque()
        self._wd_up: 'deque[tuple[int, int, str, str]]' = deque()
        self._wd_after_id: 'str | None' = None
        # Inline comment editor: the widget line it occupies, and whether
        # that line was inserted for it (a new comment) or belongs to an
        # existing comment being edited.
        self._editor_line: 'int | None' = None
        self._editor_is_new: bool = False
        self._loaded: bool = False  # the deferred first _load has run
        self._rendering: bool = False  # inside a synchronous parse/render
        self._interrupted: bool = False  # closed by Ctrl+C: exit status 130
        self._scroll_after_id: 'str | None' = None
        self._scroll_edge: int = 0  # +1/-1 while a Home/End run is heading for that edge
        self._pending_scroll_line: 'int | None' = None
        self._hunk_seps: list[tk.Canvas] = []
        self._comment_frames: list[tk.Frame] = []
        self._rewrap_id: 'str | None' = None
        # Rebuilt every render from the review store + working-tree files.
        self._pending_anchors: dict[str, list['_ResolvedAnchor']] = {}
        self._line_to_anchor: dict[int, '_ResolvedAnchor'] = {}
        # (file, new_line_no, side, line_text) for every rendered diff line —
        # used when opening the editor on a not-yet-commented line.
        self._line_post_image: dict[int, tuple[str, int, str, str]] = {}
        # Avoid re-reading and re-hashing a file for each new comment in a burst.
        self._session_snapshots: dict[str, str] = {}
        self._repo_root = _find_repo_root()
        self._scroll_remaining: float = 0.0  # pixels the smooth scroll still has to cover
        self._scroll_animating: bool = False
        self._scroll_moved: bool = False       # this animation changed the view
        self._settle_after_id: 'str | None' = None
        self._flist_selected_row: int = -1
        self._last_top: str = ''  # top index at the last scroll callback
        self._flist_row_to_entry: list[FileEntry | None] = []
        self._flist_path_to_row: dict[str, int] = {}
        self._manual_scroll: bool = False
        self._review = ReviewStore()
        self._commits = commits or []
        self._has_staged = has_staged
        self._has_unstaged = has_unstaged
        self._source = source
        self._can_reload = bool(source and source.can_reload())
        self._clist_actions: list = []
        self._active_comment_frame: tk.Frame | None = None
        self._active_comment_entry: tk.Text | None = None
        self._comment_target: '_CommentEditTarget | None' = None
        self._hover_line: int = -1
        self._hover_range: tuple[str, str] | None = None      # whole line ('hover_line' tag)
        self._hover_row: str | None = None                    # cursor display-row start (cache key)
        self._hover_row_range: tuple[str, str] | None = None  # cursor row ('hover' tag)
        self._hover_btn_line: int = -1
        self._hide_after_id: str | None = None
        self._over_hover_btn: bool = False
        self._btn_leave_after_id: str | None = None
        self._has_focus: bool = True
        cfg = self._load_config()
        self._wrap_var = tk.BooleanVar(value=cfg.get('wrap_lines', True))
        self._tree_var = tk.BooleanVar(value=cfg.get('tree_view', False))
        _ln_default = 1 if cfg.get('line_numbers', True) else 0  # migrate old bool config
        self._lineno_var = tk.IntVar(value=cfg.get('line_numbers_mode', _ln_default))  # 0 off, 1 new, 2 old/new
        _wd_default = 2 if cfg.get('word_diff', True) else 0  # migrate old bool config
        self._word_diff_var = tk.IntVar(value=cfg.get('word_diff_mode', _wd_default))
        # Per rendered-Text-line (old_line_no, new_line_no) for the gutter; only
        # content lines have an entry, so .get() returns None for headers/blanks.
        self._gutter_nums: dict[int, tuple[Optional[int], Optional[int]]] = {}
        self._gutter_max: tuple[int, int] = (0, 0)  # (old, new); avoids an O(n) scan per scroll
        self._scale = _detect_scale(root)
        # Single source of truth for the hover-ruler colour: the 'hover' tag and
        # the copy/comment buttons that sit on the ruler row must match.
        self._ruler_bg = _mix(C['topbar_bg'], C['subdued'], 0.45)

        self._build_ui()
        # Parsing and rendering a large diff take a while; do them once the
        # window is up so the progress label is visible meanwhile.
        self._progress.bind('<Map>', self._on_first_map, add='+')

    def _on_first_map(self, _event: tk.Event) -> None:
        self._progress.unbind('<Map>')
        self.root.after_idle(self._load)

    # --UI ------------------------------------------------------------

    def _make_read_only(self, widget: tk.Text) -> None:
        # Ctrl+W conflicts: Text class binds it to "delete previous word".
        # Overriding it here is unavoidable; extract to one place so each
        # read-only widget needs only a single call.
        widget.bind('<Key>', lambda e: 'break')
        widget.bind('<Control-c>', lambda e: None)
        widget.bind('<Control-w>', lambda e: self._close_app())
        widget.bind('<Control-q>', lambda e: self._close_app())

    def _make_scrollbar(self, parent: tk.Widget, **kw) -> tk.Scrollbar:
        return tk.Scrollbar(parent,
                            bg=C['selected_bg'],
                            troughcolor=C['bg'],
                            activebackground=C['subdued'],
                            relief='flat', bd=0,
                            width=int(CFG.scrollbar_w * self._scale),
                            **kw)

    def _build_ui(self) -> None:
        menu_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        menu_kw = dict(bg=C['topbar_bg'], fg=C['fg'],
                       activebackground=C['selected_bg'], activeforeground=C['fg'],
                       relief='flat', bd=0, font=menu_font)
        menubar = tk.Menu(self.root, **menu_kw)
        file_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        if self._can_reload:
            file_menu.add_command(label='Reload', accelerator='F5',
                                  command=self._reload)
            file_menu.add_separator()
        file_menu.add_command(label='Quit', accelerator='Ctrl+Q',
                              command=self._close_app)
        menubar.add_cascade(label='File', menu=file_menu)
        view_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        view_menu.add_checkbutton(label='Wrap long lines', variable=self._wrap_var,
                                  command=self._on_wrap_toggle)
        view_menu.add_checkbutton(label='Tree view', variable=self._tree_var,
                                  command=self._on_tree_toggle)
        lineno_view_menu = tk.Menu(view_menu, tearoff=0, **menu_kw)
        for _val, _label in ((0, 'Off'), (1, 'New'), (2, 'Old + new')):
            lineno_view_menu.add_radiobutton(label=_label, value=_val,
                                             variable=self._lineno_var,
                                             command=self._on_lineno_toggle)
        view_menu.add_cascade(label='Line numbers', menu=lineno_view_menu, accelerator='l')
        word_diff_menu = tk.Menu(view_menu, tearoff=0, **menu_kw)
        word_diff_menu.add_radiobutton(label='Off',                      value=0,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        word_diff_menu.add_radiobutton(label='On',                       value=1,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        word_diff_menu.add_radiobutton(label='On + collapse re-indented', value=2,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        view_menu.add_cascade(label='Word diff', menu=word_diff_menu, accelerator='d')
        menubar.add_cascade(label='View', menu=view_menu)
        go_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        go_menu.add_command(label='Next file',     accelerator='n / Tab',
                            command=lambda: self._jump_to_adjacent_file(1))
        go_menu.add_command(label='Previous file', accelerator='p / Shift+Tab',
                            command=lambda: self._jump_to_adjacent_file(-1))
        menubar.add_cascade(label='Go', menu=go_menu)
        self._review_menu = tk.Menu(menubar, tearoff=0,
                                    postcommand=self._rebuild_review_menu, **menu_kw)
        menubar.add_cascade(label='Review', menu=self._review_menu)
        self.root.configure(bg=C['bg'], menu=menubar)
        self._sash_ratio = CFG.sash_ratio
        self._pending_scroll_frac: 'float | None' = None
        saved_state = _load_window_state()
        if saved_state and isinstance(saved_state.get('geometry'), str):
            self.root.geometry(saved_state['geometry'])
        else:
            sw, sh = _primary_monitor_size()
            w, h = int(sw * CFG.window_scale), int(sh * CFG.window_scale)
            self.root.geometry(f'{w}x{h}')
        if saved_state and isinstance(saved_state.get('sash_ratio'), (int, float)):
            r = float(saved_state['sash_ratio'])
            if 0.05 < r < 0.95:
                self._sash_ratio = r
        if saved_state and isinstance(saved_state.get('top_line'), int):
            self._pending_scroll_line = max(1, saved_state['top_line'])
        elif saved_state and isinstance(saved_state.get('scroll_frac'), (int, float)):
            # Legacy state from before top_line was stored; applied once the
            # document is complete since a fraction means nothing before that.
            f = float(saved_state['scroll_frac'])
            if 0.0 <= f <= 1.0:
                self._pending_scroll_frac = f
        font = (CFG.font_family, CFG.font_size)
        bar_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))

        # top bar
        bar = tk.Frame(self.root, bg=C['topbar_bg'], pady=5)
        bar.pack(fill='x')

        self._lbl_branch = tk.Label(bar, bg=C['topbar_bg'], fg=C['fg'], font=font)
        self._lbl_branch.pack(side='left', padx=10)

        self._lbl_stat = tk.Label(bar, bg=C['topbar_bg'], fg=C['subdued'], font=font)
        self._lbl_stat.pack(side='left')

        if self._can_reload:
            self._reload_btn = tk.Button(
                bar, text='reload',
                bg=C['topbar_bg'], fg=C['fg'],
                activebackground=C['selected_bg'], activeforeground=C['fg'],
                relief='groove', bd=1, highlightthickness=0,
                font=bar_font, padx=8, pady=0, cursor='hand2',
                command=self._reload)
            self._reload_btn.pack(side='right', padx=10)


        # two-panel split
        self._sash = tk.PanedWindow(self.root, orient='horizontal',
                                     bg=C['subdued'], sashwidth=3, sashrelief='flat')
        self._sash.pack(fill='both', expand=True)

        # left: diff (grid so the scrollbar corner square fits neatly)
        lf = tk.Frame(self._sash, bg=C['bg'])
        lf.grid_rowconfigure(2, weight=1)
        lf.grid_columnconfigure(1, weight=1)  # diff text column expands; col 0 is the gutter

        bar_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        diff_bar = tk.Frame(lf, bg=C['topbar_bg'])
        diff_bar.grid(row=0, column=0, columnspan=5, sticky='ew')
        menu_kw_bar = dict(bg=C['topbar_bg'], fg=C['fg'],
                           activebackground=C['selected_bg'], activeforeground=C['fg'],
                           relief='flat', bd=0, font=bar_font, tearoff=0)
        self._wd_btn = tk.Menubutton(diff_bar, bg=C['topbar_bg'], fg=C['fg'],
                                      activebackground=C['selected_bg'], activeforeground=C['fg'],
                                      relief='groove', bd=1, highlightthickness=0,
                                      font=bar_font, padx=8, pady=2)
        wd_menu = tk.Menu(self._wd_btn, **menu_kw_bar)
        self._wd_btn['menu'] = wd_menu
        for i, name in enumerate(('plain', 'word', 'word+~')):
            wd_menu.add_command(label=name, command=lambda v=i: self._set_word_diff_mode(v))
        self._wd_btn.pack(side='left')
        self._update_wd_bar()

        self._wrap_btn = tk.Menubutton(diff_bar, bg=C['topbar_bg'], fg=C['fg'],
                                        activebackground=C['selected_bg'], activeforeground=C['fg'],
                                        relief='groove', bd=1, highlightthickness=0,
                                        font=bar_font, padx=8, pady=2)
        wrap_menu = tk.Menu(self._wrap_btn, **menu_kw_bar)
        self._wrap_btn['menu'] = wrap_menu
        for name in ('wrap', 'no wrap'):
            wrap_menu.add_command(label=name,
                                  command=lambda v=(name == 'wrap'): self._set_wrap_mode(v))
        self._wrap_btn.pack(side='left', padx=(4, 0))
        self._update_wrap_bar()

        self._lineno_btn = tk.Menubutton(diff_bar, bg=C['topbar_bg'], fg=C['fg'],
                                          activebackground=C['selected_bg'], activeforeground=C['fg'],
                                          relief='groove', bd=1, highlightthickness=0,
                                          font=bar_font, padx=8, pady=2)
        lineno_menu = tk.Menu(self._lineno_btn, **menu_kw_bar)
        self._lineno_btn['menu'] = lineno_menu
        for val, label in ((0, 'off'), (1, 'new'), (2, 'old/new')):
            lineno_menu.add_command(label=label,
                                    command=lambda v=val: self._set_lineno_mode(v))
        self._lineno_btn.pack(side='left', padx=(4, 0))
        self._update_lineno_bar()

        self._sticky = tk.Label(lf, bg=C['topbar_bg'], fg=C['fg'],
                                 font=font, anchor='w', padx=10, pady=3, text='')
        self._sticky.grid(row=1, column=0, columnspan=5, sticky='ew')

        self._diff = tk.Text(lf, bg=C['bg'], fg=C['fg'],
                              font=font, wrap='char',
                              relief='flat', bd=0, cursor='arrow',
                              selectbackground=C['selected_bg'],
                              selectforeground=C['fg'],
                              inactiveselectbackground=C['selected_bg'],
                              insertwidth=0)
        self._make_read_only(self._diff)
        self._diff.bind('<Configure>', self._on_diff_configure)
        self._bind_wheel(self._diff)
        self._diff.bind('<Up>',    lambda e: self._on_wheel(-1) or 'break')
        self._diff.bind('<Down>',  lambda e: self._on_wheel( 1) or 'break')
        self._diff.bind('<Prior>', lambda e: self._on_page_scroll(-1) or 'break')
        self._diff.bind('<Next>',  lambda e: self._on_page_scroll( 1) or 'break')
        self._diff.bind('<Home>',  lambda e: self._scroll_to(0.0) or 'break')
        self._diff.bind('<End>',   lambda e: self._scroll_to(1.0) or 'break')
        self._diff.bind('n',              lambda e: self._jump_to_adjacent_file( 1) or 'break')
        self._diff.bind('p',              lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('d',              lambda e: self._toggle_word_diff() or 'break')
        self._diff.bind('t',              lambda e: self._toggle_tree() or 'break')
        self._diff.bind('w',              lambda e: self._toggle_wrap() or 'break')
        self._diff.bind('l',              lambda e: self._toggle_lineno() or 'break')
        self._diff.bind('c',              lambda e: self._copy_loc_and_lines() or 'break')
        self._diff.bind('a',              lambda e: self._add_comment_at_cursor() or 'break')
        self._diff.bind('r',          lambda e: self._reload() or 'break')
        self._diff.bind('<F5>',       lambda e: self._reload() or 'break')
        self._diff.bind('<Control-r>', lambda e: self._reload() or 'break')
        self._diff.bind('<Tab>',          lambda e: self._jump_to_adjacent_file( 1) or 'break')
        self._diff.bind('<Shift-Tab>',      lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('<ISO_Left_Tab>',   lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('<ButtonRelease-3>', self._show_diff_context_menu)
        self._diff.bind('<Motion>', self._on_diff_hover)
        self._diff.bind('<Leave>',  lambda e: self._schedule_hide())
        self.root.bind('<FocusOut>', self._on_root_focus_out, add='+')
        self.root.bind('<FocusIn>',  self._on_root_focus_in,  add='+')
        self._comment_hover_btn = self._make_hover_button('+comment(a)', C['comment_fg'], self._on_comment_btn_click)
        self._copy_hover_btn    = self._make_hover_button('copy(c)',      C['fg'],          self._on_copy_btn_click)
        self._diff_vs = self._make_scrollbar(lf, orient='vertical', command=self._on_scrollbar_move)
        self._diff_vs.bind('<ButtonPress-1>', lambda e: setattr(self, '_manual_scroll', True))
        hs = self._make_scrollbar(lf, orient='horizontal', command=self._diff.xview)
        self._diff.configure(yscrollcommand=self._on_diff_yscroll, xscrollcommand=hs.set)

        # Line-number gutter: a Canvas drawn to the left of the diff and synced
        # to it via dlineinfo on scroll/resize (same approach as the minimap).
        # Kept out of the diff text so copy/selection and source-location stay
        # clean. Lives in column 0; the diff text expands in column 1.
        self._gutter_font = tkfont.Font(root=self.root, family=CFG.font_family, size=CFG.font_size)
        self._gutter = tk.Canvas(lf, bg=C['bg'], highlightthickness=0, width=1)
        self._gutter.grid(row=2, column=0, sticky='ns')
        self._gutter.bind('<Configure>', lambda e: self._render_gutter())
        self._bind_wheel(self._gutter)
        if not self._lineno_var.get():
            self._gutter.grid_remove()
        self._diff.grid(row=2, column=1, sticky='nsew')
        # Shown in the diff's place while it is being (re)built; keeping the
        # diff unmapped meanwhile also rules out painting it half-built.
        self._progress = tk.Label(lf, text='loading...', bg=C['bg'], fg=C['subdued'],
                                  font=font, anchor='nw', justify='left', padx=12, pady=8)
        # Gridded directly rather than via _show_progress: that runs idle
        # tasks, and the scroll callback they fire needs widgets built later.
        self._diff.grid_remove()
        self._progress.grid(row=2, column=1, sticky='nsew')
        self._progress_shown = True

        self._minimap = tk.Canvas(lf, width=int(CFG.minimap_w * self._scale),
                                  bg=C['bg'], highlightthickness=0)
        self._minimap.grid(row=2, column=2, rowspan=2, sticky='ns')
        self._minimap.bind('<Configure>',  lambda e: self._render_minimap())
        self._bind_wheel(self._minimap)
        self._bind_wheel(self._sticky)
        self._minimap.bind('<Button-1>',   self._on_minimap_press)
        self._minimap.bind('<B1-Motion>',  self._on_minimap_drag)

        self._diff_vs.grid(row=2, column=3, sticky='ns')
        # Overview strip: the whole document mapped onto the strip's height,
        # with a mark per comment (a Tk scrollbar cannot carry markers).
        self._overview = tk.Canvas(lf, width=int(CFG.overview_w * self._scale),
                                   bg=C['bg'], highlightthickness=0, cursor='hand2')
        self._overview.grid(row=2, column=4, sticky='ns')
        self._overview_refresh_id: 'str | None' = None
        self._overview_total = -1  # document pixel height the strip was drawn for
        self._overview_drawn: list[tuple[int, int]] = []  # (y, source line) as drawn
        # Deferred: the strip's <Configure> arrives before the scrollbar has
        # its new height, and a resize drag sends a storm of them.
        self._overview.bind('<Configure>', lambda e: self._schedule_overview_refresh())
        self._diff_vs.bind('<Configure>', lambda e: self._schedule_overview_refresh(), add='+')
        self._overview.bind('<Button-1>', self._on_overview_click)
        self._bind_wheel(self._overview)
        hs.grid(row=3, column=1, sticky='ew')
        _sw = int(CFG.scrollbar_w * self._scale)
        corner = tk.Frame(lf, bg=C['topbar_bg'], width=_sw, height=_sw)
        corner.grid(row=3, column=3, columnspan=2, sticky='ew')
        self._diff_hs = hs
        self._diff_hs_corner = corner
        # wrap on by default — horizontal scrollbar not needed
        hs.grid_remove()
        corner.grid_remove()

        # right: optional collapsible Comments + Commits panels above the file list
        rf = tk.Frame(self._sash, bg=C['bg'])

        def _make_section_toggle(parent: tk.Frame, command) -> tk.Button:
            return tk.Button(
                parent, text='',
                bg=C['topbar_bg'], fg=C['fg'],
                activebackground=C['topbar_bg'], activeforeground=C['fg'],
                relief='raised', bd=2, highlightthickness=0, cursor='hand2',
                font=bar_font, padx=8, pady=4, anchor='w',
                command=command)

        def _make_list_text(parent: tk.Frame) -> tuple[tk.Frame, tk.Text]:
            pane = tk.Frame(parent, bg=C['bg'])
            txt = tk.Text(pane, bg=C['bg'], fg=C['fg'],
                          font=font, wrap='none', height=1,
                          relief='flat', bd=0, state='disabled', cursor='arrow',
                          selectbackground=C['bg'], selectforeground=C['fg'],
                          inactiveselectbackground=C['bg'])
            sb = self._make_scrollbar(pane, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            txt.pack(fill='both', expand=True)
            return pane, txt

        # Comments section — created always; visibility/content updated per render.
        self._comments_expanded = False
        self._comments_header = tk.Frame(rf, bg=C['topbar_bg'])
        self._comments_toggle = _make_section_toggle(self._comments_header, self._toggle_comments_pane)
        self._comments_toggle.pack(fill='x')
        self._comments_pane, self._cmt_list = _make_list_text(rf)
        # Comments can wrap so long/multi-line comments stay readable. The
        # commits list keeps the default wrap='none' from _make_list_text.
        self._cmt_list.configure(wrap='word')

        # Commits section — created always; visibility/content updated per render
        # (mirrors how the Comments section is built).
        self._commits_expanded = False
        self._commits_header = tk.Frame(rf, bg=C['topbar_bg'])
        self._commits_toggle = _make_section_toggle(self._commits_header, self._toggle_commits_pane)
        self._commits_toggle.pack(fill='x')
        self._commits_pane, self._clist = _make_list_text(rf)

        flist_bar = tk.Frame(rf, bg=C['topbar_bg'])
        self._flist_btn = tk.Menubutton(flist_bar, bg=C['topbar_bg'], fg=C['fg'],
                                         activebackground=C['selected_bg'], activeforeground=C['fg'],
                                         relief='groove', bd=1, highlightthickness=0,
                                         font=bar_font, padx=8, pady=2)
        flist_menu = tk.Menu(self._flist_btn, **menu_kw_bar)
        self._flist_btn['menu'] = flist_menu
        for name in ('list', 'tree'):
            flist_menu.add_command(label=name,
                                   command=lambda v=(name == 'tree'): self._set_tree_mode(v))
        self._flist_btn.pack(side='left')
        self._update_flist_bar()

        self._files_pane = tk.Frame(rf, bg=C['bg'])
        self._flist = tk.Text(self._files_pane, bg=C['bg'], fg=C['fg'],
                               font=font, wrap='none',
                               relief='flat', bd=0, state='disabled', cursor='arrow',
                               selectbackground=C['bg'], selectforeground=C['fg'],
                               inactiveselectbackground=C['bg'])
        fvs = self._make_scrollbar(self._files_pane, orient='vertical', command=self._flist.yview)
        self._flist.configure(yscrollcommand=fvs.set)
        fvs.pack(side='right', fill='y')
        self._flist.pack(fill='both', expand=True)
        self._flist_bar = flist_bar
        # Pack the persistent rows in final top-to-bottom order. The
        # comments/commits headers and panes get pack()ed in via _update_*
        # / toggle methods using before=self._flist_bar (or _commits_header).
        flist_bar.pack(fill='x')
        self._files_pane.pack(fill='both', expand=True)

        self._sash.add(lf, stretch='always')
        self._sash.add(rf, stretch='never')
        self._sash.paneconfigure(lf, minsize=CFG.pane_min_w)
        self._sash.paneconfigure(rf, minsize=CFG.pane_min_w)
        self.root.after(50, self._init_sash)

        # diff tags — line highlight is a little more visible than the raw added_bg/removed_bg
        _rem_hi = _blend(C['removed_fg'], CFG.diff_hi_blend)
        _add_hi = _blend(C['added_fg'],   CFG.diff_hi_blend)
        self._diff.tag_configure('added',      foreground=C['added_fg'],   background=_add_hi)
        self._diff.tag_configure('removed',    foreground=C['removed_fg'], background=_rem_hi)
        self._diff.tag_configure('hunk',        foreground=C['hunk_fg'])
        self._diff.tag_configure('fileheader',  foreground=C['fileheader_fg'])
        self._diff.tag_configure('context',     foreground=C['fg'])
        self._diff.tag_configure('subdued',     foreground=C['subdued'])
        self._diff.tag_configure('filehdr',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('fileidx',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('status_A',    foreground=C['status_A'])
        self._diff.tag_configure('status_M',    foreground=C['status_M'])
        self._diff.tag_configure('status_D',    foreground=C['status_D'])
        self._diff.tag_configure('status_R',    foreground=C['status_R'])
        self._diff.tag_configure('hover',       background=self._ruler_bg)
        self._diff.tag_configure('hover_line',  background=_blend(C['topbar_bg'], 0.72))
        self._diff.tag_configure('repaint',     background=C['bg'])
        # Tk paints only the top tag's background, so the ruler would vanish
        # under 'sel' (or hide it). A blend on the overlap keeps both visible.
        self._diff.tag_configure('hover_sel',   background=_mix(C['selected_bg'], self._ruler_bg, 0.5))

        # file list tags
        self._flist.tag_configure('status_A',  foreground=C['status_A'])
        self._flist.tag_configure('status_M',  foreground=C['status_M'])
        self._flist.tag_configure('status_D',  foreground=C['status_D'])
        self._flist.tag_configure('status_R',  foreground=C['status_R'])
        self._flist.tag_configure('stats',     foreground=C['subdued'])
        self._flist.tag_configure('dir',       foreground=C['subdued'])
        self._flist.tag_configure('selected',  background=C['selected_bg'])

        # Word diff: unchanged words — colored text, barely-there bg so they recede
        self._diff.tag_configure('removed_word', foreground=_blend(C['removed_fg'], CFG.diff_dim_fg), background=_blend(C['removed_fg'], CFG.diff_dim_blend))
        self._diff.tag_configure('added_word',   foreground=_blend(C['added_fg'],   CFG.diff_dim_fg), background=_blend(C['added_fg'],   CFG.diff_dim_blend))
        self._diff.tag_configure('reindent',     foreground=C['subdued'])
        self._diff.tag_configure('orphan_src',   foreground=C['subdued'], background=C['topbar_bg'])
        _comment_bg = _blend(C['comment_fg'], 0.55)
        self._comment_bg = _comment_bg
        self._diff.tag_configure('comment', foreground=C['bg'], background=_comment_bg,
                                 spacing1=6, spacing3=6)
        self._comment_spacing1 = 6  # pixels between a comment line's top and its frame
        self._diff.tag_bind('comment', '<Button-1>', self._on_comment_click)
        self._diff.tag_bind('comment', '<Enter>', lambda e: self._diff.config(cursor='hand2'))
        self._diff.tag_bind('comment', '<Leave>', lambda e: self._diff.config(cursor=''))
        # Word diff: changed words — same "change highlight" bg as the full-line removed/added tags
        self._diff.tag_configure('removed_hi',   foreground=C['removed_fg'], background=_rem_hi)
        self._diff.tag_configure('added_hi',     foreground=C['added_fg'],   background=_add_hi)
        self._diff.tag_lower('repaint')
        self._diff.tag_raise('hover_line')
        self._diff.tag_raise('hover')
        self._diff.tag_raise('sel')
        self._diff.tag_raise('hover_sel')
        self._diff.bind('<<Selection>>', lambda e: self._sync_hover_sel(), add='+')

        self._flist.bind('<Button-1>', self._on_file_click)
        self._flist.bind('<B1-Motion>', lambda e: 'break')
        self._flist.bind('<Double-Button-1>', lambda e: 'break')
        self._flist.bind('<Triple-Button-1>', lambda e: 'break')
        self._flist.bind('<Button-4>',   lambda e: self._flist.yview_scroll(-4, 'units') or 'break')
        self._flist.bind('<Button-5>',   lambda e: self._flist.yview_scroll( 4, 'units') or 'break')
        self._flist.bind('<MouseWheel>', lambda e: self._flist.yview_scroll(4 * _wheel_ticks(e.delta), 'units') or 'break')
        self._flist.bind('<Up>',         lambda e: self._flist_nav(-1) or 'break')
        self._flist.bind('<Down>',       lambda e: self._flist_nav( 1) or 'break')
        self._flist.bind('<Return>',     lambda e: self._flist_activate() or 'break')
        self._flist.bind('d',            lambda e: self._toggle_word_diff() or 'break')
        self._flist.bind('t',            lambda e: self._toggle_tree() or 'break')
        self._flist.bind('w',            lambda e: self._toggle_wrap() or 'break')
        self._flist.bind('l',            lambda e: self._toggle_lineno() or 'break')
        self._flist.bind('c',            lambda e: self._copy_loc_and_lines() or 'break')
        # The bare 'w' binding above shadows Ctrl+W on this widget: Tk fires the
        # most specific per-widget binding, so without an explicit Ctrl+W here
        # the close combo is swallowed by "toggle wrap". Re-assert it (and Q for
        # symmetry), mirroring _make_read_only on the diff pane.
        self._flist.bind('<Control-w>', lambda e: self._close_app())
        self._flist.bind('<Control-q>', lambda e: self._close_app())
        self._on_wrap_toggle()

    def _update_wrap_bar(self) -> None:
        name = 'wrap' if self._wrap_var.get() else 'no wrap'
        self._wrap_btn.configure(text=f'Wrap (w): {name}')

    def _set_wrap_mode(self, wrap: bool) -> None:
        self._wrap_var.set(wrap)
        self._on_wrap_toggle()

    def _toggle_wrap(self) -> None:
        self._wrap_var.set(not self._wrap_var.get())
        self._on_wrap_toggle()

    def _on_wrap_toggle(self) -> None:
        wrap = self._wrap_var.get()
        if wrap:
            self._diff.configure(wrap='char')
            self._diff_hs.grid_remove()
            self._diff_hs_corner.grid_remove()
        else:
            self._diff.configure(wrap='none')
            self._diff_hs.grid()
            self._diff_hs_corner.grid()
        self._update_wrap_bar()
        self._save_config({'wrap_lines': wrap})
        self.root.after_idle(self._render_gutter)
        self.root.after_idle(self._mm_relayout)
        self._schedule_overview_refresh()

    def _update_lineno_bar(self) -> None:
        name = ('off', 'new', 'old/new')[self._lineno_var.get()]
        self._lineno_btn.configure(text=f'Lines (l): {name}')

    def _set_lineno_mode(self, mode: int) -> None:
        self._lineno_var.set(mode)
        self._on_lineno_toggle()

    def _toggle_lineno(self) -> None:
        self._lineno_var.set((self._lineno_var.get() + 1) % 3)
        self._on_lineno_toggle()

    def _on_lineno_toggle(self) -> None:
        mode = self._lineno_var.get()
        if mode:
            self._gutter.grid()
        else:
            self._gutter.grid_remove()
        self._update_lineno_bar()
        self._save_config({'line_numbers_mode': mode})
        self._render_gutter()

    def _update_flist_bar(self) -> None:
        name = 'tree' if self._tree_var.get() else 'list'
        self._flist_btn.configure(text=f'Files (t): {name}')

    def _set_tree_mode(self, tree: bool) -> None:
        self._tree_var.set(tree)
        self._on_tree_toggle()

    def _toggle_tree(self) -> None:
        self._tree_var.set(not self._tree_var.get())
        self._on_tree_toggle()

    def _on_tree_toggle(self) -> None:
        self._update_flist_bar()
        self._save_config({'tree_view': self._tree_var.get()})
        self._render_flist(self._entries)

    def _update_wd_bar(self) -> None:
        name = ('plain', 'word', 'word+~')[self._word_diff_var.get()]
        self._wd_btn.configure(text=f'Diff (d): {name}')

    def _set_word_diff_mode(self, mode: int) -> None:
        self._word_diff_var.set(mode)
        self._on_word_diff_toggle()

    def _toggle_word_diff(self) -> None:
        self._word_diff_var.set((self._word_diff_var.get() + 1) % 3)
        self._on_word_diff_toggle()

    def _on_word_diff_toggle(self) -> None:
        self._update_wd_bar()
        self._save_config({'word_diff_mode': self._word_diff_var.get()})
        self._rerender_preserving_scroll()

    @staticmethod
    def _load_config() -> dict:
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except Exception:
            return {}

    @staticmethod
    def _save_config(data: dict) -> None:
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            existing = App._load_config()
            existing.update(data)
            _CONFIG_PATH.write_text(json.dumps(existing))
        except Exception:
            pass

    def _clamped_sash_x(self, w: int) -> int:
        # Cap the sash position so neither pane drops below pane_min_w, even if
        # a saved ratio + a narrow window would otherwise collapse one side.
        x = int(w * self._sash_ratio)
        return max(CFG.pane_min_w, min(w - CFG.pane_min_w, x))

    def _init_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, self._clamped_sash_x(w), 0)
            self.root.bind('<Configure>', self._on_window_configure)
            self._sash.bind('<ButtonRelease-1>', self._on_sash_release, add='+')
        else:
            self.root.after(50, self._init_sash)

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self.root.after_idle(self._place_sash)

    def _place_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, self._clamped_sash_x(w), 0)

    def _on_sash_release(self, _event: tk.Event) -> None:
        w = self._sash.winfo_width()
        if w <= 1:
            return
        try:
            x = self._sash.sash_coord(0)[0]
        except (tk.TclError, IndexError):
            return
        self._sash_ratio = max(0.05, min(0.95, x / w))

    # --smooth scroll ---------------------------------------------------------
    #
    # Everything scrolls by pixels relative to the current view, never by
    # document fraction: Tk's fractions are pixel estimates that keep changing
    # for a second after a wrap toggle (it recomputes every line's height),
    # so a fraction target would land hundreds of lines away.

    def _line_px(self) -> int:
        return max(1, self._gutter_font.metrics('linespace'))

    def _scroll_by_pixels(self, px: float) -> None:
        self._manual_scroll = True
        if self._scroll_edge:
            # A wheel tick during a Home/End run means "stop here": drop the
            # rest of the run and apply the tick from the current position.
            self._scroll_edge = 0
            self._scroll_remaining = 0.0
        self._scroll_remaining += px
        if not self._scroll_animating:
            self._scroll_animating = True
            self._scroll_moved = False
            self._animate_scroll()

    def _stop_scroll_animation(self) -> None:
        self._scroll_remaining = 0.0
        was_animating = self._scroll_animating
        self._scroll_animating = False
        self._scroll_edge = 0
        if self._scroll_after_id is not None:
            self.root.after_cancel(self._scroll_after_id)
            self._scroll_after_id = None
        if was_animating and self._scroll_moved:
            self._schedule_settle()

    def _hide_for_scroll(self) -> None:
        """Hide the ruler before the view moves (see _after_scroll_settled)."""
        if self._hover_line >= 0 or self._hover_btn_line >= 0:
            self._do_hide_hover(force=True)

    def _move_view(self, *yview_args: str, px: int = 0) -> None:
        """A non-animated move of the diff view: jump, scrollbar, minimap.

        `px` scrolls further by that many pixels after the yview call.
        """
        self._hide_for_scroll()
        rects = self._embedded_rects()
        self._diff.yview(*yview_args)
        if px:
            self._diff.yview_scroll(px, 'pixels')
        self._repaint_rects(rects)
        self._schedule_settle()

    def _embedded_rects(self) -> list[tuple[int, int]]:
        """(y0, y1) of every embedded window (comment frame, hunk separator,
        editor) currently on screen, in diff widget pixels."""
        d = self._diff
        rects: list[tuple[int, int]] = []
        for _key, _name, idx in d.dump('@0,0', f'@0,{d.winfo_height()} lineend', window=True):
            info = d.dlineinfo(idx)
            if not info or info[3] < 4:
                continue  # 1 px hunk separators leave no visible hole
            y0, y1 = info[1], info[1] + info[3]
            if rects and y0 <= rects[-1][1]:
                rects[-1] = (rects[-1][0], max(rects[-1][1], y1))  # dump lists in order
            elif not rects or (y0, y1) != rects[-1]:
                rects.append((y0, y1))
        return rects

    def _repaint_rects(self, rects: list[tuple[int, int]]) -> None:
        """Dirty the lines now under `rects`, taken before a pixel scroll.

        Tk copies pixels to scroll and then moves embedded windows; the area
        a window vacated is repainted only when X's Expose arrives, a frame
        later, which shows as a flicker band trailing every comment frame.
        Dirtied here, those lines are drawn in the same redisplay.
        """
        d = self._diff
        h = d.winfo_height()
        for y0, y1 in rects:
            if y1 <= 0 or y0 >= h:
                continue
            a = d.index(f'@0,{max(y0, 0)} linestart')
            b = d.index(f'@0,{min(y1, h) - 1} lineend')
            d.tag_add('repaint', a, b)
            d.tag_remove('repaint', a, b)

    def _schedule_settle(self) -> None:
        if self._settle_after_id is None:
            self._settle_after_id = self.root.after_idle(self._after_scroll_settled)

    def _after_scroll_settled(self) -> None:
        """Repair and re-hover after the view moved.

        Tk scrolls by copying pixels, and a child window that was mapped over
        the copied area leaves a hole that X repairs only when its (Graphics)
        Expose event arrives. A later copy can move that hole first, leaving
        garbage that nothing repaints. The ruler buttons are therefore hidden
        before every move (_hide_for_scroll) and not shown while an animation
        runs, but the window_create-embedded children (hunk separators,
        comment frames, the editor) stay mapped through every copy. Dirtying
        every visible row once the view is still repaints whatever was left;
        the tag sits below all others and matches the widget bg, so it is
        invisible.
        """
        self._settle_after_id = None
        if self._scroll_animating:
            return
        d = self._diff
        # Flush the redisplay that the move queued behind this callback, so
        # its yscroll callback cannot hide the ruler placed below.
        d.update_idletasks()
        top, bottom = d.index('@0,0 linestart'), d.index(f'@0,{d.winfo_height()} lineend')
        d.tag_add('repaint', top, bottom)
        d.tag_remove('repaint', top, bottom)
        # The ruler is suppressed while scrolling; bring it back for wherever
        # the pointer ended up without waiting for the next motion event.
        self._rehover_under_pointer()

    def _rehover_under_pointer(self, under: 'tk.Widget | None' = None) -> bool:
        """Re-run the hover handler for the pointer's current position.

        `under` is the widget already known to be under the pointer, if any.
        Returns False when the pointer is not over the diff text. This checks
        the containing widget rather than the diff's bounds on purpose: over
        a placed button or an embedded comment frame the two differ.
        """
        d = self._diff
        if under is None:
            under = self._widget_under_pointer()
        if under is not d:
            return False
        try:
            px, py = self.root.winfo_pointerxy()
            ev = tk.Event()
            ev.x, ev.y = px - d.winfo_rootx(), py - d.winfo_rooty()
        except tk.TclError:
            return False
        self._on_diff_hover(ev)
        return True

    def _bind_wheel(self, widget: tk.Widget) -> None:
        # Widgets layered over the diff (hover buttons, comment frames, the
        # sticky header) receive the wheel event themselves and would swallow
        # it, so each one has to forward it to the diff explicitly.
        widget.bind('<Button-4>',   lambda e: self._on_wheel(-1) or 'break')
        widget.bind('<Button-5>',   lambda e: self._on_wheel( 1) or 'break')
        widget.bind('<MouseWheel>', lambda e: self._on_wheel(_wheel_ticks(e.delta)) or 'break')

    def _on_wheel(self, ticks: int) -> None:
        self._scroll_by_pixels(CFG.scroll_speed * ticks * self._line_px())

    def _edge_distance(self, edge: int) -> float:
        """Pixels from the current view to the top (-1) or bottom (+1).

        Tk's pixel counts are estimates until every line height is known, so
        a run re-measures when it runs out and keeps going to the real edge.
        """
        d = self._diff
        above = self._ypixels('1.0', '@0,0')
        if edge < 0:
            return -above
        total = self._ypixels('1.0', 'end')
        return max(0, total - d.winfo_height()) - above

    def _ypixels(self, a: str, b: str) -> int:
        """Pixel height between two indices.

        Text.count returns None for zero (until 3.13), and an int or a
        one-tuple depending on the Python version.
        """
        res = self._diff.count(a, b, 'ypixels')
        if res is None:
            return 0
        return int(res[0]) if isinstance(res, tuple) else int(res)

    def _at_edge(self, edge: int) -> bool:
        first, last = self._diff.yview()
        return last >= 1.0 if edge > 0 else first <= 0.0

    def _scroll_to(self, pos: float) -> None:
        """Animate to the top (pos < 1) or the bottom of the document."""
        edge = 1 if pos >= 1.0 else -1
        self._scroll_remaining = 0.0
        self._scroll_by_pixels(self._edge_distance(edge))
        self._scroll_edge = edge

    def _on_page_scroll(self, direction: int) -> None:
        self._scroll_by_pixels(direction * self._diff.winfo_height() * 2 / 3)

    def _animate_scroll(self) -> None:
        self._scroll_after_id = None
        remaining = self._scroll_remaining
        if abs(remaining) < 1 and self._scroll_edge and not self._at_edge(self._scroll_edge):
            remaining = self._scroll_remaining = self._edge_distance(self._scroll_edge)
        if abs(remaining) < 1:
            self._stop_scroll_animation()
            return
        step = remaining * 0.35
        step_i = int(step) if abs(step) >= 1 else (1 if step > 0 else -1)
        if self._at_edge(1 if step_i > 0 else -1):
            # Nothing can move, so leave the ruler alone: hiding it here would
            # make every tick at the top or bottom blink.
            self._stop_scroll_animation()
            return
        self._hide_for_scroll()  # before the copy, see _after_scroll_settled
        rects = self._embedded_rects()
        before = self._diff.yview()
        self._diff.yview_scroll(step_i, 'pixels')
        self._scroll_remaining = remaining - step_i
        if self._diff.yview() == before:
            self._stop_scroll_animation()  # at the top or bottom already
            return
        self._scroll_moved = True
        self._repaint_rects(rects)
        self._scroll_after_id = self.root.after(16, self._animate_scroll)

    # --hunk separators -------------------------------------------------------

    def _make_hover_button(self, text: str, fg: str, command) -> tk.Label:
        # Match the diff text's font so the label's natural height equals the
        # diff line height. Multiplying menu_font_size by self._scale (as menus
        # do) double-applies DPI scaling and overflows the ruler row.
        # tk.Label (rather than tk.Button) avoids the platform theme shadow.
        btn = tk.Label(
            self._diff, text=text,
            bg=self._ruler_bg, fg=fg,
            bd=0, highlightthickness=0, cursor='hand2',
            padx=4, pady=0,
            font=(CFG.font_family, CFG.font_size),
        )
        btn.bind('<Button-1>', lambda e: command())
        self._bind_wheel(btn)
        btn.bind('<Enter>', lambda e: self._on_btn_enter())
        btn.bind('<Leave>', lambda e: self._on_btn_leave())
        return btn

    def _diff_row_pad(self) -> int:
        cached = getattr(self, '_diff_row_pad_cached', None)
        if cached is not None:
            return cached
        try:
            padx = int(str(self._diff.cget('padx')))
            hl   = int(str(self._diff.cget('highlightthickness')))
        except (tk.TclError, ValueError):
            padx, hl = 1, 1
        self._diff_row_pad_cached = 2 * (padx + hl)
        return self._diff_row_pad_cached

    def _on_diff_configure(self, event: tk.Event) -> None:
        if event.width > 1:
            for sep in self._hunk_seps:
                sep.configure(width=event.width)
            row_w = max(event.width - self._diff_row_pad(), 1)
            for f in self._comment_frames:
                if f.winfo_exists():
                    f.configure(width=row_w)
            if self._active_comment_frame and self._active_comment_frame.winfo_exists():
                self._active_comment_frame.configure(width=row_w)
            self._render_gutter()  # width change reflows wrapped lines
            if self._wrap_var.get() and self._mm_wrap_cols() != self._mm_cols:
                self._schedule_mm_relayout()
            self._schedule_rewrap()

    def _update_hunk_sep_widths(self) -> None:
        w = self._diff.winfo_width()
        if w > 1:
            for sep in self._hunk_seps:
                sep.configure(width=w)
            row_w = max(w - self._diff_row_pad(), 1)
            for f in self._comment_frames:
                if f.winfo_exists():
                    f.configure(width=row_w)
            if self._active_comment_frame and self._active_comment_frame.winfo_exists():
                self._active_comment_frame.configure(width=row_w)
        else:
            self.root.after(50, self._update_hunk_sep_widths)

    # --minimap ---------------------------------------------------------------
    #
    # VS Code style: every line is _MM_LINE_H px tall (times the UI scale) with
    # one pixel column per character, never squashed to fit.  When the diff is
    # taller than the canvas the minimap scrolls along with the document, so
    # only the visible band is rasterised.  It is handed to Tk as a binary PPM,
    # which loads in a few ms where per-pixel colour names took ~20x longer.

    def _mm_wrap_cols(self) -> 'int | None':
        """Characters per display row when wrapping, else None."""
        if not self._wrap_var.get():
            return None
        w = self._diff.winfo_width() - self._diff_row_pad()
        charw = max(1, self._gutter_font.measure('0'))
        return w // charw if w >= charw else None

    @staticmethod
    def _mm_line_rows(kind: str, text: str, cols: 'int | None') -> int:
        """Minimap rows for one text line: a comment frame is as tall as its
        block (it never wraps); other lines wrap at `cols` characters."""
        if kind == 'comment':
            return text.count('\n') + 1
        if cols is None:
            return 1
        return max(1, -(-len(text.expandtabs(8)) // cols))

    def _mm_relayout(self) -> None:
        """Recompute rows per line for the current wrap width, then repaint."""
        if self._mm_relayout_id is not None:
            self.root.after_cancel(self._mm_relayout_id)  # a sync call supersedes it
            self._mm_relayout_id = None
        cols = self._mm_wrap_cols()
        self._mm_cols = cols
        lines = self._minimap_lines
        if cols is None:
            # Without wrapping only multi-line comments take more than one
            # row, so the table is skipped entirely when there are none.
            tall = [(i, text.count('\n')) for i, (kind, text) in enumerate(lines)
                    if kind == 'comment' and '\n' in text]
            if not tall:
                self._mm_row_start = None
            else:
                rows = [1] * len(lines)
                for i, extra in tall:
                    rows[i] += extra
                self._mm_row_start = [0, *accumulate(rows)]
        else:
            starts = [0] * (len(lines) + 1)
            r = 0
            for i, (kind, text) in enumerate(lines):
                starts[i] = r
                r += self._mm_line_rows(kind, text, cols)
            starts[-1] = r
            self._mm_row_start = starts
        self._mm_rows.clear()
        self._render_minimap()

    def _schedule_mm_relayout(self) -> None:
        # Coalesces the burst of <Configure> events from a resize drag.
        if self._mm_relayout_id is None:
            self._mm_relayout_id = self.root.after(100, self._mm_relayout)

    def _mm_starts(self) -> 'list[int] | None':
        """The row table, or None while rows and lines coincide (also when the
        table is stale: lines changed and the relayout has not run yet)."""
        starts = self._mm_row_start
        if starts is None or len(starts) != len(self._minimap_lines) + 1:
            return None
        return starts

    def _mm_nrows(self) -> int:
        starts = self._mm_starts()
        return starts[-1] if starts is not None else len(self._minimap_lines)

    def _mm_row_of(self, line: int, char: int) -> int:
        """Minimap row for text line `line` (1-based) at character `char`."""
        i = line - 1
        starts = self._mm_starts()
        if starts is None:
            return i
        if i >= len(self._minimap_lines):
            return starts[-1]  # Tk's implicit final line
        if self._mm_cols is None or not char:
            return starts[i]
        col = char
        if char:
            # Rows were counted on tab-expanded text; map the raw offset
            # into the same coordinate space.
            col = len(self._diff.get(f'{line}.0', f'{line}.{char}').expandtabs(8))
        return starts[i] + min(col // self._mm_cols, starts[i + 1] - starts[i] - 1)

    def _mm_char_at_col(self, line: int, col: int) -> int:
        """Raw character offset in text line `line` at display column `col`."""
        if col <= 0:
            return 0
        text = self._diff.get(f'{line}.0', f'{line}.end')
        c = 0
        for k, ch in enumerate(text):
            if c >= col:
                return k
            c = (c // 8 + 1) * 8 if ch == '\t' else c + 1
        return len(text)

    def _mm_line_of_row(self, row: int) -> tuple[int, int]:
        """(line index, segment) for a minimap row."""
        starts = self._mm_starts()
        if starts is None:
            return row, 0
        i = bisect.bisect_right(starts, row) - 1
        return i, row - starts[i]

    def _mm_layout(self) -> 'tuple[int, int, int, int, int, int] | None':
        """Return (canvas_h, img_w, char_w, line_h, content_h, n_rows), or None."""
        c = self._minimap
        cw, ch = c.winfo_width(), c.winfo_height()
        n = self._mm_nrows()
        if cw <= 1 or ch <= 1 or n == 0:
            return None
        chw = max(1, int(round(self._scale)))
        lh = _MM_LINE_H * chw
        return ch, (cw // chw) * chw, chw, lh, n * lh, n

    def _mm_offset_for(self, first: float, last: float, ch: int, content_h: int) -> int:
        """Minimap scroll offset: top of document -> 0, bottom -> content_h - ch."""
        max_first = 1.0 - (last - first)
        if content_h <= ch or max_first <= 0:
            return 0
        return max(0, min(content_h - ch, int(first / max_first * (content_h - ch))))

    def _mm_line_row(self, r: int, iw: int, chw: int) -> bytes:
        row = self._mm_rows.get(r)
        if row is not None:
            return row
        i, seg = self._mm_line_of_row(r)
        kind, text = self._minimap_lines[i]
        if kind == 'comment':
            # One block line per row, pre-wrapped; a stale row table could
            # ask for a row past a shortened block.
            block = text.split('\n')
            text = block[seg] if seg < len(block) else ''
        elif seg:
            text = text.expandtabs(8)[seg * (self._mm_cols or 1):]
        elif '\t' in text:
            text = text.expandtabs(8)
        cols = iw // chw
        tables = _MM_CHANNELS.get(kind)
        if tables is None:
            levels = bytes(cols)
            tables = _MM_CHANNELS['context']  # level 0 is the bg colour for every kind
        elif kind in _MM_SOLID_KINDS:
            levels = bytes([len(_MM_LEVELS) - 1]) * cols
        else:
            enc = text[:cols].encode('latin-1', 'replace')
            levels = enc.translate(_MM_DENSITY).ljust(cols, b'\x00')
        # Interleave the three channels (and repeat each pixel chw times)
        # with strided slice assignment so the whole row is built in C.
        px = bytearray(iw * 3)
        for k in range(3):
            chan = levels.translate(tables[k])
            for rep_ in range(chw):
                px[k + 3 * rep_::3 * chw] = chan
        row = bytes(px)
        self._mm_rows[r] = row
        return row

    def _mm_row_block(self, r: int, iw: int, chw: int, lh: int) -> bytes:
        """The `lh` pixel rows for minimap row `r`.

        Comment rows keep their bottom pixel row free of ink: prose fills a
        row edge to edge, and without a gap consecutive rows merge into one
        blob, unlike code rows, which are mostly indentation and spaces.
        """
        row = self._mm_line_row(r, iw, chw)
        if lh < 2 or self._minimap_lines[self._mm_line_of_row(r)[0]][0] != 'comment':
            return row * lh
        return row * (lh - 1) + self._mm_base_row('comment', iw)

    def _mm_base_row(self, kind: str, iw: int) -> bytes:
        """One pixel row of `kind`'s no-ink colour."""
        key = (kind, iw)
        row = self._mm_base_rows.get(key)
        if row is None:
            tables = _MM_CHANNELS[kind]
            row = bytes((tables[0][0], tables[1][0], tables[2][0])) * iw
            self._mm_base_rows[key] = row
        return row

    def _mm_paint_band(self, iw: int, chw: int, lh: int, offset: int, band_h: int) -> None:
        i0 = offset // lh
        i1 = min(self._mm_nrows(), (offset + band_h + lh - 1) // lh)
        stride = iw * 3
        start = (offset - i0 * lh) * stride
        data = b''.join(self._mm_row_block(i, iw, chw, lh) for i in range(i0, i1))
        data = data[start:start + band_h * stride]
        h = len(data) // stride
        if h <= 0:
            return
        ppm = b'P6 %d %d 255\n' % (iw, h) + data
        # Reconfiguring one image is ~3x cheaper than creating a new one per
        # scroll frame; the canvas item picks up the change on its own.
        if self._mm_img is None:
            self._mm_img = tk.PhotoImage(data=ppm)  # keep a reference or Tk drops it
            self._mm_item = self._minimap.create_image(0, 0, anchor='nw', image=self._mm_img)
        else:
            self._mm_img.configure(data=ppm)
        self._mm_painted = (offset, h)

    def _render_minimap(self) -> None:
        """Full redraw: geometry changed, so the band is stale."""
        self._mm_painted = None
        cw = self._minimap.winfo_width()
        if cw != self._mm_rows_w:
            self._mm_rows.clear()
            self._mm_rows_w = cw
        self._update_minimap_viewport()

    def _mm_view_rows(self) -> tuple[int, int]:
        """Minimap rows [top, bottom) currently visible in the diff widget.

        Derived from the visible line numbers rather than Tk's scroll
        fractions: those are pixel-based, and wrapped lines, comment frames
        and 1px separators make them drift from the line-based minimap.
        """
        n = self._mm_nrows()
        h = max(self._diff.winfo_height() - 1, 0)
        top = max(0, min(n, self._mm_row_at_y(0)))
        bottom = self._mm_row_at_y(h) + 1
        return top, max(top + 1, min(n, bottom))

    def _mm_row_at_y(self, y: int) -> int:
        """Minimap row shown at widget pixel `y`.

        A comment frame is one text line spanning several minimap rows, so
        the line index alone would pin the band to the block's first row
        while the view moves through it; the pixel offset into the frame
        picks the row instead.
        """
        d = self._diff
        idx = d.index(f'@0,{y}')
        l, c = (int(v) for v in idx.split('.'))
        row = self._mm_row_of(l, c)
        if l - 1 < len(self._minimap_lines) and self._minimap_lines[l - 1][0] == 'comment':
            starts = self._mm_starts()
            span = starts[l] - starts[l - 1] if starts is not None else 1
            info = d.dlineinfo(idx)
            full_h = self._comment_frame_height(l)
            if span > 1 and info and full_h > 0:
                # Rounded so a row half scrolled off counts as gone.
                into = int((y - info[1] - self._comment_spacing1) * span / full_h + 0.5)
                row += max(0, min(span - 1, into))
        return row

    def _comment_frame_height(self, line: int) -> int:
        """Full height of the frame embedded on text line `line`.

        dlineinfo reports only the visible part of a line cut off at the
        bottom edge, so the frame's requested height is used instead.
        """
        anchor = self._line_to_anchor.get(line - 1)
        frame = anchor.frame if anchor is not None else None
        if frame is None and self._editor_line == line:
            frame = self._active_comment_frame
        if frame is None or not frame.winfo_exists():
            return 0
        return frame.winfo_reqheight()

    def _update_minimap_viewport(self) -> None:
        c = self._minimap
        layout = self._mm_layout()
        if layout is None:
            c.delete('all')
            self._mm_img = None
            self._mm_item = 0
            self._mm_painted = None
            return
        ch, iw, chw, lh, content_h, n = layout
        top, bottom = self._mm_view_rows()
        offset = self._mm_offset_for(top / n, bottom / n, ch, content_h)
        if self._mm_painted is None or self._mm_painted[0] != offset:
            self._mm_paint_band(iw, chw, lh, offset, min(ch, content_h - offset))
        c.delete('viewport')
        y0, y1 = top * lh - offset, bottom * lh - offset
        c.create_rectangle(0, y0, c.winfo_width(), y1,
                           fill=C['fg'], stipple='gray12',
                           outline=C['fg'], width=1, tags='viewport')

    def _mm_scroll_to_row(self, row: float, span: int) -> int:
        """Put minimap row `row` at the top of the diff; returns the row used."""
        row_i = max(0, min(self._mm_nrows() - span, int(round(row))))
        i, seg = self._mm_line_of_row(row_i)
        self._manual_scroll = True
        self._stop_scroll_animation()
        if self._minimap_lines[i][0] == 'comment':
            starts = self._mm_starts()
            span = starts[i + 1] - starts[i] if starts is not None else 1
            self._move_view(f'{i + 1}.0', px=int(seg * self._comment_frame_height(i + 1) / span + 0.5))
        else:
            self._move_view(f'{i + 1}.{self._mm_char_at_col(i + 1, seg * (self._mm_cols or 0))}')
        return row_i

    def _on_minimap_press(self, event: tk.Event) -> None:
        layout = self._mm_layout()
        if layout is None:
            return
        ch, _iw, _chw, lh, content_h, n = layout
        top, bottom = self._mm_view_rows()
        offset = self._mm_offset_for(top / n, bottom / n, ch, content_h)
        y0, y1 = top * lh - offset, bottom * lh - offset
        if not (y0 <= event.y <= y1):
            # Outside the slider: centre the viewport on the clicked spot.
            span = bottom - top
            top = self._mm_scroll_to_row((offset + event.y) / lh - span / 2, span)
        self._mm_drag = (event.y, top)

    def _on_minimap_drag(self, event: tk.Event) -> None:
        layout = self._mm_layout()
        if layout is None or self._mm_drag is None:
            return
        ch, _iw, _chw, _lh, content_h, n = layout
        y_press, row0 = self._mm_drag
        top, bottom = self._mm_view_rows()
        span = bottom - top
        max_first = 1.0 - span / n
        # The slider's top edge is linear in the scroll position (y = first * k),
        # so dragging moves it by exactly the mouse delta -- mapping the
        # absolute pointer position instead would feed back through the
        # scrolling minimap and make the drag over-sensitive.
        if content_h <= ch or max_first <= 0:
            k = float(content_h)
        else:
            k = content_h - (content_h - ch) / max_first
        if k <= 0:
            return
        self._mm_scroll_to_row(row0 + (event.y - y_press) * n / k, span)

    # --line-number gutter ----------------------------------------------------

    def _render_gutter(self) -> None:
        c = self._gutter
        c.delete('all')
        mode = self._lineno_var.get()  # 0 off, 1 new, 2 old/new
        if not mode or not self._gutter_nums:
            return
        show_old = mode == 2
        max_old, max_new = self._gutter_max
        maxnum = max(max_new, max_old if show_old else 0)
        digits = max(1, len(str(maxnum)))
        charw = max(1, self._gutter_font.measure('0'))
        left_pad, right_pad = int(6 * self._scale), int(8 * self._scale)
        old_right = left_pad + digits * charw
        new_right = (old_right + charw + digits * charw) if show_old else old_right
        width = new_right + right_pad
        if c.winfo_width() != width:
            c.configure(width=width)  # re-fires <Configure>, but width is now stable
        height = c.winfo_height()
        if height <= 1:
            return
        top    = int(self._diff.index('@0,0').split('.')[0])
        bottom = int(self._diff.index(f'@0,{height - 1}').split('.')[0])
        color = C['subdued']
        for ln in range(top, bottom + 1):
            nums = self._gutter_nums.get(ln)
            if not nums:
                continue
            info = self._diff.dlineinfo(f'{ln}.0')
            if not info:
                continue
            y = info[1]
            old, new = nums
            if show_old and old is not None:
                c.create_text(old_right, y, anchor='ne', text=str(old),
                              fill=color, font=self._gutter_font)
            if new is not None:
                c.create_text(new_right, y, anchor='ne', text=str(new),
                              fill=color, font=self._gutter_font)
        c.create_line(width - 1, 0, width - 1, height, fill=_blend(C['subdued'], 0.45))

    @staticmethod
    def _file_label(df: DiffFile) -> tuple[str, str]:
        """Return (name_line, index_line) for both the sticky label and the diff header."""
        name = f'{df.old_path} -> {df.path}' if (df.status == 'R' and df.old_path) else df.path
        return name, df.index

    def _on_diff_yscroll(self, first: str, last: str) -> None:
        self._diff_vs.set(first, last)
        self._refresh_overview_if_stale()
        self._update_sticky_header()
        self._update_minimap_viewport()
        self._render_gutter()
        # Streaming appends change the fractions without moving the view;
        # only a real move invalidates the hovered row.
        top = self._diff.index('@0,0')
        moved, self._last_top = top != self._last_top, top
        if moved and (self._hover_line >= 0 or self._hover_btn_line >= 0):
            self._do_hide_hover(force=True)

    def _update_sticky_header(self) -> None:
        if not self._pos_order:
            self._sticky.configure(text='')
            return
        top = int(self._diff.index('@0,0').split('.')[0])
        path = self._pos_order[0][1]
        for line_no, p in self._pos_order:
            if line_no <= top:
                path = p
            else:
                break
        df = next((d for d in self._diff_files if d.path == path), None)
        if df is None:
            return
        name, idx = self._file_label(df)
        self._sticky.configure(text=f' {name}\n {idx}' if idx else f' {name}\n',
                               fg=C['fileheader_fg'], justify='left')
        if not self._manual_scroll:
            return
        row = self._flist_path_to_row.get(path, -1)
        if row > 0 and row != self._flist_selected_row:
            self._highlight_row(row)

    # --data ------------------------------------------------------------

    def _reload(self) -> None:
        if not self._can_reload or self._source is None:
            return
        self._show_progress('reloading...')
        try:
            new_diff = self._source.diff_text()
        except SystemExit:
            # GitSource.diff_text calls sys.exit on git failure; suppress so the
            # running app keeps working — surface the issue in the stat label
            # rather than tearing down the window.
            self._lbl_stat.configure(text='  reload failed (git error)')
            return
        self._pending_scroll_line = int(self._diff.index('@0,0').split('.')[0])
        self.diff_text = new_diff
        self._commits = self._source.commits()
        self._has_staged = self._source.has_staged()
        self._has_unstaged = self._source.has_unstaged()
        self._session_snapshots.clear()
        self._load()

    def _show_progress(self, text: str) -> None:
        """Swap the diff out for the progress label (if not already) and paint it."""
        self._progress.configure(text=text)
        if not self._progress_shown:
            self._diff.grid_remove()
            self._progress.grid(row=2, column=1, sticky='nsew')
            self._progress_shown = True
        self._progress.update_idletasks()

    def _show_diff(self) -> None:
        if self._progress_shown:
            self._progress.grid_remove()
            self._diff.grid()
            self._progress_shown = False

    def _load(self) -> None:
        self._rendering = True
        self._show_progress('parsing diff...')
        diff_files = parse_diff(self.diff_text)
        entries = entries_from_diff(diff_files)
        branch = try_current_branch()

        n = len(diff_files)
        add = sum(e.additions for e in entries)
        rem = sum(e.deletions for e in entries)
        stat = f'{n} file{"s" if n != 1 else ""} changed, +{add} -{rem}' if n else ''

        self._entries = entries
        self._render(branch, stat, diff_files, entries)
        self._loaded = True
        self._diff.focus_set()

    def _render(self, branch: str, stat: str,
                diff_files: list[DiffFile], entries: list[FileEntry]) -> None:
        self._lbl_branch.configure(text=f'branch:  {branch}' if branch else '')
        self._lbl_stat.configure(text=f'  {stat}' if stat else '')
        self._diff_files = diff_files
        self._render_flist(entries)
        self._render_diff_panel()

    def _render_diff_panel(self, restore_line: 'int | None' = None) -> None:
        """Rebuild the diff widget from self._diff_files.

        The whole document is emitted synchronously as plain lines, behind a
        progress label, so its size, file positions, gutter numbers and
        minimap are final before the first paint and every jump works
        immediately.  Word-diff highlighting, the expensive part, is applied
        in place afterwards in timed chunks (_word_diff_step); it never
        changes the line structure.
        """
        self._rendering = True
        self._show_progress('rendering...')
        self._cancel_hide_schedule()
        self._comment_hover_btn.place_forget()
        self._copy_hover_btn.place_forget()
        self._hover_line = -1
        self._hover_row = None
        self._hover_range = None
        self._hover_row_range = None
        self._hover_btn_line = -1
        if self._active_comment_frame:
            self._active_comment_frame.destroy()
            self._active_comment_frame = None
            self._active_comment_entry = None
            self._comment_target = None
        self._editor_line = None
        for sep in self._hunk_seps:
            sep.destroy()
        self._hunk_seps.clear()
        self._comment_frames.clear()
        self._line_to_anchor = {}
        self._line_post_image = {}
        self._pending_anchors = self._resolve_review_anchors()
        self._diff.delete('1.0', 'end')
        self._positions.clear()
        self._minimap_lines = []
        self._gutter_nums = {}
        self._gutter_max = (0, 0)
        self._buf = []
        self._pending_seps = []
        self._cur_line = 0
        self._mm_rows.clear()
        self._mm_painted = None
        self._mm_row_start = None
        self._cancel_word_diff_pass()
        self._wd_pending = []
        self._cmt_list_actions = []  # stale line targets until the pane is rebuilt

        if restore_line is None:
            restore_line = self._pending_scroll_line
        self._pending_scroll_line = None

        if not self._diff_files:
            self._emit('Empty diff.\n', 'subdued')
        n_files = len(self._diff_files)
        next_progress = time.perf_counter() + CFG.progress_interval_ms / 1000
        for i, df in enumerate(self._diff_files):
            if time.perf_counter() >= next_progress:
                self._show_progress(f'rendering {i} / {n_files} files...')
                next_progress = time.perf_counter() + CFG.progress_interval_ms / 1000
            if i > 0:
                self._emit('\n', 'context')
                self._minimap_lines.append(('context', ''))
            name, idx = self._file_label(df)
            self._emit(f' {name}\n', 'filehdr')
            self._positions[df.path] = f'{self._cur_line}.0'
            self._minimap_lines.append(('filehdr', f' {name}'))
            if idx:
                self._emit(f' {idx}\n', 'fileidx')
                self._minimap_lines.append(('fileidx', f' {idx}'))
            self._render_file_diff(df)
            self._flush_buf()
        self._flush_buf()
        self._update_pos_order()
        self._show_diff()
        self._rendering = False

        if restore_line is not None:
            self._scroll_diff_to_line(restore_line)
        elif self._pending_scroll_frac is not None:
            # Legacy window state: a fraction only means something once the
            # widget has laid the document out, hence after idle.
            frac, self._pending_scroll_frac = self._pending_scroll_frac, None
            self.root.after_idle(lambda: self._diff.yview_moveto(frac))
        self.root.after_idle(self._update_sticky_header)
        self.root.after_idle(self._mm_relayout)
        self.root.after_idle(self._render_gutter)
        self.root.after_idle(self._update_hunk_sep_widths)
        self.root.after_idle(self._update_commits_section)
        self.root.after_idle(self._update_comments_section)
        self.root.after_idle(self._render_overview)
        if self._wd_pending:
            # A timer rather than after_idle: the window must map and paint
            # before the highlighting pass starts competing for idle time.
            self._wd_after_id = self.root.after(1, self._start_word_diff_pass)

    def _update_pos_order(self) -> None:
        self._pos_order = sorted(
            (int(pos.split('.')[0]), path)
            for path, pos in self._positions.items()
        )

    def _emit(self, chars: str, tag: str = '') -> None:
        """Queue text for the diff widget; _flush_buf inserts it in one call.

        Text.insert costs a few microseconds per tagged segment regardless of
        its length, so adjacent segments with the same tag are merged: a run
        of removed lines becomes one segment instead of one per line.  Line
        numbers are tracked here so nothing has to ask the widget for 'end'.
        """
        buf = self._buf
        if buf and buf[-1][0] == tag:
            buf[-1][1].append(chars)
        else:
            buf.append((tag, [chars]))
        self._cur_line += chars.count('\n')

    def _flush_buf(self) -> None:
        if self._buf:
            args: list[str] = []
            for tag, parts in self._buf:
                args.append(''.join(parts))
                args.append(tag)
            self._diff.insert('end', *args)
            self._buf = []
        for ln, sep in self._pending_seps:
            self._diff.window_create(f'{ln}.0', window=sep)
        self._pending_seps = []

    def _record_gutter(self, old_no: Optional[int], new_no: Optional[int]) -> None:
        self._gutter_nums[self._cur_line] = (old_no, new_no)
        mo, mn = self._gutter_max
        self._gutter_max = (max(mo, old_no or 0), max(mn, new_no or 0))

    def _insert_word_diff(self, old_dl: DiffLine, new_dl: DiffLine, file_path: str,
                          same_tokens: bool) -> None:
        old_text = old_dl.text[1:]
        new_text = new_dl.text[1:]
        if same_tokens and self._word_diff_var.get() == 2:
            self._emit(f'~{new_text}\n', 'reindent')
            self._minimap_lines.append(('reindent', '~' + new_text))
            self._record_gutter(old_dl.old_line_no, new_dl.new_line_no)
            # The single rendered line stands in for both the - and + sides;
            # offer both as anchor candidates.
            self._insert_comment_annotation(file_path, old_dl, old_dl.text)
            self._insert_comment_annotation(file_path, new_dl, new_dl.text)
            return
        # Plain for now; _apply_word_diff rewrites both lines with word tags.
        line_old = self._cur_line + 1
        self._emit(f'-{old_text}\n', 'removed')
        self._minimap_lines.append(('removed', '-' + old_text))
        self._record_gutter(old_dl.old_line_no, None)
        self._insert_comment_annotation(file_path, old_dl, old_dl.text)
        line_new = self._cur_line + 1
        self._emit(f'+{new_text}\n', 'added')
        self._minimap_lines.append(('added', '+' + new_text))
        self._record_gutter(None, new_dl.new_line_no)
        self._insert_comment_annotation(file_path, new_dl, new_dl.text)
        self._wd_pending.append((line_old, line_new, old_text, new_text))

    def _render_file_diff(self, df: DiffFile) -> None:
        word_diff = self._word_diff_var.get()
        pending_rem: list[DiffLine] = []
        pending_add: list[DiffLine] = []

        def plain(dl: DiffLine, tag: str) -> None:
            self._emit(dl.text + '\n', tag)
            self._minimap_lines.append((tag, dl.text))
            if tag == 'removed':
                self._record_gutter(dl.old_line_no, None)
            else:
                self._record_gutter(None, dl.new_line_no)
            self._insert_comment_annotation(df.path, dl, dl.text)

        def flush() -> None:
            if word_diff and pending_rem and pending_add:
                actions = _pair_lines_for_word_diff(
                    [d.text[1:] for d in pending_rem],
                    [d.text[1:] for d in pending_add])
                rem_iter = iter(pending_rem)
                add_iter = iter(pending_add)
                for action in actions:
                    if action[0] == 'pair':
                        self._insert_word_diff(next(rem_iter), next(add_iter), df.path, action[3])
                    elif action[0] == 'rem':
                        plain(next(rem_iter), 'removed')
                    else:
                        plain(next(add_iter), 'added')
            else:
                for dl in pending_rem:
                    plain(dl, 'removed')
                for dl in pending_add:
                    plain(dl, 'added')
            pending_rem.clear()
            pending_add.clear()

        for dl in df.lines:
            if dl.kind == 'fileheader':
                continue
            if dl.kind == 'hunk':
                flush()
                sep = tk.Canvas(self._diff, height=1, bg=C['subdued'],
                                highlightthickness=0, bd=0,
                                width=max(self._diff.winfo_width(), 1))
                self._bind_wheel(sep)
                self._emit('\n')  # the separator's own line; embedded at flush
                self._minimap_lines.append(('hunksep', ''))
                self._pending_seps.append((self._cur_line, sep))
                self._hunk_seps.append(sep)
                self._emit(dl.text + '\n', dl.kind)
                self._minimap_lines.append((dl.kind, dl.text))
            elif dl.kind == 'removed':
                if pending_add:
                    flush()
                pending_rem.append(dl)
            elif dl.kind == 'added':
                pending_add.append(dl)
            else:
                flush()
                self._emit(dl.text + '\n', dl.kind)
                self._minimap_lines.append((dl.kind, dl.text))
                self._record_gutter(dl.old_line_no, dl.new_line_no)
                self._insert_comment_annotation(df.path, dl, dl.text)
        flush()
        self._insert_orphan_comments_for_file(df.path)

    # --word-diff highlighting pass ------------------------------------------

    def _cancel_word_diff_pass(self) -> None:
        if self._wd_after_id is not None:
            self.root.after_cancel(self._wd_after_id)
            self._wd_after_id = None
        self._wd_down.clear()
        self._wd_up.clear()

    def _start_word_diff_pass(self) -> None:
        self._wd_after_id = None
        pend, self._wd_pending = self._wd_pending, []
        if not pend:
            return
        # Outward from the visible region, alternating below and above it,
        # so what is on screen is highlighted in the first chunk.
        top = int(self._diff.index('@0,0').split('.')[0])
        k = bisect.bisect_left(pend, (top,))
        self._wd_down = deque(pend[k:])
        self._wd_up = deque(reversed(pend[:k]))
        self._word_diff_step()

    def _word_diff_step(self) -> None:
        self._wd_after_id = None
        t0 = time.perf_counter()
        budget = CFG.render_chunk_ms / 1000
        down, up = self._wd_down, self._wd_up
        while (down or up) and time.perf_counter() - t0 < budget:
            for q in (down, up):
                if q:
                    self._apply_word_diff(*q.popleft())
        if down or up:
            self._wd_after_id = self.root.after(1, self._word_diff_step)

    def _apply_word_diff(self, line_old: int, line_new: int, old_text: str, new_text: str) -> None:
        d = self._diff
        if (d.get(f'{line_old}.0', f'{line_old}.end') != '-' + old_text
                or d.get(f'{line_new}.0', f'{line_new}.end') != '+' + new_text):
            return  # not the lines we expected; leave them plain
        tok_old = re.findall(r'\w+|[^\w\s]|\s+', old_text) or ['']
        tok_new = re.findall(r'\w+|[^\w\s]|\s+', new_text) or ['']
        opcodes = _word_matcher(tok_old, tok_new).get_opcodes()
        segs_old: list[str] = ['-', 'removed']
        segs_new: list[str] = ['+', 'added']
        for op, i1, i2, j1, j2 in opcodes:
            text = ''.join(tok_old[i1:i2])
            if text:
                unchanged = op == 'equal' or text.isspace()
                segs_old += (text, 'removed_word' if unchanged else 'removed_hi')
            text = ''.join(tok_new[j1:j2])
            if text:
                unchanged = op == 'equal' or text.isspace()
                segs_new += (text, 'added_word' if unchanged else 'added_hi')
        # replace (rather than delete + insert) keeps the view still even
        # when the rewritten line is the one at the top of the window.
        for line, segs in ((line_old, segs_old), (line_new, segs_new)):
            foreign = self._foreign_tag_ranges(line)
            d.replace(f'{line}.0', f'{line + 1}.0', *segs, '\n', '')
            for tag, start, end in foreign:
                d.tag_add(tag, start, end)

    _OWN_LINE_TAGS = frozenset(('removed', 'added', 'removed_word', 'removed_hi',
                                'added_word', 'added_hi'))

    def _foreign_tag_ranges(self, line: int) -> list[tuple[str, str, str]]:
        """Tags on `line` that the skeleton did not put there (selection,
        hover), as (tag, start, end) so a rewrite can restore them."""
        out: list[tuple[str, str, str]] = []
        open_at: dict[str, str] = {}
        first, last = f'{line}.0', f'{line + 1}.0'
        for key, tag, index in self._diff.dump(first, last, tag=True):
            if tag in self._OWN_LINE_TAGS:
                continue
            if key == 'tagon':
                open_at[tag] = index
            elif key == 'tagoff':
                out.append((tag, open_at.pop(tag, first), index))
        out.extend((tag, start, last) for tag, start in open_at.items())
        return out

    # --incremental line edits ------------------------------------------------

    def _shift_lines(self, from_line: int, delta: int) -> None:
        """Keep every line-keyed record consistent after inserting (delta > 0)
        or deleting (delta < 0) lines in front of `from_line`.

        Callers adjust self._minimap_lines themselves, since the new rows
        depend on what was inserted.
        """
        def sh(ln: int) -> int:
            return ln + delta if ln >= from_line else ln
        self._gutter_nums = {sh(k): v for k, v in self._gutter_nums.items()}
        self._line_to_anchor = {sh(k): v for k, v in self._line_to_anchor.items()}
        self._line_post_image = {sh(k): v for k, v in self._line_post_image.items()}
        for anchors in self._pending_anchors.values():
            for a in anchors:
                if a.src_line is not None:
                    a.src_line = sh(a.src_line)
        self._positions = {path: f'{sh(int(pos.split(".")[0]))}.0'
                           for path, pos in self._positions.items()}
        self._update_pos_order()
        self._wd_pending = [(sh(a), sh(b), o, n) for a, b, o, n in self._wd_pending]
        self._wd_down = deque((sh(a), sh(b), o, n) for a, b, o, n in self._wd_down)
        self._wd_up = deque((sh(a), sh(b), o, n) for a, b, o, n in self._wd_up)
        if self._editor_line is not None:
            self._editor_line = sh(self._editor_line)
        self._cur_line += delta
        self._mm_rows.clear()
        self._do_hide_hover(force=True)

    def _after_line_edit(self) -> None:
        self._mm_relayout()
        self._render_gutter()
        self._update_sticky_header()
        self._update_comments_section()  # its click targets are line numbers
        self._render_overview()

    # --overview strip -------------------------------------------------------

    def _overview_marks(self) -> list[tuple[int, int]]:
        """(comment line, source line) of every rendered comment, in order."""
        return sorted((a.src_line + 1, a.src_line)
                      for a in self._line_to_anchor.values() if a.src_line is not None)

    def _overview_span(self) -> tuple[int, int]:
        """(top, bottom) of the scrollbar's trough, in strip pixels.

        The strip is as tall as the scrollbar, but the scrollbar's arrow
        buttons take the ends, so marks are mapped onto the trough between
        them to line up with the thumb. delta(0, 1) is the fraction one
        pixel of the trough stands for, which gives its length exactly.
        """
        sb = self._diff_vs
        h = sb.winfo_height()
        try:
            delta = sb.delta(0, 1)
        except tk.TclError:
            delta = 0.0
        field = int(round(1 / delta)) + 1 if delta > 0 else 0
        if field < 2 or field > h:
            return 0, h  # collapsed or not yet laid out
        top = (h - field) // 2
        return top, top + field

    def _schedule_overview_refresh(self) -> None:
        if self._overview_refresh_id is None:
            self._overview_refresh_id = self.root.after(50, self._render_overview)

    def _refresh_overview_if_stale(self) -> None:
        """Redraw the strip when the document's pixel height changed since
        it was drawn: Tk measures line heights in the background for a while
        after a render, moving the scrollbar thumb, and this is called from
        the scroll callback Tk fires as it does so."""
        if self._ypixels('1.0', 'end') != self._overview_total:
            self._schedule_overview_refresh()

    def _render_overview(self) -> None:
        """Marks are pixel-proportional, from the same (possibly still
        estimated) line metrics the scrollbar thumb is placed with, so a
        mark sits where the thumb's top is when its line tops the view."""
        if self._overview_refresh_id is not None:
            self.root.after_cancel(self._overview_refresh_id)
            self._overview_refresh_id = None
        total = self._overview_total = self._ypixels('1.0', 'end')
        c = self._overview
        c.delete('all')
        self._overview_drawn = []
        w = c.winfo_width()
        top, bottom = self._overview_span()
        h = bottom - top
        if h <= 1 or total <= 0:
            return

        def y_of(line: int) -> int:
            return top + int(self._ypixels('1.0', f'{line}.0') * h / total)

        if CFG.overview_file_ticks:
            # Faint, 1 px, under the comment marks: a run of small files
            # merges into a grey band, a large file shows as a gap. One
            # item per strip row, however many files share it.
            th = max(1, int(round(self._scale)))
            painted = bytearray(bottom)
            for line, _path in self._pos_order:
                y = min(y_of(line), bottom - th)
                if not painted[y]:
                    painted[y] = 1
                    c.create_rectangle(0, y, w, y + th, fill=C['topbar_bg'], outline='')
        mh = max(2, int(round(2 * self._scale)))
        for line, src in self._overview_marks():
            y = min(y_of(line), bottom - mh)
            c.create_rectangle(0, y, w, y + mh, fill=C['comment_fg'], outline='')
            self._overview_drawn.append((y, src))

    def _on_overview_click(self, event: tk.Event) -> None:
        """A click on a mark jumps to its comment; elsewhere it scrolls to
        the proportional position, like a scrollbar trough."""
        top, bottom = self._overview_span()
        h = bottom - top
        if h <= 1:
            return
        # Hit-test what is drawn, not a recomputation: the drawing can lag
        # Tk's metrics by a refresh, and the user clicked what they saw.
        if self._overview_drawn:
            y, src = min(self._overview_drawn, key=lambda m: abs(m[0] - event.y))
            if abs(y - event.y) <= 4 * self._scale:
                self._jump_to_diff_line(src)  # same path as the comments panel
                return
        self._manual_scroll = True
        self._stop_scroll_animation()
        self._move_view('moveto', str(min(1.0, max(0.0, (event.y - top) / h))))

    def _insert_blank_line(self, line: int) -> None:
        """Open an empty line at `line`, pushing the current one down."""
        self._diff.insert(f'{line - 1}.end', '\n')
        # Tk gives the new newline the tags of the line it splits; a comment
        # row must not look or hover like a removed/added line.
        for tag in self._diff.tag_names(f'{line}.0'):
            self._diff.tag_remove(tag, f'{line}.0', f'{line + 1}.0')
        self._shift_lines(line, 1)
        self._minimap_lines.insert(line - 1, ('comment', ''))

    def _delete_lines(self, first: int, count: int) -> None:
        self._diff.delete(f'{first}.0', f'{first + count}.0')
        del self._minimap_lines[first - 1:first - 1 + count]
        self._shift_lines(first + count, -count)

    def _render_flist(self, entries: list[FileEntry]) -> None:
        self._flist_selected_row = -1
        self._flist_row_to_entry = []
        self._flist_path_to_row = {}
        self._flist.configure(state='normal')
        self._flist.delete('1.0', 'end')

        if self._tree_var.get():
            rows = _build_tree_rows(entries)
            for label, depth, entry in rows:
                indent = '  ' * depth
                if entry is None:
                    self._flist.insert('end', f'{indent}{label}\n', 'dir')
                    self._flist_row_to_entry.append(None)
                else:
                    stats: list[str] = []
                    if entry.additions:
                        stats.append(f'+{entry.additions}')
                    if entry.deletions:
                        stats.append(f'-{entry.deletions}')
                    self._flist.insert('end', f'{indent}', 'dir')
                    self._flist.insert('end', f'{entry.status} ', f'status_{entry.status}')
                    self._flist.insert('end', label)
                    if stats:
                        self._flist.insert('end', f'  {" ".join(stats)}', 'stats')
                    self._flist.insert('end', '\n')
                    display_row = len(self._flist_row_to_entry) + 1
                    self._flist_path_to_row[entry.path] = display_row
                    self._flist_row_to_entry.append(entry)
        else:
            prev_path = ''
            for e in entries:
                parts: list[str] = []
                if e.additions:
                    parts.append(f'+{e.additions}')
                if e.deletions:
                    parts.append(f'-{e.deletions}')
                self._flist.insert('end', f' {e.status} ', f'status_{e.status}')
                prefix = _common_dir_prefix(prev_path, e.path)
                self._flist.insert('end', ' ')
                if prefix:
                    self._flist.insert('end', prefix, 'dir')
                    self._flist.insert('end', e.path[len(prefix):])
                else:
                    self._flist.insert('end', e.path)
                if parts:
                    self._flist.insert('end', f'  {" ".join(parts)}', 'stats')
                self._flist.insert('end', '\n')
                display_row = len(self._flist_row_to_entry) + 1
                self._flist_path_to_row[e.path] = display_row
                self._flist_row_to_entry.append(e)
                prev_path = e.path

        self._flist.configure(state='disabled')
        if entries:
            first_file_row = next(
                (i + 1 for i, e in enumerate(self._flist_row_to_entry) if e is not None), -1
            )
            if first_file_row > 0:
                self._highlight_row(first_file_row)

    # --interaction ----------------------------------------------------------

    def _flist_nav(self, offset: int) -> None:
        self._jump_to_adjacent_file(offset)
        self._flist.focus_set()

    def _flist_activate(self) -> None:
        if self._flist_selected_row > 0:
            entry = self._flist_row_to_entry[self._flist_selected_row - 1]
            if entry is not None:
                self._jump_to(entry.path)
                self._diff.focus_set()

    def _on_file_click(self, event: tk.Event) -> None:
        idx = self._flist.index(f'@{event.x},{event.y}')
        row_0 = int(idx.split('.')[0]) - 1
        if 0 <= row_0 < len(self._flist_row_to_entry):
            entry = self._flist_row_to_entry[row_0]
            if entry is not None:
                self._highlight_row(row_0 + 1)
                self._jump_to(entry.path)

    def _highlight_row(self, row: int) -> None:
        self._flist_selected_row = row
        self._flist.tag_remove('selected', '1.0', 'end')
        self._flist.tag_add('selected', f'{row}.0', f'{row}.end+1c')
        self._flist.see(f'{row}.0')

    def _jump_to_adjacent_file(self, offset: int) -> None:
        if not self._entries:
            return
        cur_entry: FileEntry | None = None
        if self._flist_selected_row > 0:
            cur_entry = self._flist_row_to_entry[self._flist_selected_row - 1]
        elif self._pos_order:
            top = int(self._diff.index('@0,0').split('.')[0])
            path = self._pos_order[0][1]
            for line_no, p in self._pos_order:
                if line_no <= top:
                    path = p
                else:
                    break
            cur_entry = next((e for e in self._entries if e.path == path), None)
        if cur_entry is None:
            return
        paths = [e.path for e in self._entries]
        try:
            idx = paths.index(cur_entry.path)
        except ValueError:
            return
        target = (idx + offset) % len(self._entries)
        target_entry = self._entries[target]
        display_row = self._flist_path_to_row.get(target_entry.path, -1)
        if display_row > 0:
            self._highlight_row(display_row)
        self._jump_to(target_entry.path)
        self._diff.focus_set()

    def _source_location(self, text_line: int) -> tuple[str, int | None]:
        """Return (file_path, new-file line number) for a diff text widget line."""
        if not self._pos_order:
            return '', None

        path = self._pos_order[0][1]
        for ln, p in self._pos_order:
            if ln <= text_line:
                path = p
            else:
                break

        # Scan backwards for the nearest @@ hunk header.
        hunk_line = None
        new_start = None
        for ln in range(text_line, 0, -1):
            content = self._diff.get(f'{ln}.0', f'{ln}.end')
            if content.startswith('@@ '):
                m = re.search(r'\+(\d+)', content)
                if m:
                    new_start = int(m.group(1))
                    hunk_line = ln
                break

        if hunk_line is None or new_start is None:
            return path, None

        # Walk from the hunk header to the clicked line tracking new-file line number.
        # Removed lines (-) don't exist in the new file, so only context and added lines advance.
        new_line = new_start - 1
        for ln in range(hunk_line + 1, text_line + 1):
            if not self._diff.get(f'{ln}.0', f'{ln}.1').startswith('-'):
                new_line += 1

        return path, new_line

    def _widget_under_pointer(self) -> tk.Widget | None:
        try:
            return self.root.winfo_containing(*self.root.winfo_pointerxy())
        except (tk.TclError, KeyError):
            return None

    def _line_under_pointer(self) -> int | None:
        try:
            x_root, y_root = self.root.winfo_pointerxy()
        except tk.TclError:
            return None
        x = x_root - self._diff.winfo_rootx()
        y = y_root - self._diff.winfo_rooty()
        if x < 0 or y < 0 or x >= self._diff.winfo_width() or y >= self._diff.winfo_height():
            return None
        return int(self._diff.index(f'@{x},{y}').split('.')[0])

    @staticmethod
    def _format_comment_block(comment: str, moved: bool = False,
                              width: 'int | None' = None) -> str:
        """The comment as shown in the diff: a marker on the first line and
        indented continuation lines, wrapped to `width` columns if given."""
        prefix       = '~ >> ' if moved else '  >> '
        continuation = '~    ' if moved else '     '
        cmt_lines = comment.splitlines() or ['']
        if width is not None:
            wrapped: list[str] = []
            for l in cmt_lines:
                wrapped.extend(textwrap.wrap(
                    l, max(1, width - len(prefix)),
                    break_long_words=True, break_on_hyphens=False,
                    expand_tabs=False, replace_whitespace=False,
                    drop_whitespace=False) or [''])
            cmt_lines = wrapped
        return '\n'.join([prefix + cmt_lines[0]]
                         + [continuation + l for l in cmt_lines[1:]])

    def _comment_button_metrics(self) -> tuple[int, int]:
        """(width, height) taken by a comment row's two buttons and their
        padding; measured once from throwaway buttons."""
        cached = getattr(self, '_comment_btn_metrics', None)
        if cached is not None:
            return cached
        font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        w = h = 0
        for text in ('remove', 'copy(c)'):
            b = tk.Button(self._diff, text=text, relief='flat', bd=0,
                          highlightthickness=0, font=font)
            w += b.winfo_reqwidth() + 8  # pack padx=4 on both sides
            h = max(h, b.winfo_reqheight())
            b.destroy()
        self._comment_btn_metrics = (w, h)
        return self._comment_btn_metrics

    def _comment_wrap_cols(self) -> 'int | None':
        """Columns available for comment text at the diff's current width,
        or None when the widget has no usable width yet (before first map)."""
        w = self._diff.winfo_width() - self._diff_row_pad() - self._comment_button_metrics()[0]
        charw = max(1, self._gutter_font.measure('0'))
        cols = w // charw - 1  # the label's own padding
        return cols if cols >= CFG.comment_wrap_min_cols else None

    def _comment_block_for(self, anchor: '_ResolvedAnchor') -> str:
        return self._format_comment_block(anchor.comment, anchor.moved, self._comment_wrap_cols())

    def _schedule_rewrap(self) -> None:
        # Coalesces the burst of <Configure> events from a resize drag.
        if self._rewrap_id is None:
            self._rewrap_id = self.root.after(100, self._rewrap_comments)

    def _rewrap_comments(self) -> None:
        """Re-wrap every rendered comment for the current width."""
        self._rewrap_id = None
        cols = self._comment_wrap_cols()
        # Compared per label rather than against the last rewrap width: a
        # frame built during the debounce window may have used another.
        changed = False
        btn_h = self._comment_button_metrics()[1]
        for src, anchor in self._line_to_anchor.items():
            label, frame = anchor.label, anchor.frame
            if label is None or frame is None or not frame.winfo_exists():
                continue
            block = self._format_comment_block(anchor.comment, anchor.moved, cols)
            if label.cget('text') == block:
                continue
            label.configure(text=block)
            frame.configure(height=max(label.winfo_reqheight(), btn_h))
            self._minimap_lines[src] = ('comment', block)  # the comment line is src + 1
            changed = True
        if changed:
            self._mm_relayout()
            self._render_gutter()
            self._schedule_overview_refresh()  # frames grew or shrank

    def _loc_for_line(self, line_no: int) -> str | None:
        path, ln = self._source_location(line_no)
        if not path:
            return None
        return f'{path}:{ln}' if ln is not None else path

    def _comment_for_line(self, line_no: int) -> '_ResolvedAnchor | None':
        if 'comment' not in self._diff.tag_names(f'{line_no}.0'):
            return None
        src_line = line_no - 1
        if src_line < 1:
            return None
        return self._line_to_anchor.get(src_line)

    def _copy_loc_and_lines(self, anchor_line: int | None = None) -> None:
        try:
            sel_first = self._diff.index('sel.first')
            sel_last  = self._diff.index('sel.last')
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first is not None:
            lines_start = int(sel_first.split('.')[0])
            lines_end   = int(sel_last.split('.')[0])
            if sel_last.split('.')[1] == '0' and lines_end > lines_start:
                lines_end -= 1
        else:
            line = (anchor_line
                    or self._line_under_pointer()
                    or int(self._diff.index('insert').split('.')[0]))
            # If cursor is on a comment annotation, anchor to source line above
            # and include the annotation in the copy.
            if 'comment' in self._diff.tag_names(f'{line}.0') and line > 1:
                lines_start = line - 1
                lines_end   = line
            else:
                lines_start = lines_end = line

        loc = self._loc_for_line(lines_start)
        if loc is None:
            return
        parts = []
        for ln in range(lines_start, lines_end + 1):
            content = self._diff.get(f'{ln}.0', f'{ln}.end')
            if content:
                parts.append(content)
                continue
            cmt = self._comment_for_line(ln)
            if cmt is not None:
                parts.append(self._format_comment_block(cmt.comment, cmt.moved))
        text = '\n'.join(parts)
        self.root.clipboard_clear()
        self.root.clipboard_append(f'{loc}\n{text}\n')

    def _add_comment_at_cursor(self) -> None:
        line_no = (self._line_under_pointer()
                   or (self._hover_line if self._hover_line >= 0 else None)
                   or int(self._diff.index('insert').split('.')[0]))
        self._open_comment_editor(line_no)

    def _show_diff_context_menu(self, event: tk.Event) -> None:
        text_line = int(self._diff.index(f'@{event.x},{event.y}').split('.')[0])
        path, line_no = self._source_location(text_line)
        if not path:
            return

        loc = f'{path}:{line_no}' if line_no is not None else path
        menu_kw = dict(bg=C['topbar_bg'], fg=C['fg'],
                       activebackground=C['selected_bg'], activeforeground=C['fg'],
                       relief='flat', bd=0, tearoff=0,
                       font=(CFG.font_family, int(CFG.menu_font_size * self._scale)))
        menu = tk.Menu(self.root, **menu_kw)
        menu.add_command(label=f'Copy "{loc}"',
                         command=lambda: (self.root.clipboard_clear(),
                                          self.root.clipboard_append(loc)))

        try:
            sel_first = self._diff.index('sel.first')
            sel_last  = self._diff.index('sel.last')
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first is not None:
            lines_start = int(sel_first.split('.')[0])
            lines_end   = int(sel_last.split('.')[0])
            if sel_last.split('.')[1] == '0' and lines_end > lines_start:
                lines_end -= 1
            lines_path, lines_line_no = self._source_location(lines_start)
            lines_loc = f'{lines_path}:{lines_line_no}' if lines_line_no is not None else lines_path
        else:
            lines_start = lines_end = text_line
            lines_loc = loc

        n_lines = lines_end - lines_start + 1
        lines_text = self._diff.get(f'{lines_start}.0', f'{lines_end}.end')
        menu.add_command(
            label=f'Copy "{lines_loc}" + {n_lines} {"line" if n_lines == 1 else "lines"}',
            accelerator='c',
            command=lambda: self._copy_loc_and_lines(text_line))

        menu.tk_popup(event.x_root, event.y_root)

    @staticmethod
    def _side_for_kind(kind: str) -> str:
        if kind == 'added':   return '+'
        if kind == 'removed': return '-'
        return ' '

    def _read_current_file(self, file_path: str) -> 'str | None':
        if not self._repo_root:
            return None
        return _read_text_safe(self._repo_root / file_path)

    def _resolve_review_anchors(self) -> dict[str, list['_ResolvedAnchor']]:
        """Map every stored comment through its snapshot to a target line in
        the current working tree (via difflib). Unmatched anchors fall
        through to orphan rendering at the end of each file's hunks."""
        result: dict[str, list[_ResolvedAnchor]] = {}
        current_cache: dict[str, str | None] = {}
        map_cache: dict[tuple[str, str], dict[int, int]] = {}
        for file, entry in self._review.all_entries():
            snap_sha  = str(entry.get('snapshot') or '')
            line_no   = int(entry.get('line_no') or 0)
            side      = str(entry.get('side') or ' ')
            line_text = str(entry.get('line_text') or '')
            comment   = str(entry.get('comment') or '')
            if not (snap_sha and line_no and comment):
                continue
            if file not in current_cache:
                current_cache[file] = self._read_current_file(file)
            current = current_cache[file]
            snap    = self._review.read_snapshot(snap_sha)
            if current is not None and snap is not None:
                key = (snap_sha, file)
                line_map = map_cache.get(key)
                if line_map is None:
                    line_map = _compute_line_map(snap, current)
                    map_cache[key] = line_map
                target = line_map.get(line_no, line_no)
            else:
                target = line_no
            result.setdefault(file, []).append(_ResolvedAnchor(
                file=file, snapshot=snap_sha, snap_line_no=line_no,
                target_line_no=target, side=side, line_text=line_text,
                comment=comment,
            ))
        return result

    def _consume_anchor(self, file_path: str, new_line_no: int, side: str,
                        rendered_text: str) -> '_ResolvedAnchor | None':
        anchors = self._pending_anchors.get(file_path)
        if not anchors:
            return None
        exact: '_ResolvedAnchor | None' = None
        loose: '_ResolvedAnchor | None' = None
        for a in anchors:
            if a.matched or a.target_line_no != new_line_no or a.side != side:
                continue
            if a.line_text == rendered_text:
                exact = a
                break
            loose = loose or a
        a = exact or loose
        if a is None:
            return None
        a.matched = True
        a.moved   = (exact is None) or (a.snap_line_no != a.target_line_no)
        return a

    def _insert_comment_annotation(self, file_path: str, dl: DiffLine,
                                   line_text: str) -> None:
        src_line_no = self._cur_line
        if dl.new_line_no is not None:
            self._line_post_image[src_line_no] = (
                file_path, dl.new_line_no, self._side_for_kind(dl.kind), line_text,
            )
        if dl.new_line_no is None:
            return
        anchor = self._consume_anchor(
            file_path, dl.new_line_no, self._side_for_kind(dl.kind), line_text,
        )
        if anchor is None:
            return
        anchor.src_line = src_line_no
        self._line_to_anchor[src_line_no] = anchor
        self._render_comment_frame(src_line_no, anchor)

    def _build_comment_frame(self, src_line_no: int, anchor: '_ResolvedAnchor') -> tk.Frame:
        cmt_display = self._comment_block_for(anchor)
        frame = tk.Frame(self._diff, bg=self._comment_bg)
        label = tk.Label(
            frame, text=cmt_display,
            bg=self._comment_bg, fg=C['bg'],
            anchor='w', justify='left', cursor='hand2',
            font=(CFG.font_family, CFG.font_size),
        )
        btn = tk.Button(
            frame, text='remove',
            bg=self._comment_bg, fg=C['removed_fg'],
            activebackground=self._comment_bg, activeforeground=C['removed_fg'],
            relief='flat', bd=0, highlightthickness=0, cursor='hand2',
            font=(CFG.font_family, int(CFG.menu_font_size * self._scale)),
            command=lambda a=anchor: self._delete_comment(a),
        )
        copy_btn = tk.Button(
            frame, text='copy(c)',
            bg=self._comment_bg, fg=C['fg'],
            activebackground=self._comment_bg, activeforeground=C['fg'],
            relief='flat', bd=0, highlightthickness=0, cursor='hand2',
            font=(CFG.font_family, int(CFG.menu_font_size * self._scale)),
            command=lambda a=anchor: self._copy_loc_and_lines((a.src_line or 0) + 1),
        )
        btn.pack(side='right', padx=4)
        copy_btn.pack(side='right', padx=4)
        label.pack(side='left', fill='x', expand=True)
        # Requested sizes are valid without update_idletasks, and calling it
        # here would let Tk paint the half-built document mid-render.
        h = max(label.winfo_reqheight(), btn.winfo_reqheight(), copy_btn.winfo_reqheight())
        w = max(self._diff.winfo_width() - self._diff_row_pad(), 1)
        frame.configure(width=w, height=h)
        frame.pack_propagate(False)
        def _on_click(e: tk.Event) -> str:
            if anchor.src_line is not None:
                self._open_comment_editor(anchor.src_line)
            return 'break'
        label.bind('<Button-1>', _on_click)
        frame.bind('<Button-1>', _on_click)
        frame.bind('<Enter>', lambda e: self._do_hide_hover())
        label.bind('<Enter>', lambda e: self._do_hide_hover())
        btn.bind('<Enter>',   lambda e: self._do_hide_hover())
        copy_btn.bind('<Enter>', lambda e: self._do_hide_hover())
        for w in (frame, label, btn, copy_btn):
            self._bind_wheel(w)
        self._discard_frame(anchor)
        anchor.frame = frame
        anchor.label = label
        self._comment_frames.append(frame)
        return frame

    def _discard_frame(self, anchor: '_ResolvedAnchor') -> None:
        frame, anchor.frame, anchor.label = anchor.frame, None, None
        if frame is None:
            return
        if frame in self._comment_frames:
            self._comment_frames.remove(frame)
        if frame.winfo_exists():
            frame.destroy()

    def _embed_comment_frame(self, line: int, anchor: '_ResolvedAnchor') -> None:
        """Put the anchor's comment frame on an existing empty line."""
        frame = self._build_comment_frame(line - 1, anchor)
        self._diff.window_create(f'{line}.0', window=frame)
        self._diff.tag_add('comment', f'{line}.0', f'{line}.end')
        self._minimap_lines[line - 1] = ('comment', self._comment_block_for(anchor))
        self._mm_relayout()  # the block may have a different height now
        self._render_overview()  # a fresh comment gets its mark here, not via _after_line_edit
        self.root.after_idle(self._render_gutter)  # the row grew; no yscroll fires

    def _render_comment_frame(self, src_line_no: int, anchor: '_ResolvedAnchor') -> None:
        """Append the comment for the line just emitted (skeleton render)."""
        frame = self._build_comment_frame(src_line_no, anchor)
        self._flush_buf()  # embedded windows bypass the text buffer
        self._diff.window_create('end', window=frame)
        self._emit('\n')
        self._flush_buf()
        cmt_line_no = src_line_no + 1
        self._diff.tag_add('comment', f'{cmt_line_no}.0', f'{cmt_line_no}.end')
        self._minimap_lines.append(('comment', self._comment_block_for(anchor)))

    def _remove_comment(self, anchor: '_ResolvedAnchor') -> None:
        """Take a rendered comment out of the widget without re-rendering.

        An orphan placeholder line goes with its comment; a real source line
        stays.
        """
        src = anchor.src_line
        if src is None:
            return
        self._discard_frame(anchor)
        orphan = 'orphan_src' in self._diff.tag_names(f'{src}.0')
        first, count = (src, 2) if orphan else (src + 1, 1)
        self._line_to_anchor.pop(src, None)
        if orphan:
            self._line_post_image.pop(src, None)
        anchors = self._pending_anchors.get(anchor.file, [])
        if anchor in anchors:
            anchors.remove(anchor)
        anchor.src_line = None
        self._delete_lines(first, count)
        self._after_line_edit()

    def _insert_orphan_comments_for_file(self, file_path: str) -> None:
        anchors = self._pending_anchors.get(file_path) or []
        for a in anchors:
            if a.matched:
                continue
            self._emit(a.line_text + '\n', 'orphan_src')
            self._minimap_lines.append(('orphan', a.line_text))
            src_line_no = self._cur_line
            a.matched = True
            a.moved = True
            a.src_line = src_line_no
            self._line_to_anchor[src_line_no] = a
            self._line_post_image[src_line_no] = (
                file_path, a.snap_line_no, a.side, a.line_text,
            )
            self._render_comment_frame(src_line_no, a)

    def _delete_comment(self, anchor: '_ResolvedAnchor') -> None:
        self._review.delete(anchor.file, anchor.snapshot,
                            anchor.snap_line_no, anchor.side)
        self._remove_comment(anchor)
        self._update_comments_section()

    def _scroll_diff_to_line(self, line_no: int) -> None:
        self._stop_scroll_animation()
        self._move_view(f'{line_no}.0')

    def _on_scrollbar_move(self, *args: str) -> None:
        self._move_view(*args)

    def _rerender_preserving_scroll(self) -> None:
        top_line = int(self._diff.index('@0,0').split('.')[0])
        self._render_diff_panel(restore_line=top_line)

    def _cancel_hide_schedule(self) -> None:
        if self._hide_after_id:
            self.root.after_cancel(self._hide_after_id)
            self._hide_after_id = None

    def _schedule_hide(self) -> None:
        self._cancel_hide_schedule()
        self._hide_after_id = self.root.after(CFG.hover_hide_delay_ms, self._do_hide_hover)

    def _on_btn_enter(self) -> None:
        if self._btn_leave_after_id:
            self.root.after_cancel(self._btn_leave_after_id)
            self._btn_leave_after_id = None
        self._over_hover_btn = True
        self._cancel_hide_schedule()

    def _on_btn_leave(self) -> None:
        if self._btn_leave_after_id:
            self.root.after_cancel(self._btn_leave_after_id)
        self._btn_leave_after_id = self.root.after(CFG.hover_btn_leave_delay_ms, self._finalize_btn_leave)

    def _finalize_btn_leave(self) -> None:
        self._btn_leave_after_id = None
        under = self._widget_under_pointer()
        if under in (self._comment_hover_btn, self._copy_hover_btn):
            return
        self._over_hover_btn = False
        # Unmapping the buttons for a scroll also fires Leave. A hide scheduled
        # from here could then land after the post-scroll re-hover and wipe it,
        # so a pointer still over the diff re-hovers instead of hiding. With an
        # editor open the hover handler does nothing, so hide explicitly.
        if self._active_comment_frame or not self._rehover_under_pointer(under):
            self._schedule_hide()

    def _on_root_focus_out(self, event: tk.Event) -> None:
        # FocusOut fires for child widgets too; only act when the toplevel
        # itself loses focus (another app/window taking it).
        if event.widget is self.root:
            self._has_focus = False
            self._do_hide_hover(force=True)

    def _on_root_focus_in(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._has_focus = True

    def _sync_hover_sel(self) -> None:
        """Tag the overlap of the hovered row with the selection."""
        d = self._diff
        if self._hover_row_range is None:
            return
        start, end = self._hover_row_range
        d.tag_remove('hover_sel', start, end)
        ranges = d.tag_ranges('sel')
        for s, e in zip(ranges[::2], ranges[1::2]):
            if d.compare(e, '<=', start) or d.compare(s, '>=', end):
                continue
            d.tag_add('hover_sel',
                      s if d.compare(s, '>', start) else start,
                      e if d.compare(e, '<', end) else end)

    def _clear_hover_tags(self) -> None:
        if self._hover_row_range is not None:
            self._diff.tag_remove('hover_sel', *self._hover_row_range)
            self._diff.tag_remove('hover', *self._hover_row_range)
            self._hover_row_range = None
        if self._hover_range is not None:
            self._diff.tag_remove('hover_line', *self._hover_range)
            self._hover_range = None

    def _do_hide_hover(self, force: bool = False) -> None:
        # Cancel rather than just forget a pending scheduled hide: a live timer
        # would otherwise fire later and wipe a ruler placed in the meantime.
        self._cancel_hide_schedule()
        if not force and self._widget_under_pointer() in (self._comment_hover_btn, self._copy_hover_btn):
            return
        self._clear_hover_tags()
        self._hover_row = None
        self._hover_line = -1
        self._comment_hover_btn.place_forget()
        self._copy_hover_btn.place_forget()
        self._hover_btn_line = -1
        self._over_hover_btn = False

    def _on_diff_hover(self, event: tk.Event) -> None:
        if not self._has_focus:
            return
        if self._active_comment_frame or self._over_hover_btn:
            return
        # Mapping a button over a view that is laid out but not yet painted
        # makes the next pixel copy smear it; _after_scroll_settled re-hovers.
        if self._scroll_animating:
            return
        self._cancel_hide_schedule()
        idx = self._diff.index(f'@{event.x},{event.y}')
        disp_start = self._diff.index(f'{idx} display linestart')
        if disp_start == self._hover_row:
            return
        line_no = int(idx.split('.')[0])
        tags = set(self._diff.tag_names(f'{line_no}.0'))
        if not tags & {'added', 'removed', 'context'}:
            self._do_hide_hover()
            return
        self._clear_hover_tags()
        # Two highlights: 'hover' is the strong ruler on the single display row
        # under the cursor; 'hover_line' is a lighter wash over the whole logical
        # line so you can see what copy/comment will act on (both operate on the
        # whole line, not the row). For unwrapped lines and the last wrap row we
        # extend past the newline so Tk fills the bg to the right edge; other
        # wrap rows stop at display lineend.
        disp_end   = self._diff.index(f'{idx} display lineend')
        line_end   = self._diff.index(f'{disp_start} lineend')
        if self._diff.compare(disp_end, '>=', line_end):
            row_end = self._diff.index(f'{line_end}+1c')
        else:
            row_end = disp_end
        line_start    = f'{line_no}.0'
        line_full_end = self._diff.index(f'{line_end}+1c')
        self._hover_row       = disp_start
        self._hover_row_range = (disp_start, row_end)
        self._hover_range     = (line_start, line_full_end)
        self._hover_line      = line_no
        self._diff.tag_add('hover_line', line_start, line_full_end)
        self._diff.tag_add('hover', disp_start, row_end)
        self._sync_hover_sel()
        info = self._diff.dlineinfo(disp_start)
        if info:
            _, y, _, h, _ = info
            # Inset the button so it can never exceed the ruler vertically,
            # regardless of font metrics / theme quirks. Centered in the row.
            inset  = 2
            btn_h  = max(1, h - 2 * inset)
            btn_y  = y + inset
            comment_w = self._comment_hover_btn.winfo_reqwidth()
            copy_w    = self._copy_hover_btn.winfo_reqwidth()
            comment_x = self._diff.winfo_width() - comment_w - 4
            copy_x    = comment_x - copy_w - 6
            if copy_x > 0:
                self._comment_hover_btn.place(x=comment_x, y=btn_y, height=btn_h)
                self._copy_hover_btn.place(x=copy_x, y=btn_y, height=btn_h)
                self._hover_btn_line = line_no
            else:
                self._comment_hover_btn.place_forget()
                self._copy_hover_btn.place_forget()
                self._hover_btn_line = -1
        else:
            self._comment_hover_btn.place_forget()
            self._copy_hover_btn.place_forget()
            self._hover_btn_line = -1

    def _on_comment_btn_click(self) -> None:
        line_no = self._hover_btn_line
        self._do_hide_hover()
        if line_no > 0:
            self._open_comment_editor(line_no)

    def _on_copy_btn_click(self) -> None:
        line_no = self._hover_btn_line
        self._do_hide_hover()
        if line_no > 0:
            self._copy_loc_and_lines(line_no)

    def _on_comment_click(self, event: tk.Event) -> str | None:
        if self._active_comment_frame:
            return None
        line_no = int(self._diff.index(f'@{event.x},{event.y}').split('.')[0])
        self._do_hide_hover()
        self._open_comment_editor(line_no)
        return 'break'

    def _open_comment_editor(self, line_no: int) -> None:
        if self._active_comment_frame:
            self._cancel_comment_edit()
            return
        if 'comment' in self._diff.tag_names(f'{line_no}.0'):
            line_no -= 1
            if line_no < 1:
                return
        raw_line = self._diff.get(f'{line_no}.0', f'{line_no}.end')
        if not raw_line:
            return
        post = self._line_post_image.get(line_no)
        if post is None:
            return
        file, new_line_no, side, line_text = post
        anchor = self._line_to_anchor.get(line_no)
        existing = anchor.comment if anchor else ''
        # The ruler must not stay up next to the editor; the button click's
        # own hide is not forced and yields while the pointer is on the button.
        self._do_hide_hover(force=True)
        self._comment_target = _CommentEditTarget(
            file=file, new_line_no=new_line_no, side=side, line_text=line_text,
            existing_snapshot     = anchor.snapshot     if anchor else None,
            existing_snap_line_no = anchor.snap_line_no if anchor else None,
        )
        bar_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        frame = tk.Frame(self._diff, bg=C['topbar_bg'])
        prefix = tk.Label(frame, text='  >> ', bg=C['topbar_bg'], fg=C['comment_fg'],
                          font=bar_font)
        prefix.pack(side='left', padx=(4, 0), pady=2, anchor='nw')
        self._bind_wheel(frame)
        self._bind_wheel(prefix)
        line_count = max(1, existing.count('\n') + 1)
        entry = tk.Text(frame, bg=C['bg'], fg=C['comment_fg'],
                        insertbackground=C['comment_fg'],
                        relief='flat', bd=0, height=line_count,
                        wrap='word', undo=True,
                        font=(CFG.font_family, CFG.font_size))
        entry.pack(side='left', fill='both', expand=True, padx=(0, 4), pady=2)
        if existing:
            entry.insert('1.0', existing)
            entry.tag_add('sel', '1.0', 'end-1c')
        def _newline(e: tk.Event) -> str:
            entry.insert('insert', '\n')
            self._resize_editor_frame(frame, prefix, entry)
            return 'break'
        def _confirm(e: tk.Event) -> str:
            self._confirm_comment_edit()
            return 'break'
        entry.bind('<Return>',         _confirm)
        entry.bind('<KP_Enter>',       _confirm)
        entry.bind('<Shift-Return>',   _newline)
        entry.bind('<Shift-KP_Enter>', _newline)
        entry.bind('<Alt-Return>',     _newline)
        entry.bind('<Alt-KP_Enter>',   _newline)
        entry.bind('<Escape>',         lambda e: self._cancel_comment_edit() or 'break')
        entry.bind('<FocusOut>',       lambda e: self.root.after(CFG.edit_focus_out_delay_ms, self._cancel_if_still_active))
        def _on_modified(e: tk.Event) -> None:
            if entry.edit_modified():
                entry.edit_modified(False)
                self._resize_editor_frame(frame, prefix, entry)
        entry.bind('<<Modified>>', _on_modified)
        self._active_comment_frame = frame
        self._active_comment_entry = entry
        if 'comment' in self._diff.tag_names(f'{line_no + 1}.0'):
            # Editing: the editor takes over the comment's line.  Tk destroys
            # the frame with its character, so it is rebuilt afterwards.
            self._editor_is_new = False
            self._diff.delete(f'{line_no + 1}.0', f'{line_no + 1}.end')
            if anchor is not None:
                self._discard_frame(anchor)
        else:
            self._editor_is_new = True
            self._insert_blank_line(line_no + 1)
            self._after_line_edit()
        self._editor_line = line_no + 1  # after the insert: _shift_lines moves it too
        self._diff.window_create(f'{line_no + 1}.0', window=frame)
        self._resize_editor_frame(frame, prefix, entry)
        entry.focus_set()

    def _resize_editor_frame(self, frame: tk.Frame, prefix: tk.Label, entry: tk.Text) -> None:
        line_count = max(1, int(entry.index('end-1c').split('.')[0]))
        entry.configure(height=line_count)
        if self._editor_line is not None:
            # Draw the draft in the minimap as it is typed. A changed row
            # count needs the (debounced) row table rebuild; otherwise only
            # the block's own rows are re-rasterised.
            idx = self._editor_line - 1
            old = self._minimap_lines[idx]
            mm_entry = ('comment', self._format_comment_block(
                entry.get('1.0', 'end-1c'), False, self._comment_wrap_cols()))
            if old != mm_entry:
                self._minimap_lines[idx] = mm_entry
                if old[0] != 'comment' or old[1].count('\n') != mm_entry[1].count('\n'):
                    self._schedule_mm_relayout()
                    self._schedule_overview_refresh()
                else:
                    r0 = self._mm_row_of(self._editor_line, 0)
                    for r in range(r0, r0 + mm_entry[1].count('\n') + 1):
                        self._mm_rows.pop(r, None)
                    self._render_minimap()
        frame.update_idletasks()
        h = max(prefix.winfo_reqheight(), entry.winfo_reqheight()) + 4
        w = max(self._diff.winfo_width() - self._diff_row_pad(), 1)
        frame.configure(width=w, height=h)
        frame.pack_propagate(False)
        self.root.after_idle(self._render_gutter)

    def _cancel_if_still_active(self) -> None:
        if self._active_comment_frame:
            text = self._active_comment_entry.get('1.0', 'end-1c') if self._active_comment_entry else ''
            if text.strip():
                self._confirm_comment_edit()
            else:
                self._cancel_comment_edit()

    def _close_editor(self) -> 'tuple[int, _CommentEditTarget | None, str]':
        """Tear the editor widget down; returns (editor line, target, text)."""
        text = self._active_comment_entry.get('1.0', 'end-1c') if self._active_comment_entry else ''
        if self._active_comment_frame:
            self._active_comment_frame.destroy()
            self._active_comment_frame = None
        self._active_comment_entry = None
        target, self._comment_target = self._comment_target, None
        line, self._editor_line = self._editor_line, None
        return line or 0, target, text.strip()

    def _cancel_comment_edit(self) -> None:
        line, _target, _text = self._close_editor()
        if line:
            if self._editor_is_new:
                self._delete_lines(line, 1)
                self._after_line_edit()
            else:
                anchor = self._line_to_anchor.get(line - 1)
                if anchor is not None:
                    self._embed_comment_frame(line, anchor)
        self._diff.focus_set()

    def _confirm_comment_edit(self) -> None:
        if not self._active_comment_entry or not self._comment_target:
            return
        line, target, comment = self._close_editor()
        src_line = line - 1
        anchor = self._line_to_anchor.get(src_line)
        if target.existing_snapshot is not None and target.existing_snap_line_no is not None:
            if comment:
                self._review.add(target.file, target.existing_snapshot,
                                 target.existing_snap_line_no,
                                 target.side, target.line_text, comment)
                if anchor is not None:
                    anchor.comment = comment
                    self._embed_comment_frame(line, anchor)
            else:
                self._review.delete(target.file, target.existing_snapshot,
                                    target.existing_snap_line_no, target.side)
                if anchor is not None:
                    self._remove_comment(anchor)
        elif comment:
            snap_sha = self._session_snapshots.get(target.file)
            if not snap_sha:
                content = self._read_current_file(target.file)
                if content is not None:
                    snap_sha = self._review.write_snapshot(content)
                    self._session_snapshots[target.file] = snap_sha
            if snap_sha:
                self._review.add(target.file, snap_sha, target.new_line_no,
                                 target.side, target.line_text, comment)
                anchor = _ResolvedAnchor(
                    file=target.file, snapshot=snap_sha, snap_line_no=target.new_line_no,
                    target_line_no=target.new_line_no, side=target.side,
                    line_text=target.line_text, comment=comment,
                    matched=True, src_line=src_line,
                )
                self._pending_anchors.setdefault(target.file, []).append(anchor)
                self._line_to_anchor[src_line] = anchor
                self._embed_comment_frame(line, anchor)
            else:
                self._delete_lines(line, 1)
                self._after_line_edit()
        elif self._editor_is_new:
            self._delete_lines(line, 1)
            self._after_line_edit()
        self._update_comments_section()
        self._diff.focus_set()

    @staticmethod
    def _row_from_event(widget: tk.Text, event: tk.Event) -> int:
        return int(widget.index(f'@{event.x},{event.y}').split('.')[0]) - 1

    @staticmethod
    def _bind_list_mouse_events(widget: tk.Text, on_click) -> None:
        widget.bind('<Button-1>',         on_click)
        widget.bind('<Double-Button-1>',  lambda e: 'break')
        widget.bind('<Triple-Button-1>',  lambda e: 'break')
        widget.bind('<B1-Motion>',        lambda e: 'break')

    def _iter_all_comments(self) -> 'Iterator[tuple[int | None, str, str, str, bool, bool]]':
        """Yield (src_line, loc, src_text, comment, is_orphan, moved) for every
        stored comment. ``is_orphan`` covers both files-not-in-diff and lines
        rendered via the orphan placeholder."""
        for file, anchors in self._pending_anchors.items():
            for a in anchors:
                src_line = a.src_line
                is_orphan = (src_line is None
                             or 'orphan_src' in self._diff.tag_names(f'{src_line}.0'))
                if src_line is None:
                    loc = file
                elif is_orphan:
                    loc = f'{file} (orphaned)'
                else:
                    loc = self._loc_for_line(src_line) or file
                yield src_line, loc, a.line_text, a.comment, is_orphan, a.moved

    def _rebuild_review_menu(self) -> None:
        m = self._review_menu
        m.delete(0, 'end')
        m.add_command(label='Dump to terminal', command=self._dump_to_terminal)
        m.add_command(label='Clear all', command=self._clear_all_comments,
                      state='disabled' if self._review.is_empty() else 'normal')
        items = list(self._iter_all_comments())
        if items:
            m.add_separator()
            for src_line, loc, _src_text, cmt, _is_orphan, moved in items:
                first_line = (cmt.splitlines() or [cmt])[0]
                marker = '~ ' if moved else ''
                label = f'{marker}{loc} - {first_line[:CFG.menu_label_max_len]}'
                m.add_command(label=label,
                              state='disabled' if src_line is None else 'normal',
                              command=lambda ln=src_line: self._jump_to_diff_line(ln) if ln else None)

    def _clear_all_comments(self) -> None:
        if self._review.is_empty():
            return
        self._review.clear()
        self._rerender_preserving_scroll()

    def _jump_to_diff_line(self, line_no: int) -> None:
        self._manual_scroll = False
        self._scroll_diff_to_line(line_no)

    def _close_app(self) -> None:
        if self._loaded and not self._review.is_empty():
            try:
                self._dump_to_terminal()
            except OSError:
                pass  # stdout's reader is gone (e.g. piped into head); still close
        if self._loaded:  # closed before the diff was loaded: keep the saved position
            try:
                top_line = int(self._diff.index('@0,0').split('.')[0])
                _save_window_state(self.root.winfo_geometry(), self._sash_ratio, top_line)
            except tk.TclError:
                pass
        self.root.destroy()

    def _show_commit(self, sha: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(sha)
        try:
            out = subprocess.check_output(
                ['git', 'show', '--stat', '--no-color', sha],
                text=True, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f'gitr: git show {sha} failed: {e}')
            return
        print(out)

    @staticmethod
    def _section_arrow(expanded: bool) -> str:
        return CFG.section_expanded_arrow if expanded else CFG.section_collapsed_arrow

    def _toggle_pane(self, expanded_attr: str, pane: tk.Frame, toggle_btn: tk.Button,
                     title: str, count: int, anchor: tk.Widget) -> None:
        expanded = getattr(self, expanded_attr)
        if expanded:
            pane.pack_forget()
        else:
            pane.pack(fill='x', before=anchor)
        new_state = not expanded
        setattr(self, expanded_attr, new_state)
        toggle_btn.configure(text=f'{self._section_arrow(new_state)} {title} ({count})')

    def _has_commits_section(self) -> bool:
        return bool(self._commits or self._has_staged or self._has_unstaged)

    def _commits_count(self) -> int:
        return len(self._commits) + (1 if self._has_staged else 0) + (1 if self._has_unstaged else 0)

    def _toggle_commits_pane(self) -> None:
        if not self._has_commits_section():
            return
        self._toggle_pane('_commits_expanded', self._commits_pane, self._commits_toggle,
                          'Commits', self._commits_count(), self._flist_bar)

    def _update_commits_section(self) -> None:
        if not self._has_commits_section():
            self._commits_pane.pack_forget()
            self._commits_header.pack_forget()
            self._commits_expanded = False
            return
        self._commits_header.pack_forget()
        self._commits_header.pack(fill='x', before=self._flist_bar)
        n = self._commits_count()
        self._commits_toggle.configure(
            text=f'{self._section_arrow(self._commits_expanded)} Commits ({n})')
        self._clist.configure(height=min(n + 1, CFG.list_pane_max_lines))
        self._render_clist()
        if self._commits_expanded:
            self._commits_pane.pack_forget()
            self._commits_pane.pack(fill='x', before=self._flist_bar)

    def _comments_anchor(self) -> tk.Widget:
        return self._commits_header if self._has_commits_section() else self._flist_bar

    def _toggle_comments_pane(self) -> None:
        n = len(list(self._iter_all_comments()))
        self._toggle_pane('_comments_expanded', self._comments_pane, self._comments_toggle,
                          'Comments', n, self._comments_anchor())

    def _update_comments_section(self) -> None:
        items = list(self._iter_all_comments())
        n = len(items)
        anchor = self._comments_anchor()
        if n == 0:
            self._comments_pane.pack_forget()
            self._comments_header.pack_forget()
            self._comments_expanded = False
            return
        self._comments_header.pack_forget()
        self._comments_header.pack(fill='x', before=anchor)
        self._comments_toggle.configure(
            text=f'{self._section_arrow(self._comments_expanded)} Comments ({n})')
        self._render_cmt_list(items)
        total_rows = len(self._cmt_list_actions)
        self._cmt_list.configure(height=min(total_rows + 1, CFG.list_pane_max_lines))
        if self._comments_expanded:
            self._comments_pane.pack_forget()
            self._comments_pane.pack(fill='x', before=anchor)

    def _render_cmt_list(self, items: list) -> None:
        self._cmt_list.tag_configure('loc',      foreground=C['fileheader_fg'])
        self._cmt_list.tag_configure('cmt',      foreground=C['comment_fg'])
        self._cmt_list.tag_configure('orphan',   foreground=C['subdued'])
        self._cmt_list.tag_configure('moved',    foreground=C['comment_fg'])
        self._cmt_list.configure(state='normal')
        self._cmt_list.delete('1.0', 'end')
        # One entry per logical line so _row_from_event maps clicks (including
        # clicks on wrapped continuations of the same line) back to a source
        # line. Continuation lines of a multi-line comment repeat the same
        # source line so clicking any of them still jumps correctly.
        self._cmt_list_actions: list = []
        for src_line, loc, _src_text, cmt, is_orphan, moved in items:
            lines = cmt.splitlines() or ['']
            self._cmt_list.insert('end', '~ ' if moved and not is_orphan else '  ', 'moved')
            self._cmt_list.insert('end', loc, 'orphan' if is_orphan else 'loc')
            self._cmt_list.insert('end', '  ' + lines[0] + '\n', 'cmt')
            self._cmt_list_actions.append(src_line)
            for cont in lines[1:]:
                self._cmt_list.insert('end', '    ' + cont + '\n', 'cmt')
                self._cmt_list_actions.append(src_line)
        self._cmt_list.configure(state='disabled')
        self._bind_list_mouse_events(self._cmt_list, self._on_cmt_list_click)

    def _on_cmt_list_click(self, event: tk.Event) -> str:
        row = self._row_from_event(self._cmt_list, event)
        if 0 <= row < len(self._cmt_list_actions):
            line = self._cmt_list_actions[row]
            if line is not None:
                self._jump_to_diff_line(line)
        return 'break'

    def _render_clist(self) -> None:
        self._clist.tag_configure('sha',      foreground=C['fileheader_fg'])
        self._clist.tag_configure('subject',  foreground=C['fg'])
        self._clist.tag_configure('marker',   foreground=C['comment_fg'])
        self._clist.tag_configure('selected', background=C['selected_bg'])
        self._clist.configure(state='normal')
        self._clist.delete('1.0', 'end')
        self._clist_actions: list = []
        if self._has_unstaged:
            self._clist.insert('end', '* unstaged changes\n', 'marker')
            self._clist_actions.append(('unstaged',))
        if self._has_staged:
            self._clist.insert('end', '* staged changes\n', 'marker')
            self._clist_actions.append(('staged',))
        if (self._has_unstaged or self._has_staged) and self._commits:
            self._clist.insert('end', '\n')
            self._clist_actions.append(None)
        for sha, subject in self._commits:
            self._clist.insert('end', sha, 'sha')
            self._clist.insert('end', '  ' + subject + '\n', 'subject')
            self._clist_actions.append(('commit', sha))
        self._clist.configure(state='disabled')
        self._bind_list_mouse_events(self._clist, self._on_clist_click)

    def _on_clist_click(self, event: tk.Event) -> str:
        row = self._row_from_event(self._clist, event)
        if 0 <= row < len(self._clist_actions):
            action = self._clist_actions[row]
            if action and action[0] == 'commit':
                self._show_commit(action[1])
            elif action and action[0] == 'staged':
                self._show_staged_or_unstaged(['git', 'diff', '--cached', '--no-color'])
            elif action and action[0] == 'unstaged':
                self._show_staged_or_unstaged(['git', 'diff', '--no-color'])
        return 'break'

    def _show_staged_or_unstaged(self, cmd: list[str]) -> None:
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f'gitr: {" ".join(cmd)} failed: {e}')
            return
        print(out)

    def _dump_to_terminal(self) -> None:
        if self._review.is_empty():
            print('gitr: no review comments')
            return
        for _src_line, loc, src_text, cmt, _is_orphan, moved in self._iter_all_comments():
            print(f'{loc}\n{src_text}\n{self._format_comment_block(cmt, moved)}\n')

    def _jump_to(self, path: str) -> None:
        self._manual_scroll = False
        pos = self._positions.get(path)
        if not pos:
            return
        df = next((d for d in self._diff_files if d.path == path), None)
        header_lines = 2 if (df and df.index) else 1
        self._scroll_diff_to_line(int(pos.split('.')[0]) + header_lines)


# --entry point ------------------------------------------------------------


def _install_interrupt(root: tk.Tk, app: 'App') -> None:
    """Make Ctrl+C in the terminal close the app the way Ctrl+W does.

    Python's default handler does not work under Tk: the event wait blocks
    in C, so the signal is only noticed on the next event, and when that
    event runs a Python callback the KeyboardInterrupt raised inside it is
    caught and printed by tkinter while the loop carries on. A timer wakes
    the loop regularly, and the handler schedules the normal close instead
    of raising.
    """
    closing = False

    def on_sigint(signum: int, frame: object) -> None:
        nonlocal closing
        # Mid-render there is nothing to save and the render cannot be cut
        # short from inside its callback; a second Ctrl+C means the close
        # is stuck (tkinter swallows exceptions raised in callbacks).
        if app._rendering or closing:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(130)
        closing = True
        app._interrupted = True
        try:
            root.after(0, app._close_app)
        except tk.TclError:
            os._exit(130)

    signal.signal(signal.SIGINT, on_sigint)

    def heartbeat() -> None:
        root.after(CFG.sigint_poll_ms, heartbeat)
    heartbeat()


def main() -> None:
    parser = argparse.ArgumentParser(prog='gitr', description=USAGE,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--merge-base', action='store_true', dest='merge_base')
    parser.add_argument('-p', '--patch', metavar='FILE', default=None)
    parser.add_argument('refs', nargs='*')
    args = parser.parse_args()

    if args.merge_base and not args.refs:
        sys.exit('gitr: --merge-base requires a ref (e.g. gitr --merge-base master)')

    source: PatchSource | GitSource

    if args.patch is not None:
        try:
            text = sys.stdin.read() if args.patch == '-' else Path(args.patch).read_text()
        except OSError as e:
            sys.exit(f'gitr: {e}')
        label = '' if args.patch == '-' else args.patch
        source = PatchSource(text, label=label)
    elif args.refs == ['-']:
        source = PatchSource(sys.stdin.read())
    elif args.refs or args.merge_base:
        source = GitSource(args.refs, merge_base=args.merge_base)
    elif not sys.stdin.isatty():
        source = PatchSource(sys.stdin.read())
    else:
        source = GitSource([])

    diff_text = source.diff_text()
    if not diff_text.strip():
        print('gitr: no changes')
        sys.exit(0)

    root = tk.Tk()
    cwd = Path(os.getcwd())
    try:
        cwd_label = '~/' + cwd.relative_to(Path.home()).as_posix()
    except ValueError:
        cwd_label = cwd.as_posix()
    title_parts = ['gitr', cwd_label]
    src_label = source.label()
    if src_label:
        title_parts.append(src_label)
    root.title(' | '.join(title_parts))
    app = App(root, diff_text,
              commits=source.commits(),
              has_staged=source.has_staged(),
              has_unstaged=source.has_unstaged(),
              source=source)
    _install_interrupt(root, app)
    root.mainloop()
    if app._interrupted:
        sys.exit(130)


if __name__ == '__main__':
    main()
