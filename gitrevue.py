#!/usr/bin/env python3
"""gitrevue - lightweight Git diff viewer"""

import re
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass, field
from typing import Optional


USAGE = """\
usage: <git-command> | gitrevue

Examples:
  git diff main...HEAD | gitrevue          # branch diff vs main
  git diff HEAD | gitrevue                 # staged + unstaged vs last commit
  git diff --cached | gitrevue             # staged only
  git diff HEAD~5 | gitrevue              # last 5 commits
  git show HEAD | gitrevue                 # single commit
  git diff --first-parent main...HEAD | gitrevue

  GITREVUE_SCALE=2 git diff HEAD | gitrevue   # scale UI up (HiDPI)
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


@dataclass
class DiffFile:
    path: str
    lines: list[DiffLine] = field(default_factory=list)


# --git helpers ------------------------------------------------------------

def try_current_branch() -> str:
    r = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


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


def parse_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: Optional[DiffFile] = None

    for raw in text.splitlines():
        if raw.startswith('diff --git '):
            if current is not None:
                files.append(current)
            b_idx = raw.rfind(' b/')
            path = raw[b_idx + 3:] if b_idx != -1 else 'unknown'
            current = DiffFile(path)
        if current is not None:
            current.lines.append(DiffLine(raw, _classify(raw)))

    if current is not None:
        files.append(current)
    return files


def entries_from_diff(diff_files: list[DiffFile]) -> list[FileEntry]:
    result = []
    for df in diff_files:
        add = sum(1 for l in df.lines if l.kind == 'added')
        rem = sum(1 for l in df.lines if l.kind == 'removed')
        # derive status from diff metadata lines
        status = 'M'
        for l in df.lines:
            if l.kind == 'fileheader':
                if l.text.startswith('new file'):
                    status = 'A'
                elif l.text.startswith('deleted file'):
                    status = 'D'
                elif l.text.startswith('rename '):
                    status = 'R'
        result.append(FileEntry(df.path, status, add, rem))
    return result


# --config -------------------------------------------------------------------

class CFG:
    font_family   = 'monospace'
    font_size     = 12
    window_scale  = 0.75    # fraction of screen size on startup
    sash_ratio    = 0.70
    scrollbar_w   = 28


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
}



# --application ------------------------------------------------------------

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


class App:
    def __init__(self, root: tk.Tk, diff_text: str) -> None:
        self.root = root
        self.diff_text = diff_text
        self._entries: list[FileEntry] = []
        self._positions: dict[str, str] = {}

        self._build_ui()
        self._load()

    # --UI ------------------------------------------------------------

    def _make_scrollbar(self, parent: tk.Widget, **kw) -> tk.Scrollbar:
        return tk.Scrollbar(parent,
                            bg=C['selected_bg'],
                            troughcolor=C['bg'],
                            activebackground=C['subdued'],
                            relief='flat', bd=0,
                            width=CFG.scrollbar_w,
                            **kw)

    def _build_ui(self) -> None:
        self.root.configure(bg=C['bg'])
        sw, sh = _primary_monitor_size()
        w, h = int(sw * CFG.window_scale), int(sh * CFG.window_scale)
        self.root.geometry(f'{w}x{h}')
        font = (CFG.font_family, CFG.font_size)

        # top bar
        bar = tk.Frame(self.root, bg=C['topbar_bg'], pady=5)
        bar.pack(fill='x')

        self._lbl_branch = tk.Label(bar, bg=C['topbar_bg'], fg=C['fg'], font=font)
        self._lbl_branch.pack(side='left', padx=10)

        self._lbl_stat = tk.Label(bar, bg=C['topbar_bg'], fg=C['subdued'], font=font)
        self._lbl_stat.pack(side='left')

        # two-panel split
        self._sash = tk.PanedWindow(self.root, orient='horizontal',
                                     bg=C['subdued'], sashwidth=3, sashrelief='flat')
        self._sash.pack(fill='both', expand=True)

        # left: diff (grid so the scrollbar corner square fits neatly)
        lf = tk.Frame(self._sash, bg=C['bg'])
        lf.grid_rowconfigure(0, weight=1)
        lf.grid_columnconfigure(0, weight=1)
        self._diff = tk.Text(lf, bg=C['bg'], fg=C['fg'],
                              font=font, wrap='none',
                              relief='flat', bd=0, state='disabled', cursor='arrow',
                              selectbackground=C['selected_bg'])
        vs = self._make_scrollbar(lf, orient='vertical',   command=self._diff.yview)
        hs = self._make_scrollbar(lf, orient='horizontal', command=self._diff.xview)
        self._diff.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self._diff.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        tk.Frame(lf, bg=C['topbar_bg'],
                 width=CFG.scrollbar_w, height=CFG.scrollbar_w).grid(row=1, column=1)

        # right: file list
        rf = tk.Frame(self._sash, bg=C['bg'])
        self._flist = tk.Text(rf, bg=C['bg'], fg=C['fg'],
                               font=font, wrap='none',
                               relief='flat', bd=0, state='disabled', cursor='arrow')
        fvs = self._make_scrollbar(rf, orient='vertical', command=self._flist.yview)
        self._flist.configure(yscrollcommand=fvs.set)
        fvs.pack(side='right', fill='y')
        self._flist.pack(fill='both', expand=True)

        self._sash.add(lf, stretch='always')
        self._sash.add(rf, stretch='never')
        self.root.after(50, self._init_sash)

        # diff tags
        self._diff.tag_configure('added',      foreground=C['added_fg'],      background=C['added_bg'])
        self._diff.tag_configure('removed',     foreground=C['removed_fg'],    background=C['removed_bg'])
        self._diff.tag_configure('hunk',        foreground=C['hunk_fg'])
        self._diff.tag_configure('fileheader',  foreground=C['fileheader_fg'])
        self._diff.tag_configure('context',     foreground=C['fg'])
        self._diff.tag_configure('subdued',     foreground=C['subdued'])

        # file list tags
        self._flist.tag_configure('status_A',  foreground=C['status_A'])
        self._flist.tag_configure('status_M',  foreground=C['status_M'])
        self._flist.tag_configure('status_D',  foreground=C['status_D'])
        self._flist.tag_configure('status_R',  foreground=C['status_R'])
        self._flist.tag_configure('stats',     foreground=C['subdued'])
        self._flist.tag_configure('selected',  background=C['selected_bg'])

        self._flist.bind('<Button-1>', self._on_file_click)

    def _init_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, int(w * CFG.sash_ratio), 0)
        else:
            self.root.after(50, self._init_sash)

    # --data ------------------------------------------------------------

    def _load(self) -> None:
        diff_files = parse_diff(self.diff_text)
        entries = entries_from_diff(diff_files)
        branch = try_current_branch()

        n = len(diff_files)
        add = sum(e.additions for e in entries)
        rem = sum(e.deletions for e in entries)
        stat = f'{n} file{"s" if n != 1 else ""} changed, +{add} -{rem}' if n else ''

        self._entries = entries
        self._render(branch, stat, diff_files, entries)

    def _render(self, branch: str, stat: str,
                diff_files: list[DiffFile], entries: list[FileEntry]) -> None:
        self._lbl_branch.configure(text=f'branch:  {branch}' if branch else '')
        self._lbl_stat.configure(text=f'  {stat}' if stat else '')

        # diff panel
        self._diff.configure(state='normal')
        self._diff.delete('1.0', 'end')
        self._positions.clear()

        if diff_files:
            for df in diff_files:
                self._positions[df.path] = self._diff.index('end-1c linestart')
                for dl in df.lines:
                    self._diff.insert('end', dl.text + '\n', dl.kind)
        else:
            self._diff.insert('end', 'Empty diff.\n', 'subdued')

        self._diff.configure(state='disabled')

        # file list panel
        self._flist.configure(state='normal')
        self._flist.delete('1.0', 'end')

        for e in entries:
            self._flist.insert('end', f' {e.status} ', f'status_{e.status}')
            self._flist.insert('end', f' {e.path}')
            parts: list[str] = []
            if e.additions:
                parts.append(f'+{e.additions}')
            if e.deletions:
                parts.append(f'-{e.deletions}')
            if parts:
                self._flist.insert('end', f'  {" ".join(parts)}', 'stats')
            self._flist.insert('end', '\n')

        self._flist.configure(state='disabled')

    # --interaction ----------------------------------------------------------

    def _on_file_click(self, event: tk.Event) -> None:
        idx = self._flist.index(f'@{event.x},{event.y}')
        row = int(idx.split('.')[0]) - 1
        if 0 <= row < len(self._entries):
            self._highlight_row(row + 1)
            self._jump_to(self._entries[row].path)

    def _highlight_row(self, row: int) -> None:
        self._flist.tag_remove('selected', '1.0', 'end')
        self._flist.tag_add('selected', f'{row}.0', f'{row}.end+1c')

    def _jump_to(self, path: str) -> None:
        pos = self._positions.get(path)
        if not pos:
            return
        line = int(pos.split('.')[0])
        total = int(self._diff.index('end').split('.')[0])
        if total > 1:
            self._diff.yview_moveto((line - 1) / total)


# --entry point ------------------------------------------------------------


def main() -> None:
    if sys.stdin.isatty():
        print(USAGE, end='')
        sys.exit(0)

    diff_text = sys.stdin.read()

    root = tk.Tk()
    root.title('gitrevue')
    root.bind('<Control-w>', lambda _: root.destroy())
    root.bind('<Control-q>', lambda _: root.destroy())
    App(root, diff_text)
    root.mainloop()


if __name__ == '__main__':
    main()
