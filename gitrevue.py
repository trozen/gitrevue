#!/usr/bin/env python3
"""gitrevue - lightweight Git diff viewer"""

import json
import re
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path.home() / '.config' / 'gitrevue' / 'config.json'


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
    status: str = 'M'
    old_path: str = ''
    index: str = ''


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
            dl = DiffLine(raw, _classify(raw))
            current.lines.append(dl)
            if dl.kind == 'fileheader':
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


# --config -------------------------------------------------------------------

class CFG:
    font_family   = 'monospace'
    font_size     = 12
    window_scale  = 0.75    # fraction of screen size on startup
    sash_ratio    = 0.70
    scrollbar_w   = 28
    minimap_w     = 120
    scroll_speed  = 8   # lines per mouse-wheel tick


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


# non-whitespace pixel colours in the minimap; None = leave as canvas bg
_MINIMAP_COLORS: dict[str, str | None] = {
    'added':   _blend(C['added_fg'],      0.45),
    'removed': _blend(C['removed_fg'],    0.45),
    'hunk':    _blend(C['hunk_fg'],       0.35),
    'filehdr': _blend(C['fileheader_fg'], 0.35),
    'fileidx': _blend(C['fileheader_fg'], 0.35),
    'context': _blend(C['fg'], 0.18),
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


_MM_LINE_H = 2  # natural minimap pixels per source line (matches VS Code behaviour)


class App:
    def __init__(self, root: tk.Tk, diff_text: str) -> None:
        self.root = root
        self.diff_text = diff_text
        self._entries: list[FileEntry] = []
        self._diff_files: list[DiffFile] = []
        self._positions: dict[str, str] = {}
        self._pos_order: list[tuple[int, str]] = []
        self._minimap_lines: list[tuple[str, str]] = []  # (kind, text)
        self._scroll_pos: tuple[float, float] = (0.0, 1.0)
        self._minimap_content_h: int = 0
        self._hunk_seps: list[tk.Canvas] = []
        self._scroll_target: float = 0.0
        self._scroll_animating: bool = False
        self._flist_selected_row: int = -1
        self._manual_scroll: bool = False
        self._wrap_var = tk.BooleanVar(value=self._load_config().get('wrap_lines', True))

        self._build_ui()
        self._load()

    # --UI ------------------------------------------------------------

    def _make_read_only(self, widget: tk.Text) -> None:
        # Ctrl+W conflicts: Text class binds it to "delete previous word".
        # Overriding it here is unavoidable; extract to one place so each
        # read-only widget needs only a single call.
        widget.bind('<Key>', lambda e: 'break')
        widget.bind('<Control-c>', lambda e: None)
        widget.bind('<Control-w>', lambda e: self.root.destroy())
        widget.bind('<Control-q>', lambda e: self.root.destroy())

    def _make_scrollbar(self, parent: tk.Widget, **kw) -> tk.Scrollbar:
        return tk.Scrollbar(parent,
                            bg=C['selected_bg'],
                            troughcolor=C['bg'],
                            activebackground=C['subdued'],
                            relief='flat', bd=0,
                            width=CFG.scrollbar_w,
                            **kw)

    def _build_ui(self) -> None:
        menu_kw = dict(bg=C['topbar_bg'], fg=C['fg'],
                       activebackground=C['selected_bg'], activeforeground=C['fg'],
                       relief='flat', bd=0)
        menubar = tk.Menu(self.root, **menu_kw)
        file_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        file_menu.add_command(label='Quit', accelerator='Ctrl+Q',
                              command=self.root.destroy)
        menubar.add_cascade(label='File', menu=file_menu)
        view_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        view_menu.add_checkbutton(label='Wrap long lines', variable=self._wrap_var,
                                  command=self._on_wrap_toggle)
        menubar.add_cascade(label='View', menu=view_menu)
        self.root.configure(bg=C['bg'], menu=menubar)
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
        lf.grid_rowconfigure(1, weight=1)
        lf.grid_columnconfigure(0, weight=1)

        self._sticky = tk.Label(lf, bg=C['topbar_bg'], fg=C['fg'],
                                 font=font, anchor='w', padx=10, pady=3, text='')
        self._sticky.grid(row=0, column=0, columnspan=3, sticky='ew')

        self._diff = tk.Text(lf, bg=C['bg'], fg=C['fg'],
                              font=font, wrap='char',
                              relief='flat', bd=0, cursor='arrow',
                              selectbackground=C['selected_bg'],
                              selectforeground=C['fg'])
        self._make_read_only(self._diff)
        self._diff.bind('<Configure>', self._on_diff_configure)
        self._diff.bind('<Button-4>',  lambda e: self._on_wheel(-1) or 'break')
        self._diff.bind('<Button-5>',  lambda e: self._on_wheel( 1) or 'break')
        self._diff.bind('<MouseWheel>', lambda e: self._on_wheel(-e.delta // 120) or 'break')
        self._diff_vs = self._make_scrollbar(lf, orient='vertical', command=self._diff.yview)
        self._diff_vs.bind('<ButtonPress-1>', lambda e: setattr(self, '_manual_scroll', True))
        hs = self._make_scrollbar(lf, orient='horizontal', command=self._diff.xview)
        self._diff.configure(yscrollcommand=self._on_diff_yscroll, xscrollcommand=hs.set)
        self._diff.grid(row=1, column=0, sticky='nsew')

        self._minimap = tk.Canvas(lf, width=CFG.minimap_w, bg=C['bg'],
                                   highlightthickness=0)
        self._minimap.grid(row=1, column=1, rowspan=2, sticky='ns')
        self._minimap.bind('<Configure>',  lambda e: self._render_minimap())
        self._minimap.bind('<Button-1>',   self._on_minimap_click)
        self._minimap.bind('<B1-Motion>',  self._on_minimap_click)

        self._diff_vs.grid(row=1, column=2, sticky='ns')
        hs.grid(row=2, column=0, sticky='ew')
        corner = tk.Frame(lf, bg=C['topbar_bg'], width=CFG.scrollbar_w, height=CFG.scrollbar_w)
        corner.grid(row=2, column=2)
        self._diff_hs = hs
        self._diff_hs_corner = corner
        # wrap on by default — horizontal scrollbar not needed
        hs.grid_remove()
        corner.grid_remove()

        # right: file list
        rf = tk.Frame(self._sash, bg=C['bg'])
        self._flist = tk.Text(rf, bg=C['bg'], fg=C['fg'],
                               font=font, wrap='none',
                               relief='flat', bd=0, state='disabled', cursor='arrow',
                               selectbackground=C['bg'], selectforeground=C['fg'],
                               inactiveselectbackground=C['bg'])
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
        self._diff.tag_configure('filehdr',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('fileidx',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('status_A',    foreground=C['status_A'])
        self._diff.tag_configure('status_M',    foreground=C['status_M'])
        self._diff.tag_configure('status_D',    foreground=C['status_D'])
        self._diff.tag_configure('status_R',    foreground=C['status_R'])

        # file list tags
        self._flist.tag_configure('status_A',  foreground=C['status_A'])
        self._flist.tag_configure('status_M',  foreground=C['status_M'])
        self._flist.tag_configure('status_D',  foreground=C['status_D'])
        self._flist.tag_configure('status_R',  foreground=C['status_R'])
        self._flist.tag_configure('stats',     foreground=C['subdued'])
        self._flist.tag_configure('selected',  background=C['selected_bg'])

        self._diff.tag_raise('sel')

        self._flist.bind('<Button-1>', self._on_file_click)
        self._flist.bind('<B1-Motion>', lambda e: 'break')
        self._flist.bind('<Double-Button-1>', lambda e: 'break')
        self._flist.bind('<Triple-Button-1>', lambda e: 'break')
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
        self._save_config({'wrap_lines': wrap})

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
            _CONFIG_PATH.write_text(json.dumps(data))
        except Exception:
            pass

    def _init_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, int(w * CFG.sash_ratio), 0)
            self.root.bind('<Configure>', self._on_window_configure)
        else:
            self.root.after(50, self._init_sash)

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self.root.after_idle(self._place_sash)

    def _place_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, int(w * CFG.sash_ratio), 0)

    # --smooth scroll ---------------------------------------------------------

    def _on_wheel(self, ticks: int) -> None:
        total = int(self._diff.index('end').split('.')[0])
        if total < 2:
            return
        self._manual_scroll = True
        first, last = self._diff.yview()
        max_pos = 1.0 - (last - first)  # true bottom: yview[0] never exceeds this
        step = (CFG.scroll_speed * ticks) / total
        self._scroll_target = max(0.0, min(max_pos, self._scroll_target + step))
        if not self._scroll_animating:
            self._scroll_animating = True
            self._animate_scroll()

    def _animate_scroll(self) -> None:
        current = self._diff.yview()[0]
        remaining = self._scroll_target - current
        if abs(remaining) < 0.0003:
            self._diff.yview_moveto(self._scroll_target)
            self._scroll_animating = False
            return
        self._diff.yview_moveto(current + remaining * 0.35)
        self.root.after(16, self._animate_scroll)

    # --hunk separators -------------------------------------------------------

    def _on_diff_configure(self, event: tk.Event) -> None:
        if event.width > 1:
            for sep in self._hunk_seps:
                sep.configure(width=event.width)

    def _update_hunk_sep_widths(self) -> None:
        w = self._diff.winfo_width()
        if w > 1:
            for sep in self._hunk_seps:
                sep.configure(width=w)
        else:
            self.root.after(50, self._update_hunk_sep_widths)

    # --minimap ---------------------------------------------------------------

    def _render_minimap(self) -> None:
        c = self._minimap
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        n = len(self._minimap_lines)
        if n == 0:
            c.delete('all')
            self._minimap_content_h = 0
            return

        bg = C['bg']
        blank = '{' + ' '.join([bg] * cw) + '}'

        # Only stretch when the diff is too tall to fit at natural scale.
        natural_h = n * _MM_LINE_H
        img_h = min(natural_h, ch)
        self._minimap_content_h = img_h

        # Cache rendered rows per source-line index to avoid redundant work
        # when multiple canvas rows map to the same source line.
        line_cache: dict[int, str] = {}

        def _row(i: int) -> str:
            if i in line_cache:
                return line_cache[i]
            kind, text = self._minimap_lines[i]
            color = _MINIMAP_COLORS.get(kind)
            if color is None:
                line_cache[i] = blank
                return blank
            pixels = [color if x < len(text) and text[x] not in (' ', '\t') else bg
                      for x in range(cw)]
            result = '{' + ' '.join(pixels) + '}'
            line_cache[i] = result
            return result

        rows = [_row(min(int(y * n / img_h), n - 1)) for y in range(img_h)]
        img = tk.PhotoImage(width=cw, height=img_h)
        img.put(' '.join(rows))

        c.delete('all')
        c.create_image(0, 0, anchor='nw', image=img)
        c._mm_img = img  # keep reference; PhotoImage is GC'd without it
        self._update_minimap_viewport()

    def _update_minimap_viewport(self) -> None:
        c = self._minimap
        if c.winfo_height() <= 1 or self._minimap_content_h <= 0:
            return
        c.delete('viewport')
        first, last = self._scroll_pos
        h = self._minimap_content_h
        y0, y1 = int(first * h), int(last * h)
        c.create_rectangle(0, y0, c.winfo_width(), y1,
                           fill=C['fg'], stipple='gray12',
                           outline=C['fg'], width=1, tags='viewport')

    def _on_minimap_click(self, event: tk.Event) -> None:
        h = self._minimap_content_h
        if h <= 0:
            return
        self._manual_scroll = True
        first, last = self._scroll_pos
        span = last - first
        frac = max(0.0, min(1.0 - span, event.y / h - span / 2))
        self._diff.yview_moveto(frac)

    @staticmethod
    def _file_label(df: DiffFile) -> tuple[str, str]:
        """Return (name_line, index_line) for both the sticky label and the diff header."""
        name = f'{df.old_path} -> {df.path}' if (df.status == 'R' and df.old_path) else df.path
        return name, df.index

    def _on_diff_yscroll(self, first: str, last: str) -> None:
        self._diff_vs.set(first, last)
        self._scroll_pos = (float(first), float(last))
        if not self._scroll_animating:
            self._scroll_target = float(first)
        self._update_sticky_header()
        self._update_minimap_viewport()

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
        row = next((i + 1 for i, e in enumerate(self._entries) if e.path == path), -1)
        if row > 0 and row != self._flist_selected_row:
            self._highlight_row(row)
            self._flist.see(f'{row}.0')

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

        self._diff_files = diff_files

        # diff panel
        for sep in self._hunk_seps:
            sep.destroy()
        self._hunk_seps.clear()
        self._diff.delete('1.0', 'end')
        self._positions.clear()
        self._minimap_lines = []

        if diff_files:
            for i, df in enumerate(diff_files):
                if i > 0:
                    self._diff.insert('end', '\n', 'context')
                    self._minimap_lines.append(('context', ''))
                self._positions[df.path] = self._diff.index('end-1c linestart')
                name, idx = self._file_label(df)
                self._diff.insert('end', f' {name}\n', 'filehdr')
                self._minimap_lines.append(('filehdr', f' {name}'))
                if idx:
                    self._diff.insert('end', f' {idx}\n', 'fileidx')
                    self._minimap_lines.append(('fileidx', f' {idx}'))
                for dl in df.lines:
                    if dl.kind != 'fileheader':
                        if dl.kind == 'hunk':
                            sep = tk.Canvas(self._diff, height=1, bg=C['subdued'],
                                            highlightthickness=0, bd=0, width=1)
                            self._diff.window_create('end', window=sep)
                            self._diff.insert('end', '\n')
                            self._hunk_seps.append(sep)
                        self._diff.insert('end', dl.text + '\n', dl.kind)
                        self._minimap_lines.append((dl.kind, dl.text))
        else:
            self._diff.insert('end', 'Empty diff.\n', 'subdued')

        self._pos_order = sorted(
            (int(pos.split('.')[0]), path)
            for path, pos in self._positions.items()
        )
        self.root.after_idle(self._update_sticky_header)
        self.root.after_idle(self._render_minimap)
        self.root.after_idle(self._update_hunk_sep_widths)

        # file list panel
        self._flist_selected_row = -1
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
        self._flist_selected_row = row
        self._flist.tag_remove('selected', '1.0', 'end')
        self._flist.tag_add('selected', f'{row}.0', f'{row}.end+1c')

    def _jump_to(self, path: str) -> None:
        self._manual_scroll = False
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
