#!/usr/bin/env python3
"""
Rotating Proxy — desktop control panel (Tkinter).

A light, card-based dashboard in the style of the commercial proxy
managers (GoLogin et al.): white panels on a light background, a left
navigation rail with the connection controls, blue primary actions and
colour-coded status — while every feature of the engine stays reachable:

  * start / stop / restart the local proxy
  * live stats: served, active, failures, pool health, uptime, traffic
  * pool table with per-upstream status, strength tier, latency, country,
    state, hits and last error — filterable by status and free text
  * exit cascade (Region → Country → State): where traffic leaves, and a
    one-time download of the state/city database behind the last level
  * add / remove / import / export upstream proxies
  * manual + scheduled health checks with a progress indicator
  * colour-coded log with level filtering
  * traffic sparkline
  * settings dialog, persisted between runs

Everything Tk-facing runs on the main thread; the engine reports through a
queue that the main loop drains on a timer, so worker threads never touch
the UI.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from appstate import state_file
from engine import (DEFAULTS, FAST_MS, RotatingProxy, USE_FLAGS,
                    parse_entry, region_of)
from proxylist import PROXY_LIST
import proxyctl

# ---------------------------------------------------------------------------
# palette — light, GoLogin-style: grey canvas, white cards, blue accent
# ---------------------------------------------------------------------------
BG        = "#f2f4f8"        # window canvas
PANEL     = "#ffffff"        # cards, table rows, dialogs
PANEL_2   = "#f7f8fa"        # toolbar strips, inputs
BORDER    = "#e3e7ee"
TEXT      = "#161d2b"        # primary text
MUTED     = "#697386"        # captions, secondary text
ACCENT    = "#2563eb"        # primary actions, selection
GREEN     = "#15803d"        # healthy / running
AMBER     = "#b45309"        # checking / starting
RED       = "#dc2626"        # stopped / dead
CYAN      = "#0284c7"        # traffic series

# soft backgrounds for status chips (text keeps the strong colour)
GREEN_TINT   = "#e7f6ec"
AMBER_TINT   = "#fdf2e2"
RED_TINT     = "#fdecec"

# next to the scripts in a checkout; the user's config directory in a
# packaged build, where the install location is read-only (see appstate.py)
STATE_FILE = state_file()

# How often the panel refreshes stats/pool/log. A module constant so tests
# (and anyone wanting a snappier panel) can tighten it.
POLL_MS = 600

LOG_COLORS = {
    "debug": "#98a2b3",
    "info":  "#3d4757",
    "warn":  AMBER,
    "error": RED,
}
LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}

SETTINGS_SPEC = [
    ("host",           "Listen host",              "str"),
    ("port",           "Listen port",              "int"),
    ("check_interval", "Health check every (s)",   "int"),
    ("max_retries",    "Upstream retries/request", "int"),
    ("probe_timeout",  "Probe timeout (s)",        "float"),
    ("connect_timeout","Connect timeout (s)",      "float"),
    ("idle_timeout",   "Idle relay timeout (s)",   "float"),
    ("health_workers", "Parallel health probes",   "int"),
    ("probe_connect",  "Also test HTTPS tunnels",  "bool"),
    ("rotate_on",      "Rotate exit on status",    "str"),
    ("refresh_url",    "Refresh list from URL(s)", "str"),
    ("refresh_interval", "Refresh list every (s)", "int"),
]

# Real font specs -- Tk only honours a *single* name as a named font, so we
# resolve the theme family ourselves instead of guessing.
F: dict[str, object] = {}


def _init_fonts(root: tk.Tk) -> None:
    family = tkfont.nametofont("TkDefaultFont").cget("family")
    mono = tkfont.nametofont("TkFixedFont").cget("family")
    F.update(big=(family, 19, "bold"), title=(family, 13, "bold"),
             cap=(family, 9), small=(family, 10), bold=(family, 10, "bold"),
             heading=(family, 9, "bold"), pill=(family, 10, "bold"),
             mono=(mono, 10), mono_small=(mono, 9))


# ---------------------------------------------------------------------------
# small widgets
# ---------------------------------------------------------------------------
class StatCard(tk.Frame):
    """One big number with a caption, drawn as a white card.

    At the window's minimum width the six cards sit narrower than their
    text, so the number steps down through smaller font sizes and the
    caption wraps — both measured with the real font metrics, so nothing
    is clipped whatever the platform's fonts look like.
    """

    def __init__(self, parent, caption: str):
        super().__init__(parent, bg=PANEL, padx=10, pady=11,
                         highlightbackground=BORDER, highlightthickness=1,
                         highlightcolor=ACCENT)
        self._value_font = tkfont.Font(family=F["big"][0], size=F["big"][1],
                                       weight="bold")
        self.value = tk.Label(self, text="—", bg=PANEL, fg=TEXT,
                              font=self._value_font)
        self.caption = tk.Label(self, text=caption.upper(), bg=PANEL, fg=MUTED,
                                font=F["cap"])
        self.value.pack(anchor="w")
        self.caption.pack(anchor="w")
        self.bind("<Configure>", self._refit)

    def set(self, text: str, color: str = TEXT) -> None:
        self.value.configure(text=text, fg=color)
        self._refit()

    def _refit(self, _event=None) -> None:
        """Keep both lines inside the card at any window width."""
        avail = max(48, self.winfo_width() - 24)     # padding + border
        text = str(self.value.cget("text"))
        for size in (19, 17, 15, 13, 11, 9):
            self._value_font.configure(size=size)
            if self._value_font.measure(text) <= avail:
                break
        self.caption.configure(wraplength=avail)


class SectionBar(tk.Frame):
    """Light toolbar strip used above the pool table and the log."""

    def __init__(self, parent):
        super().__init__(parent, bg=PANEL_2, padx=12, pady=7)


def _btn(parent, text, command, style="TButton", width=None):
    # ttk resolves layouts by the full style name, so "Ghost" -> "Ghost.TButton"
    if style != "TButton" and not style.endswith(".TButton"):
        style += ".TButton"
    b = ttk.Button(parent, text=text, command=command, style=style)
    if width:
        b.configure(width=width)
    return b


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------
class ProxyGUI(tk.Tk):
    def __init__(self, *, auto_start: bool = True):
        super().__init__()
        self.title("Rotating Proxy")
        # fit the screen we are on: the table wants ~660px of columns plus
        # the navigation rail, and 780px would not fit a 768px-tall one
        w = min(1360, max(1024, self.winfo_screenwidth() - 24))
        h = min(760, max(640, self.winfo_screenheight() - 56))
        self.geometry(f"{w}x{h}")
        self.minsize(960, 640)
        self.configure(bg=BG)
        try:
            icon = Path(__file__).parent / "packaging" / "icons" / "icon-64.png"
            if icon.exists():
                self.iconphoto(True, tk.PhotoImage(file=str(icon)))
        except tk.TclError:
            pass                          # no icon is never fatal

        self._q: queue.Queue = queue.Queue()
        self._log_lines: deque = deque(maxlen=5000)
        self._hist: deque = deque(maxlen=120)
        self._prev_served = 0
        self._busy = False
        self._row_labels: list[str] = []
        self._sort = ("status", False)          # (column, descending)
        self._filter_var = tk.StringVar(value="")
        self._status_var = tk.StringVar(value="All")
        # the exit cascade: Region -> Country -> State, each box re-offered
        # from the level above (the engine clears what no longer fits)
        self._region_var = tk.StringVar(value="Anywhere")
        self._region_choices: tuple = ("Anywhere",)
        self._country_var = tk.StringVar(value="Anywhere")
        self._country_values: dict[str, str] = {"Anywhere": ""}
        self._country_choices: tuple = ()
        self._country_names: dict[str, str] = {}   # ISO code -> display name
        self._state_var = tk.StringVar(value="Anywhere")
        self._state_values: dict[str, str] = {"Anywhere": ""}
        self._state_choices: tuple = ()
        self._geo_busy = False
        self._asn_busy = False
        self._was_verifying = False        # edge-triggered status line
        self._https_var = tk.BooleanVar(value=False)
        self._level_var = tk.StringVar(value="Info & up")
        self._auto_scroll = tk.BooleanVar(value=True)
        self._closing = False

        _init_fonts(self)

        settings, proxies = self._load_state()
        # restored before the widgets are built, so the routing chips and
        # the "Exit via" picker open showing what the engine actually does
        self._https_var.set(bool(settings.get("https_only")))
        self.engine = RotatingProxy(logger=self._on_engine_log,
                                    proxies=proxies, **settings)

        self._build_styles()
        self._build_menu()
        self._build_sidebar()
        self._main = tk.Frame(self, bg=BG)
        self._main.pack(side="left", fill="both", expand=True)
        self._build_cards()
        # the statusbar must be packed BEFORE the body: the body fills its
        # cavity with expand=True, so anything packed afterwards is left
        # with no space and never appears (this is how the bar went missing)
        self._build_statusbar()
        self._build_body()
        self._build_dialogs()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_buttons()

        self.after(80, self._poll)
        if auto_start:
            self.after(400, self._auto_start)

    # ---------------------------------------------------------------- setup
    def _build_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT,
                        fieldbackground="#ffffff", bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER,
                        focusthickness=0)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL_2)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=F["title"])
        style.configure("TLabelframe", background=BG, foreground=MUTED)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)

        # buttons: flat colour blocks, like the action buttons in the
        # commercial proxy panels (blue primary, green start, red stop)
        for name, fg, bg in (("Accent", "#ffffff", ACCENT),
                             ("Go", "#ffffff", GREEN),
                             ("Halt", "#ffffff", RED),
                             ("Ghost", TEXT, PANEL)):
            style.configure(f"{name}.TButton", background=bg, foreground=fg,
                            focusthickness=0, borderwidth=1, padding=(13, 7))
            style.map(f"{name}.TButton",
                      background=[("active", bg), ("pressed", bg),
                                  ("disabled", "#eef1f5")],
                      foreground=[("disabled", "#aab2bf")])
        style.configure("TButton", background=PANEL, foreground=TEXT,
                        focusthickness=0, borderwidth=1, padding=(11, 6))
        style.map("TButton", background=[("active", "#eef1f5"),
                                         ("pressed", "#e6eaf0")],
                  foreground=[("disabled", "#aab2bf")])

        style.configure("TEntry", fieldbackground="#ffffff", foreground=TEXT,
                        insertcolor=TEXT, bordercolor=BORDER, padding=4)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground="#ffffff", foreground=TEXT,
                        background="#ffffff", arrowcolor=MUTED,
                        bordercolor=BORDER, padding=3)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", "#ffffff")],
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)])

        # the table: white rows, quiet grey headings, blue selection
        style.configure("Treeview", background=PANEL, foreground=TEXT,
                        fieldbackground=PANEL, bordercolor=BORDER,
                        rowheight=27)
        style.configure("Treeview.Heading", background=PANEL_2,
                        foreground="#5b6472", relief="flat", padding=(4, 6),
                        font=F["heading"])
        style.map("Treeview.Heading", background=[("active", "#eceff4")])
        style.map("Treeview", background=[("selected", ACCENT)],
                  foreground=[("selected", "#ffffff")])

        style.configure("TCheckbutton", background=PANEL_2, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", PANEL_2)],
                  foreground=[("active", TEXT)])

        # notebook tabs merge into the card below them
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED,
                        padding=(16, 8), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL)],
                  foreground=[("selected", TEXT)])

        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor="#eceff4", bordercolor="#eceff4",
                        lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_menu(self):
        menubar = tk.Menu(self, bg=PANEL_2, fg=TEXT, activebackground=ACCENT,
                          activeforeground="#ffffff", borderwidth=0)

        filem = tk.Menu(menubar, tearoff=0, bg=PANEL_2, fg=TEXT,
                        activebackground=ACCENT, activeforeground="#ffffff")
        filem.add_command(label="Export alive proxies…", command=self._export)
        filem.add_command(label="Import proxies…", command=self._import)
        filem.add_separator()
        filem.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filem)

        proxm = tk.Menu(menubar, tearoff=0, bg=PANEL_2, fg=TEXT,
                        activebackground=ACCENT, activeforeground="#ffffff")
        proxm.add_command(label="Start", command=self._start)
        proxm.add_command(label="Stop", command=self._stop)
        proxm.add_separator()
        proxm.add_command(label="Check health now", command=self._check_now)
        proxm.add_command(label="Verify exits now", command=self._verify_now)
        proxm.add_command(label="Refresh proxy list now",
                          command=self._refresh_list_now)
        proxm.add_command(label="Settings…", command=self._open_settings)
        proxm.add_separator()
        proxm.add_command(label="Point apps at this proxy…",
                          command=self._open_apps)
        menubar.add_cascade(label="Proxy", menu=proxm)

        helpm = tk.Menu(menubar, tearoff=0, bg=PANEL_2, fg=TEXT,
                        activebackground=ACCENT, activeforeground="#ffffff")
        helpm.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=helpm)

        self.config(menu=menubar)

    def _build_sidebar(self):
        """The left navigation rail: brand, status chip, controls.

        This is the GoLogin-style rail — everything that starts, stops or
        configures the proxy lives here, while the cards and the table own
        the main area.
        """
        side = tk.Frame(self, bg=PANEL, width=236,
                        highlightbackground=BORDER, highlightthickness=1,
                        highlightcolor=BORDER)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=PANEL)
        brand.pack(fill="x", padx=16, pady=(16, 0))
        tk.Label(brand, text="⟳", bg=PANEL, fg=ACCENT,
                 font=F["big"]).pack(side="left")
        tk.Label(brand, text="Rotating Proxy", bg=PANEL, fg=TEXT,
                 font=F["title"]).pack(side="left", padx=(7, 0))

        self.endpoint_var = tk.StringVar(value="")
        tk.Label(side, textvariable=self.endpoint_var, bg=PANEL, fg=MUTED,
                 font=F["mono_small"], anchor="w", padx=16,
                 wraplength=180, justify="left").pack(fill="x", pady=(2, 0))

        # status chip: tinted background, strong text colour
        self.pill = tk.Label(side, text="● STOPPED", bg=RED_TINT, fg=RED,
                             font=F["pill"], padx=12, pady=6, anchor="w")
        self.pill.pack(fill="x", padx=16, pady=(12, 0))

        pad = dict(fill="x", padx=16, pady=(0, 7))
        self.btn_start = _btn(side, "▶  Start proxy", self._start, "Go")
        self.btn_start.pack(**pad)
        self.btn_stop = _btn(side, "■  Stop", self._stop, "Halt")
        self.btn_stop.pack(**pad)
        self.btn_check = _btn(side, "⟳  Check health", self._check_now, "Ghost")
        self.btn_check.pack(**pad)

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", padx=16, pady=9)

        _btn(side, "🌐  Point apps here", self._open_apps,
             "Ghost").pack(**pad)
        _btn(side, "⚙  Settings", self._open_settings, "Ghost").pack(**pad)

        tk.Label(side, text="Route every app through one\nrotating exit — "
                            "all of it stays on this machine.",
                 bg=PANEL, fg=MUTED, font=F["cap"], justify="left",
                 anchor="w", wraplength=180).pack(side="bottom", fill="x",
                                                  padx=16, pady=14)

    def _build_cards(self):
        wrap = tk.Frame(self._main, bg=BG)
        wrap.pack(side="top", fill="x", padx=16, pady=(14, 10))
        self.cards = {}
        specs = [("served", "Requests served"), ("active", "Active now"),
                 ("failed", "Failed (502)"), ("pool", "Pool alive"),
                 ("uptime", "Uptime"), ("traffic", "Traffic")]
        for i, (key, caption) in enumerate(specs):
            card = StatCard(wrap, caption)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else 9, 0))
            wrap.columnconfigure(i, weight=1)
            self.cards[key] = card

    def _build_body(self):
        """Main area: the upstream table card, with the log/traffic card
        underneath — one full-width table, like the proxy lists in the
        commercial panels."""
        body = tk.Frame(self._main, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=16, pady=(0, 14))

        # ---- the activity card underneath -------------------------------
        # Packed *first*: Tk's packer hands out space in packing order while
        # the body is shorter than everything asks for, so a card with a
        # fixed height has to claim its rows before the table asks for its
        # own. The pool is the flexible part (it scrolls) and takes the rest.
        nb_card = tk.Frame(body, bg=PANEL, highlightbackground=BORDER,
                           highlightthickness=1, height=215)
        nb_card.pack(side="bottom", fill="x")
        nb_card.pack_propagate(False)

        self.notebook = ttk.Notebook(nb_card)
        self.notebook.pack(fill="both", expand=True)

        # ---- the pool card ----------------------------------------------
        pool = tk.Frame(body, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        pool.pack(fill="both", expand=True, pady=(0, 10))

        bar = SectionBar(pool)
        bar.pack(fill="x")
        tk.Label(bar, text="UPSTREAM POOL", bg=PANEL_2, fg=MUTED,
                 font=F["heading"]).pack(side="left")

        self.btn_remove = _btn(bar, "Remove", self._remove_selected, "Ghost")
        self.btn_remove.pack(side="right", padx=(5, 0))
        self.btn_add = _btn(bar, "＋ Add proxies", self._open_add, "Accent")
        self.btn_add.pack(side="right", padx=(5, 0))

        # ---- exit via: Region -> Country -> State ------------------------
        # Where traffic leaves, as a cascade: every box only offers what
        # the one above it contains, and the table below stays a view of
        # the *whole* pool (the scope is routing, not a display filter).
        exitbar = tk.Frame(pool, bg=PANEL_2, padx=12, pady=5)
        exitbar.pack(fill="x")
        tk.Label(exitbar, text="Exit via", bg=PANEL_2, fg=MUTED,
                 font=F["small"]).pack(side="left", padx=(0, 8))
        self.region_combo = ttk.Combobox(
            exitbar, textvariable=self._region_var, width=15,
            state="readonly", values=list(self._region_choices))
        self.region_combo.pack(side="left", padx=(0, 8))
        self.region_combo.bind("<<ComboboxSelected>>", self._on_region_pick)
        self.country_combo = ttk.Combobox(
            exitbar, textvariable=self._country_var, width=18,
            state="readonly", values=["Anywhere"])
        self.country_combo.pack(side="left", padx=(0, 8))
        self.country_combo.bind("<<ComboboxSelected>>", self._on_country_pick)
        self.state_combo = ttk.Combobox(
            exitbar, textvariable=self._state_var, width=16,
            state="readonly", values=["Anywhere"])
        self.state_combo.pack(side="left")
        self.state_combo.bind("<<ComboboxSelected>>", self._on_state_pick)

        # ---- routing scope: which proxies the engine may use at all ------
        usebar = tk.Frame(pool, bg=PANEL_2, padx=12, pady=5)
        usebar.pack(fill="x")
        tk.Label(usebar, text="Use", bg=PANEL_2, fg=MUTED,
                 font=F["small"]).pack(side="left", padx=(0, 8))
        self.use_chips: dict[str, tk.Button] = {}
        for flag, label in (("http", "HTTP"), ("https", "HTTPS"),
                            ("socks4", "SOCKS4"), ("socks5", "SOCKS5"),
                            ("strong", "Strong"), ("fast", "Fast")):
            chip = self._make_chip(usebar, label, flag)
            chip.pack(side="left", padx=(0, 6))
            self.use_chips[flag] = chip
        self._sync_use_chips()

        filt = tk.Frame(pool, bg=PANEL_2, padx=12, pady=7)
        filt.pack(fill="x")
        tk.Label(filt, text="Filter", bg=PANEL_2, fg=MUTED).pack(side="left")
        entry = ttk.Entry(filt, textvariable=self._filter_var, width=17)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda e: self._render_pool(force=True))
        combo = ttk.Combobox(filt, textvariable=self._status_var, width=10,
                             state="readonly",
                             values=["All", "Alive", "Dead", "Other", "HTTPS",
                                     "Strong", "Fast", "Flagged"])
        combo.pack(side="left", padx=(0, 6))
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_pool(force=True))
        self.pool_count = tk.Label(filt, text="", bg=PANEL_2, fg=MUTED,
                                   anchor="e")
        self.pool_count.pack(side="right", padx=(10, 0))

        cols = ("proxy", "kind", "country", "state", "status", "latency",
                "checked", "hits", "error")
        self.tree = ttk.Treeview(pool, columns=cols, show="headings",
                                 selectmode="extended")
        # widths sum to 682px -- measured against the real font metrics, so
        # every heading and badge fits even at the window's minimum size
        # (the proxy and error columns stretch to fill whatever is left)
        headings = {"proxy": ("Proxy", 94, "w"), "kind": ("Type", 62, "center"),
                    "country": ("Country", 64, "center"),
                    "state": ("State", 78, "center"),
                    "status": ("Status", 98, "center"),
                    "latency": ("Latency", 72, "e"),
                    "checked": ("Last check", 82, "center"),
                    "hits": ("Served", 58, "e"),
                    "error": ("Last error", 74, "w")}
        for col, (title, width, anchor) in headings.items():
            self.tree.heading(col, text=title,
                              command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor,
                             minwidth=44, stretch=(col in ("proxy", "error")))
        # healthy rows stay neutral like the commercial tables; only
        # trouble (dead/checking), the verification flags and the zebra
        # striping get a colour
        for tag, colour in (("alive", TEXT), ("dead", RED),
                            ("checking", AMBER), ("unknown", TEXT),
                            ("flag", AMBER), ("zebra", "#fafbfd")):
            self.tree.tag_configure(tag, foreground=colour)
        self.tree.tag_configure("zebra", background="#fafbfd")

        vsb = ttk.Scrollbar(pool, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        # packed before the table: a widget packed afterwards would be carved
        # out of the leftover sliver instead of getting a full-width strip
        self.detail = tk.Label(pool, text="Select an upstream to see details",
                               bg=PANEL, fg=MUTED, anchor="w", padx=12, pady=8,
                               font=F["small"])
        self.detail.pack(side="bottom", fill="x")

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())

        # the activity card itself is built and packed at the top of
        # _build_body -- it has to claim its rows before the table asks

        log_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(log_tab, text="  Log  ")

        lbar = SectionBar(log_tab)
        lbar.pack(fill="x")
        tk.Label(lbar, text="ACTIVITY", bg=PANEL_2, fg=MUTED,
                 font=F["heading"]).pack(side="left")
        ttk.Checkbutton(lbar, text="Auto-scroll", variable=self._auto_scroll,
                        style="TCheckbutton").pack(side="right", padx=4)
        ttk.Button(lbar, text="Clear", style="Ghost.TButton",
                   command=self._clear_log, width=7).pack(side="right", padx=4)
        ttk.Button(lbar, text="Copy", style="Ghost.TButton",
                   command=self._copy_log, width=7).pack(side="right", padx=4)
        lvl = ttk.Combobox(lbar, textvariable=self._level_var, width=12,
                           state="readonly",
                           values=["All", "Info & up", "Warnings & up", "Errors only"])
        lvl.pack(side="right", padx=4)
        lvl.bind("<<ComboboxSelected>>", lambda e: self._rebuild_log())

        body2 = tk.Frame(log_tab, bg=PANEL)
        body2.pack(fill="both", expand=True)
        # width=60 keeps the notebook's request modest: the table above is
        # the primary citizen now, this panel is a companion
        self.log_text = tk.Text(body2, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                relief="flat", padx=12, pady=8, wrap="word",
                                width=60, font=F["mono"], state="disabled",
                                highlightthickness=0)
        lsb = ttk.Scrollbar(body2, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        for level, colour in LOG_COLORS.items():
            self.log_text.tag_configure(level, foreground=colour)
        self.log_text.tag_configure("ts", foreground="#98a2b3")
        lsb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        graph_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(graph_tab, text="  Traffic  ")
        self.graph = tk.Canvas(graph_tab, bg=PANEL, highlightthickness=0)
        self.graph.pack(fill="both", expand=True, padx=1, pady=1)
        self.graph.bind("<Configure>", lambda e: self._draw_graph())

    def _build_statusbar(self):
        bar = tk.Frame(self._main, bg=PANEL_2, padx=14, pady=6)
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(bar, textvariable=self.status_var, bg=PANEL_2, fg=MUTED,
                 font=F["small"]).pack(side="left")
        self.hint_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.hint_var, bg=PANEL_2, fg="#8b94a6",
                 font=F["small"]).pack(side="right")
        # created un-packed; shown only while a health sweep is running
        self.progress = ttk.Progressbar(bar, length=170, mode="determinate")

    def _build_dialogs(self):
        self.add_win = None
        self.settings_win = None
        self.apps_win = None
        # the Settings dialog's city-database row (created when it opens)
        self.geo_status = None
        self.geo_btn = None
        # …and its network/ASN row, behind the exit risk flags
        self.asn_status = None
        self.asn_btn = None

    # -------------------------------------------------------------- state
    @staticmethod
    def _coerce(key: str, value, fallback):
        """Cast a persisted setting to its default's type.

        A hand-edited or corrupt state file must never be able to break
        start-up, so anything suspicious falls back to the default.
        """
        want = type(fallback)
        try:
            if want is int:
                out = int(value)
                if key == "port":
                    if not 1 <= out <= 65535:
                        return fallback
                elif out < 1:
                    return fallback
                return out
            if want is float:
                out = float(value)
                return out if out > 0 else fallback
            if want is str:
                out = str(value).strip()
                return out if out else fallback
            if want is bool:
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return bool(value)
                out = str(value).strip().lower()
                if out in ("1", "true", "yes", "on"):
                    return True
                if out in ("0", "false", "no", "off"):
                    return False
                return fallback
        except (TypeError, ValueError):
            return fallback
        return fallback

    def _load_state(self):
        settings, proxies = dict(DEFAULTS), list(PROXY_LIST)
        if not STATE_FILE.exists():
            return settings, proxies
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[gui] could not read {STATE_FILE.name}: {exc}")
            return settings, proxies
        if not isinstance(data, dict):
            return settings, proxies
        for key, value in (data.get("settings") or {}).items():
            if key in settings:
                settings[key] = self._coerce(key, value, settings[key])
        saved = data.get("proxies")
        if isinstance(saved, list) and saved:
            proxies = [str(p) for p in saved]
        return settings, proxies

    def _save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(
                {"settings": self.engine.settings, "proxies": self.engine.proxies()},
                indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"[gui] could not save state: {exc}")

    def _auto_start(self):
        self._start()

    # ------------------------------------------------------------ engine IO
    def _on_engine_log(self, level: str, message: str) -> None:
        """Called from engine threads -- only touches the queue."""
        self._q.put(("log", time.time(), level, message))

    def _run_async(self, fn, *, busy=True, on_done=None):
        if self._busy and busy:
            return
        if busy:
            self._busy = True
            self._update_buttons()

        def worker():
            error = None
            try:
                fn()
            except Exception as exc:                     # surface, don't crash
                error = exc
            self._q.put(("op", error, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        # Line up the next tick *first*: one bad update (or a modal dialog)
        # must never be able to freeze the whole panel.
        try:
            self.after(POLL_MS, self._poll)
        except tk.TclError:
            return                                   # window is going away
        try:
            self._drain_queue()
            self._refresh_stats()
            self._render_pool()
            self._draw_graph()
        except tk.TclError:
            return
        except Exception as exc:
            print(f"[gui] poll error: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    def _drain_queue(self):
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if item[0] == "log":
                self._append_log(*item[1:])
            elif item[0] == "geo":
                self._geo_progress(item[1])
            elif item[0] == "geo-done":
                self._geo_finished(item[1], item[2])
            elif item[0] == "asn":
                self._asn_progress(item[1])
            elif item[0] == "asn-done":
                self._asn_finished(item[1], item[2])
            elif item[0] == "op":
                _, error, on_done = item
                self._busy = False
                self._update_buttons()
                if error:
                    self.status_var.set(str(error))
                    messagebox.showerror("Rotating Proxy", str(error), parent=self)
                if on_done:
                    on_done()

    # ------------------------------------------------------------ commands
    def _start(self):
        if self.engine.running or self._busy:
            return
        snap = self.engine.snapshot()
        self.status_var.set("Starting…")
        self._run_async(lambda: self.engine.start(),
                        on_done=lambda: self.status_var.set(
                            f"Proxy listening on "
                            f"{snap['settings']['host']}:{snap['settings']['port']}"))

    def _stop(self):
        if not self.engine.running or self._busy:
            return
        self.status_var.set("Stopping…")
        self._run_async(self.engine.stop,
                        on_done=lambda: self.status_var.set("Stopped."))

    def _check_now(self):
        if self.engine.checking:
            return
        self.engine.check_now(reason="manual")
        self.status_var.set("Health check running…")

    def _verify_now(self):
        """Ask every exit that is not dead where traffic really leaves.

        The answer is kept on the row, so a second run only asks the
        exits that could not be reached the first time -- use
        `python3 run.py --verify --force` to ask everyone again.
        """
        if self.engine.verify_now():
            self.status_var.set("Verifying exits (asking where traffic "
                                "really leaves)…")

    def _refresh_list_now(self):
        """Pull the configured proxy lists right now (Proxy menu)."""
        if not self.engine.refresh_url:
            self.status_var.set("No refresh URL set — add one in Settings "
                                "first")
            return
        result: dict[str, tuple] = {}

        def work():
            result["pair"] = self.engine.refresh_list(reason="manual")

        def done():
            added, total = result.get("pair", (0, 0))
            self.status_var.set(f"Proxy list refreshed: +{added} new "
                                f"({total} configured)")

        self.status_var.set("Refreshing proxy list…")
        self._run_async(work, on_done=done)

    # -------------------------------------------------------------- pool UI
    def _status_filter_ok(self, node: dict) -> bool:
        choice = self._status_var.get()
        status = node["status"]
        if choice == "All":
            return True
        if choice == "Alive":
            return status == "alive"
        if choice == "Dead":
            return status == "dead"
        if choice == "HTTPS":
            return bool(node.get("connect_ok"))
        if choice == "Strong":
            return node.get("strength") == "Strong"
        if choice == "Fast":
            latency = node.get("latency")
            return latency is not None and latency <= FAST_MS
        if choice == "Flagged":
            # the verification pass has something to say about this exit:
            # it leaves from somewhere else, or from a network services
            # already distrust
            return bool(_flags_of(node))
        return status == "unknown"

    def _sort_by(self, column: str):
        if self._sort[0] == column:
            self._sort = (column, not self._sort[1])
        else:
            self._sort = (column, False)
        self._render_pool(force=True)

    @staticmethod
    def _make_sort_key(column: str):
        """Return a total-order key for the requested column.

        Every key ends on `_ip_key`, so rows that tie (same status, same
        latency, …) still list their addresses numerically instead of as
        text -- 10.0.0.2 always ahead of 10.0.0.10."""
        rank = {"alive": 0, "unknown": 1, "checking": 1, "dead": 2}
        tier = {"Strong": 0, "Good": 1, "New": 2, "Weak": 3}
        if column == "status":
            return lambda n: (rank.get(n["status"], 3),
                              tier.get(n.get("strength", "New"), 2),
                              _ip_key(n["label"]))
        if column == "latency":
            return lambda n: (n["latency"] is None, n["latency"] or 0.0,
                              _ip_key(n["label"]))
        if column == "checked":
            return lambda n: (n["last_check"], _ip_key(n["label"]))
        if column == "hits":
            return lambda n: (n["hits"], _ip_key(n["label"]))
        if column == "error":
            return lambda n: (n["last_error"], _ip_key(n["label"]))
        if column == "kind":
            return lambda n: (n.get("kind", ""), _ip_key(n["label"]))
        if column == "country":
            return lambda n: (n.get("cc") or "~", _ip_key(n["label"]))
        if column == "state":
            return lambda n: ((n.get("state") or n.get("state_code") or "~"),
                              _ip_key(n["label"]))
        return lambda n: _ip_key(n["label"])   # proxy / fallback

    def _render_pool(self, force: bool = False):
        nodes = self.engine.nodes()
        needle = self._filter_var.get().strip().lower()
        # the table is a view of the *whole* pool: the exit cascade above
        # narrows routing, never what is listed (a scope that hides rows
        # makes it impossible to see what else is configured)
        visible = [n for n in nodes
                   if self._status_filter_ok(n)
                   and (not needle or needle in n["label"].lower()
                        or needle in n.get("kind", "").lower()
                        or needle in n.get("country", "").lower()
                        or needle in n.get("cc", "").lower()
                        or needle in n.get("region", "").lower()
                        or needle in n.get("state", "").lower()
                        or needle in n.get("city", "").lower()
                        or needle in n.get("strength", "").lower()
                        or needle in n["last_error"].lower())]

        column, descending = self._sort
        visible.sort(key=self._make_sort_key(column), reverse=descending)

        labels = [n["label"] for n in visible]
        if labels != self._row_labels:
            # membership or order changed -> rebuild, keeping the selection
            selected = set(self.tree.selection())
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            for idx, n in enumerate(visible):
                self.tree.insert("", "end", iid=n["label"],
                                 values=self._row_values(n),
                                 tags=self._tags_of(n, idx % 2 == 1))
            self._row_labels = labels
            keep = [l for l in labels if l in selected]
            if keep:
                self.tree.selection_set(keep)
        else:
            # same rows, same order -> cheap in-place value refresh
            for idx, n in enumerate(visible):
                values = self._row_values(n)
                if self.tree.item(n["label"], "values") != values:
                    self.tree.item(n["label"], values=values,
                                   tags=self._tags_of(n, idx % 2 == 1))

        alive = sum(1 for n in nodes if n["status"] == "alive")
        socks = sum(1 for n in nodes if n.get("proto") != "http")
        strong = sum(1 for n in nodes if n.get("strength") == "Strong")
        fast = sum(1 for n in nodes
                   if n.get("latency") is not None and n["latency"] <= 300)
        tally = [f"{alive}/{len(nodes)} alive"]
        if socks:
            tally.append(f"{socks} socks")
        https = sum(1 for n in nodes if n.get("connect_ok"))
        if https:
            tally.append(f"{https} https-capable")
        if strong:
            tally.append(f"{strong} strong")
        if fast:
            tally.append(f"{fast} fast")
        verified = sum(1 for n in nodes if n.get("verified"))
        if verified:
            tally.append(f"{verified} verified")
        mismatch = sum(1 for n in nodes if n.get("mismatch"))
        if mismatch:
            tally.append(f"{mismatch} geo≠")
        risky = sum(1 for n in nodes if n.get("risk"))
        if risky:
            tally.append(f"{risky} risky")
        place = self.engine.exit_place
        if place:
            tally.append(f"exit {place}")
        for flag in self.engine.scope_fallback:
            # a quality preference nobody in scope can honour yet: said
            # out loud, because the traffic is leaving somewhere else
            tally.append(f"{flag} fallback")
        if self.engine.https_only:
            tally.append("HTTPS only")
        # at the window's minimum the filter row runs out of room: drop
        # the nice-to-have items (least important first) rather than
        # clipping the counter.  The spare room is measured against the
        # row and the real font -- never against the label's own width,
        # which is stale the instant the text changes -- and the next
        # poll rebuilds the full list as soon as there is space again.
        text = " · ".join(tally)
        room = self._tally_room()
        font = self._tally_font()
        while len(tally) > 1 and font.measure(text) > room:
            tally.pop()
            text = " · ".join(tally)
        self.pool_count.configure(text=text)

    def _tally_room(self) -> int:
        """Pixels the filter row can spare for the pool counter."""
        row = self.pool_count.master
        used = sum(c.winfo_width() for c in row.children.values()
                   if c is not self.pool_count)
        # gaps, the row's own padding and the counter's left padding
        return max(0, row.winfo_width() - used - 20)

    def _tally_font(self):
        """The font the counter really draws with, for width measuring."""
        spec = self.pool_count.cget("font")
        try:
            return tkfont.Font(font=spec) if spec else \
                tkfont.nametofont("TkDefaultFont")
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")
        if force:
            self.tree.update_idletasks()

    def _refresh_countries(self, snap: dict):
        """Rebuild the "Exit via" picker from the countries the pool has.

        Only countries *inside the chosen region* are offered: the box
        below must never be able to contradict the one above it -- a
        country outside the region is refused by the engine, and offering
        it just makes the panel look broken when the pick does nothing.
        """
        scope = snap.get("pool_countries") or {}
        region = str(snap.get("region") or "")
        choices = [("", "Anywhere")]
        for e in sorted(scope.values(),
                        key=lambda v: (-int(v.get("alive", 0)),
                                       -int(v.get("total", 0)),
                                       str(v.get("name") or v["cc"]).lower())):
            cc = e["cc"]
            if region and region_of(cc) != region:
                continue
            name = e.get("name") or cc
            choices.append((cc, f"{name} ({cc}) · "
                                f"{e.get('alive', 0)}/{e.get('total', 0)}"))
        labels = [text for _, text in choices]
        if tuple(labels) != self._country_choices:
            self._country_choices = tuple(labels)
            self._country_values = {text: cc for cc, text in choices}
            self.country_combo.configure(values=labels)
        names = {cc: (e.get("name") or cc) for cc, e in scope.items()
                 if not region or region_of(cc) == region}
        if names != self._country_names:
            self._country_names = names
        active = snap.get("country") or ""
        wanted = next((text for cc, text in choices if cc == active), "")
        if active and not wanted:
            # the scope outlived its upstreams (or GeoIP lost the code):
            # keep showing it rather than claiming "Anywhere" while the
            # engine is still restricting traffic
            name = names.get(active) or active
            wanted = f"{name} ({active}) · not in pool"
            choices.append((active, wanted))
            labels = [text for _, text in choices]
            self._country_choices = tuple(labels)
            self._country_values = {text: cc for cc, text in choices}
            self.country_combo.configure(values=labels)
        if not wanted:
            wanted = "Anywhere"
        if self._country_var.get() != wanted:
            self._country_var.set(wanted)

    def _refresh_regions(self, snap: dict):
        """Rebuild the Region box of the exit cascade from the pool."""
        scope = snap.get("pool_countries") or {}
        present = sorted({region_of(cc) for cc in scope} - {""},
                         key=str.lower)
        active = snap.get("region") or ""
        if active and active not in present:
            # a scope whose upstreams all left must stay visible rather
            # than the panel claiming "Anywhere" while it still restricts
            present.append(active)
            present.sort(key=str.lower)
        values = ["Anywhere"] + present
        if tuple(values) != self._region_choices:
            self._region_choices = tuple(values)
            self.region_combo.configure(values=values)
        wanted = active or "Anywhere"
        if self._region_var.get() != wanted:
            self._region_var.set(wanted)

    def _refresh_states(self, snap: dict):
        """Rebuild the State box from the states inside region + country."""
        scope = snap.get("pool_states") or {}
        active = snap.get("state") or ""
        choices = [("", "Anywhere")]
        for name, count in sorted(scope.items(), key=lambda kv: kv[0].lower()):
            choices.append((name, f"{name} · {count}"))
        if active and active not in scope:
            # keep showing a state that no longer has an upstream in the
            # pool -- it is still what the engine is restricting traffic to
            choices.append((active, f"{active} · not in pool"))
        labels = [text for _, text in choices]
        if tuple(labels) != self._state_choices:
            self._state_choices = tuple(labels)
            self._state_values = {text: name for name, text in choices}
            self.state_combo.configure(values=labels)
        wanted = next((text for name, text in choices if name == active), "")
        if not wanted:
            wanted = "Anywhere"
        if self._state_var.get() != wanted:
            self._state_var.set(wanted)

    def _on_region_pick(self, _event=None):
        region = self._region_var.get()
        region = "" if region in ("", "Anywhere") else region
        try:
            # the engine logs the change itself, on every thread
            self.engine.set_region(region)
        except ValueError as exc:
            self._append_log(time.time(), "error", f"exit region: {exc}")
            self._refresh_regions(self.engine.snapshot())
            return
        # written straight away: a restart (or a kill) must not lose it
        self._save_state()
        snap = self.engine.snapshot()
        # everything below the region may no longer fit: re-offer it from
        # here, exactly like picking a new country re-offers its states
        self._refresh_regions(snap)
        self._refresh_countries(snap)
        self._refresh_states(snap)
        self.status_var.set(f"Exit region: {self._region_var.get()}")
        self._render_pool(force=True)

    def _on_country_pick(self, _event=None):
        code = self._country_values.get(self._country_var.get(), "")
        try:
            # the engine logs the change itself, on every thread
            self.engine.set_country(code)
        except ValueError as exc:
            self._append_log(time.time(), "error", f"exit country: {exc}")
            self._refresh_countries(self.engine.snapshot())
            return
        # written straight away: a restart (or a kill) must not lose it
        self._save_state()
        snap = self.engine.snapshot()
        self.status_var.set(f"Exit country: "
                            f"{self._country_var.get()}")
        self._refresh_countries(snap)
        # a state from the country we just left is gone from the pool
        # the engine keeps -- the box has to follow it
        self._refresh_states(snap)
        self._render_pool(force=True)

    def _on_state_pick(self, _event=None):
        state = self._state_values.get(self._state_var.get(), "")
        try:
            self.engine.set_state(state)
        except ValueError as exc:
            self._append_log(time.time(), "error", f"exit state: {exc}")
            self._refresh_states(self.engine.snapshot())
            return
        self._save_state()
        snap = self.engine.snapshot()
        self._refresh_states(snap)
        self.status_var.set(f"Exit state: {self._state_var.get()}")
        self._render_pool(force=True)

    def _on_https_toggle(self):
        """Route only through upstreams that can open CONNECT tunnels."""
        try:
            self.engine.set_https_only(self._https_var.get())
        except ValueError as exc:
            self._append_log(time.time(), "error", f"https scope: {exc}")
            self._https_var.set(self.engine.https_only)
            return
        self._save_state()
        self._sync_use_chips()
        self.status_var.set("HTTPS-only routing is "
                            f"{'on' if self.engine.https_only else 'off'}.")
        self._render_pool(force=True)

    # ---- routing chips (the "Use" row above the pool table) -------------
    def _make_chip(self, parent, text, flag):
        """A pill toggle that narrows what the engine may route through.

        HTTPS rides the existing `https_only` setting (one source of truth
        with the old checkbox it replaces); the other flags toggle entries
        in the engine's `use_only` scope.
        """
        return tk.Button(
            parent, text=text, relief="flat", bd=0, padx=9, pady=2,
            font=F["small"], cursor="hand2", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=BORDER,
            activebackground="#eef1f5", activeforeground=TEXT,
            command=(self._on_https_chip if flag == "https"
                     else lambda f=flag: self._toggle_use(f)))

    def _sync_use_chips(self):
        """Paint each chip from the engine's actual routing scope."""
        flags = set(self.engine.use_flags)
        https = bool(self.engine.https_only)
        for flag, chip in self.use_chips.items():
            on = https if flag == "https" else (flag in flags)
            chip.configure(bg=ACCENT if on else PANEL,
                           fg="#ffffff" if on else MUTED,
                           highlightbackground=ACCENT if on else BORDER,
                           activebackground="#1d4ed8" if on else "#eef1f5",
                           activeforeground="#ffffff" if on else TEXT)

    def _chip_on(self, flag: str) -> bool:
        """True when the chip for `flag` is currently lit."""
        chip = self.use_chips.get(flag)
        return bool(chip) and str(chip.cget("bg")) == ACCENT

    def _on_https_chip(self):
        self._https_var.set(not self._https_var.get())
        self._on_https_toggle()

    def _toggle_use(self, flag: str):
        """Add or remove one routing flag — which proxies may be used."""
        flags = set(self.engine.use_flags)
        flags.symmetric_difference_update({flag})
        try:
            self.engine.set_use(",".join(sorted(flags)))
        except ValueError as exc:
            self._append_log(time.time(), "error", f"use scope: {exc}")
            return
        self._sync_use_chips()
        self._save_state()
        active = [f for f in USE_FLAGS if f in self.engine.use_flags]
        self.status_var.set("Routing scope: "
                            + (", ".join(active) if active
                               else "any upstream"))
        self._render_pool(force=True)

    @staticmethod
    def _row_values(n: dict) -> tuple[str, ...]:
        """All strings -- Tk stores/displays everything as text, and keeping
        the types stable lets us cheap-compare rows for changes.

        The status cell doubles as the strength badge: "Alive · Strong" is
        the tier users actually filter and sort on, and the Treeview can
        only colour whole rows, not individual cells.

        Verification speaks through the same cell, and only when it has
        something to say: a proven mismatch or a hosting/VPN network
        replaces the tier (98 px buys 14 characters, and knowing your
        traffic leaves from somewhere else beats knowing the tier -- which
        the detail line still carries).  The tokens are deliberately
        short: geo≠ = traffic leaves from another country, DC = datacentre
        or cloud address, VPN = the ASN calls itself a VPN.
        """
        latency = "—" if n["latency"] is None else f"{n['latency']:.0f} ms"
        status = n["status"]
        if status == "alive":
            badge = _live_badge(n)
        elif status == "checking":
            badge = "Checking…"
        elif status == "dead":
            badge = "Dead"
        else:
            badge = "Unverified"
        return (n["label"], n.get("kind", "HTTP"),
                n.get("cc") or "—", n.get("state_label") or "—", badge,
                latency,
                _ago(n["last_check"]), str(n["hits"]),
                (n["last_error"] or "")[:70])

    @staticmethod
    def _tags_of(n: dict, striped: bool) -> tuple[str, ...]:
        """Row colours: status, then the verification flag, then the zebra
        background last so it can never hide a foreground colour.

        Only alive rows get the flag colour -- a red dead row already says
        everything, and amber on top of it would only say less.
        """
        tags = [n["status"]]
        if n["status"] == "alive" and _flags_of(n):
            tags.append("flag")
        if striped:
            tags.append("zebra")
        return tuple(tags)

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            self.detail.configure(text="Select an upstream to see details")
            return
        wanted = {n["label"]: n for n in self.engine.nodes()}
        if len(selection) == 1 and selection[0] in wanted:
            n = wanted[selection[0]]
            latency = ("—" if n["latency"] is None
                       else f"{n['latency']:.1f} ms")
            score = n.get("score")
            samples = int(n.get("samples") or 0)
            if score is None or not samples:
                strength = f"strength: {n.get('strength', 'New')}"
            else:
                strength = (f"strength: {n.get('strength', 'New')} "
                            f"({score * 100:.0f}% over {samples} checks)")
            parts = [n["label"], f"type: {n.get('kind', 'HTTP')}",
                     f"country: {_country_of(n)}",
                     f"region: {n.get('region') or 'unknown'}",
                     f"status: {n['status']}",
                     strength,
                     f"latency: {latency}",
                     f"consecutive failures: {n['failures']}",
                     f"served: {n['hits']}",
                     f"last check: {_ago(n['last_check'])}"]
            if n.get("connect_ok") is not None:
                parts.append("https: " + ("yes" if n["connect_ok"] else "no"))
            if n.get("egress_ip"):
                # what the verification pass proved, in the exit's own words
                flag = ("  ← leaves from another country"
                        if n.get("mismatch") else "")
                parts.append(f"egress: {n.get('egress_label')}{flag}")
            if n.get("asn_org") or n.get("asn"):
                net = f"AS{n['asn']} {n['asn_org']}".strip()
                if n.get("risk"):
                    net += f" ({n['risk_label']})"
                parts.append(f"network: {net}")
            if n.get("blocks"):
                parts.append(f"blocked by targets: {n['blocks']}×")
            if n["last_error"]:
                parts.append(f"last error: {n['last_error']}")
            where = ", ".join(b for b in (n.get("state"), n.get("city"))
                              if b)
            if where:
                # only when the city database knows the address: an
                # "unknown" place would just be noise in the detail line
                parts.insert(4, f"place: {where}")
            self.detail.configure(text="   ·   ".join(parts))
        else:
            self.detail.configure(text=f"{len(selection)} upstreams selected")

    def _selected_labels(self) -> list[str]:
        return list(self.tree.selection())

    def _open_add(self):
        win = tk.Toplevel(self)
        win.title("Add upstream proxies")
        win.configure(bg=BG)
        win.geometry("460x340")
        win.transient(self)
        win.grab_set()
        self.add_win = win

        tk.Label(win,
                 text=("Paste upstreams, one per line (comma separated also "
                       "works):\n"
                       "    1.2.3.4:8080                 HTTP\n"
                       "    socks4://1.2.3.4:1080     SOCKS4\n"
                       "    socks5://1.2.3.4:1080     SOCKS5\n"
                       "Whole export files (ip, port, country, type…) are "
                       "understood too."),
                 bg=BG, fg=TEXT, anchor="w", justify="left").pack(
                     fill="x", padx=14, pady=(14, 6))
        text = tk.Text(win, bg="#ffffff", fg=TEXT, insertbackground=TEXT,
                       relief="flat", padx=10, pady=8, font=F["mono"],
                       highlightthickness=1, highlightbackground=BORDER)
        text.pack(fill="both", expand=True, padx=14)
        text.insert("1.0", "")
        text.focus_set()

        def do_add():
            added = self.engine.add_proxies(text.get("1.0", "end"))
            self._save_state()
            self.status_var.set(f"Added {added} upstream"
                                f"{'s' if added != 1 else ''}")
            self._render_pool(force=True)
            win.destroy()

        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=14, pady=12)
        ttk.Button(row, text="Cancel", style="Ghost.TButton",
                   command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Add to pool", style="Accent.TButton",
                   command=do_add).pack(side="right")

    def _remove_selected(self):
        labels = self._selected_labels()
        if not labels:
            self.status_var.set("Nothing selected.")
            return
        if len(labels) == 1 or messagebox.askyesno(
                "Remove upstreams",
                f"Remove {len(labels)} upstream{'s' if len(labels) != 1 else ''} "
                f"from the pool?", parent=self):
            self.engine.remove_proxies(labels)
            self._save_state()
            self.status_var.set(f"Removed {len(labels)} upstream"
                                f"{'s' if len(labels) != 1 else ''}")
            self._render_pool(force=True)

    def _export(self):
        alive = self.engine.proxies("alive") or self.engine.proxies()
        path = filedialog.asksaveasfilename(
            parent=self, title="Export proxies", defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="alive_proxies.txt")
        if not path:
            return
        try:
            Path(path).write_text("\n".join(alive) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.status_var.set(f"Exported {len(alive)} proxies to {path}")

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self, title="Import proxies",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            blob = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return
        added = self.engine.add_proxies(blob)
        self._save_state()
        self.status_var.set(f"Imported {added} proxies from {path}")
        self._render_pool(force=True)

    # -------------------------------------------------------------- log UI
    def _append_log(self, ts: float, level: str, message: str):
        self._log_lines.append((ts, level, message))
        if self._level_visible(level):
            self._insert_line(ts, level, message)

    def _level_visible(self, level: str) -> bool:
        threshold = {"All": 0, "Info & up": 1, "Warnings & up": 2,
                     "Errors only": 3}.get(self._level_var.get(), 1)
        return LEVEL_RANK.get(level, 1) >= threshold

    @staticmethod
    def _stamp(ts: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(ts))

    def _insert_line(self, ts: float, level: str, message: str):
        text = self.log_text
        # are we already looking at the bottom? (must be read before we scroll)
        near_end = text.yview()[1] >= 0.995
        text.configure(state="normal")
        text.insert("end", f"{self._stamp(ts)} ", "ts")
        text.insert("end", f"[{level.upper():5}] ", level)
        text.insert("end", f"{message}\n", level)
        line_count = int(text.index("end-1c").split(".")[0])
        if line_count > 5200:
            text.delete("1.0", f"{line_count - 5000}.0")
        text.configure(state="disabled")
        if self._auto_scroll.get() or near_end:
            text.see("end")

    def _rebuild_log(self):
        text = self.log_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for ts, level, message in self._log_lines:
            if not self._level_visible(level):
                continue
            text.insert("end", f"{self._stamp(ts)} ", "ts")
            text.insert("end", f"[{level.upper():5}] ", level)
            text.insert("end", f"{message}\n", level)
        text.configure(state="disabled")
        if self._auto_scroll.get():
            text.see("end")

    def _clear_log(self):
        self._log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _copy_log(self):
        blob = "\n".join(f"{self._stamp(t)} [{lvl.upper()}] {msg}"
                         for t, lvl, msg in self._log_lines)
        self.clipboard_clear()
        self.clipboard_append(blob)
        self.status_var.set("Log copied to clipboard")

    # ------------------------------------------------------------- stats UI
    def _refresh_stats(self):
        snap = self.engine.snapshot()

        self.endpoint_var.set(
            f"http://{snap['host']}:{snap['port']}   ·   "
            f"{snap['total']} upstreams configured")
        self.hint_var.set("Set any app's HTTP/HTTPS proxy to the address above")

        # status pill: soft tint behind a strong text colour
        if snap["status"] == "running" and snap["checking"]:
            pill_text, pill_bg, pill_fg = "● CHECKING", AMBER_TINT, AMBER
        elif snap["status"] == "running" and snap.get("verifying"):
            pill_text, pill_bg, pill_fg = "● VERIFYING", AMBER_TINT, AMBER
        elif snap["status"] == "running":
            pill_text, pill_bg, pill_fg = "● RUNNING", GREEN_TINT, GREEN
        elif snap["status"] == "starting":
            pill_text, pill_bg, pill_fg = "● STARTING", AMBER_TINT, AMBER
        else:
            pill_text, pill_bg, pill_fg = "● STOPPED", RED_TINT, RED
        if self.pill.cget("text") != pill_text:
            self.pill.configure(text=pill_text, bg=pill_bg, fg=pill_fg)

        self.cards["served"].set(f"{snap['served']:,}", CYAN)
        self.cards["active"].set(f"{snap['active']}",
                                 AMBER if snap["active"] else TEXT)
        self.cards["failed"].set(f"{snap['failed']:,}",
                                 RED if snap["failed"] else TEXT)
        self.cards["pool"].set(f"{snap['pool_alive']} / {snap['total']}",
                               GREEN if snap["pool_alive"] else RED)
        self.cards["uptime"].set(_duration(snap["uptime"]))
        mb = (snap["bytes_in"] + snap["bytes_out"]) / 1_048_576
        self.cards["traffic"].set(f"{mb:,.1f} MB")

        # health-check progress
        done, total = snap["progress"]
        if snap["checking"] and total:
            if not self.progress.winfo_ismapped():
                self.progress.pack(side="right", padx=(8, 0))
            self.progress.configure(maximum=total, value=done)
            self.status_var.set(f"Checking upstreams… {done}/{total}")
        elif self.progress.winfo_ismapped():
            self.progress.pack_forget()
        elif self.status_var.get().startswith(("Checking", "Health check")):
            self.status_var.set("")        # the sweep finished

        # the verification pass reports through the log; the status line
        # only says when it started and that it is over
        verifying = bool(snap.get("verifying"))
        if verifying != self._was_verifying:
            if verifying:
                self.status_var.set("Verifying exits (asking where traffic "
                                    "really leaves)…")
            else:
                self.status_var.set("Exit verification finished — "
                                    "see the log for the report")
            self._was_verifying = verifying

        # traffic history
        rate = snap["served"] - self._prev_served
        self._prev_served = snap["served"]
        self._hist.append((max(rate, 0), snap["active"]))

        self._update_buttons()
        self._refresh_countries(snap)
        self._refresh_regions(snap)
        self._refresh_states(snap)

    def _update_buttons(self):
        running = self.engine.running
        self.btn_start.configure(state="disabled" if running or self._busy else "normal")
        self.btn_stop.configure(state="normal" if running and not self._busy else "disabled")
        self.btn_check.configure(state="normal" if not self._busy else "disabled")
        self.btn_add.configure(state="disabled" if self._busy else "normal")
        self.btn_remove.configure(state="disabled" if self._busy else "normal")

    # ------------------------------------------------------------ graph UI
    def _draw_graph(self):
        canvas = self.graph
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 60 or h < 60:
            return
        pad_l, pad_r, pad_t, pad_b = 46, 14, 18, 24
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b

        for i in range(5):
            y = pad_t + gh * i / 4
            canvas.create_line(pad_l, y, pad_l + gw, y, fill="#e6eaf1")
        for i in range(7):
            x = pad_l + gw * i / 6
            canvas.create_line(x, pad_t, x, pad_t + gh, fill="#f0f3f7")

        data = list(self._hist)
        peak = max([1] + [max(s, a) for s, a in data])
        canvas.create_text(pad_l - 8, pad_t, text=str(int(peak)),
                           anchor="e", fill=MUTED, font=F["cap"])
        canvas.create_text(pad_l - 8, pad_t + gh, text="0",
                           anchor="e", fill=MUTED, font=F["cap"])
        canvas.create_text(pad_l, h - 8, text=f"−{len(data)}s"
                           if data else "waiting for traffic",
                           anchor="w", fill="#98a2b3", font=F["cap"])

        if len(data) >= 2:
            for idx, colour in ((0, CYAN), (1, AMBER)):
                pts = []
                for i, sample in enumerate(data):
                    x = pad_l + gw * i / (len(data) - 1)
                    y = pad_t + gh * (1 - sample[idx] / peak)
                    pts += [x, y]
                canvas.create_line(*pts, fill=colour, width=2, smooth=True)

        canvas.create_text(pad_l + 6, pad_t - 8, anchor="w", fill=CYAN,
                           font=F["bold"],
                           text="■ requests / poll")
        canvas.create_text(pad_l + 130, pad_t - 8, anchor="w", fill=AMBER,
                           font=F["bold"],
                           text="■ concurrent connections")

    # ------------------------------------------------------------ settings
    @staticmethod
    def _geo_status_text() -> str:
        """One line for the Settings row: is the state database there?"""
        import geodb

        info = geodb.city_status()
        if not info["available"]:
            return "not installed — needed for state scope"
        size = f"{info['size'] / 1_048_576:.1f} MB"
        when = (time.strftime("%Y-%m-%d", time.localtime(info["updated"]))
                if info.get("updated") else "")
        return "installed · " + " · ".join(x for x in (size, when) if x)

    def _download_geo(self) -> None:
        """Fetch the city/state database once -- in the background.

        The panel must stay usable while ~60 MB downloads, and worker
        threads never touch Tk: progress and the result travel through the
        same queue the engine's log uses.
        """
        if self._geo_busy:
            return
        self._geo_busy = True
        if self.geo_btn is not None:
            self.geo_btn.configure(state="disabled", text="Downloading…")
        self.status_var.set("Downloading the state/city database…")

        import geodb

        def progress(done: int, total: int) -> None:
            text = f"downloading… {done / 1_048_576:.1f} MB"
            if total:
                text += f" / {total / 1_048_576:.0f} MB ({100 * done // total}%)"
            self._q.put(("geo", text))

        def worker():
            path, error = None, None
            try:
                path = geodb.fetch_city_db(progress=progress)
                # every row now has a place: re-tag the pool (health is
                # kept for the entries that survive the rewrite)
                self.engine.set_proxies(list(self.engine.proxies()))
            except Exception as exc:                 # surface, don't crash
                path, error = None, exc
            self._q.put(("geo-done", path, error))

        threading.Thread(target=worker, daemon=True).start()

    def _geo_progress(self, text: str) -> None:
        if self.geo_status is not None:
            self.geo_status.set(text)

    def _geo_finished(self, path, error) -> None:
        self._geo_busy = False
        if self.geo_status is not None:
            self.geo_status.set(str(error) if error
                                else self._geo_status_text())
        if self.geo_btn is not None and self.geo_btn.winfo_exists():
            self.geo_btn.configure(state="normal", text="Refresh")
        if error is not None:
            self.status_var.set(str(error))
            self._append_log(time.time(), "error", f"state database: {error}")
            return
        self._append_log(time.time(), "info", f"state database installed: "
                                              f"{path}")
        self.status_var.set("State/city database installed.")
        # the cascade can offer states now: re-offer every box from the top
        snap = self.engine.snapshot()
        self._refresh_regions(snap)
        self._refresh_countries(snap)
        self._refresh_states(snap)
        self._render_pool(force=True)

    # ---- the ASN database behind the exit risk flags ---------------------
    @staticmethod
    def _asn_status_text() -> str:
        """One line for the Settings row: is the network database there?"""
        import geodb

        info = geodb.asn_status()
        if not info["available"]:
            return "not installed — risk flags stay empty"
        size = f"{info['size'] / 1_048_576:.1f} MB"
        when = (time.strftime("%Y-%m-%d", time.localtime(info["updated"]))
                if info.get("updated") else "")
        return "installed · " + " · ".join(x for x in (size, when) if x)

    def _download_asn(self) -> None:
        """Fetch the ASN database once -- in the background, like the city
        one: the panel stays usable while it downloads, and worker threads
        never touch Tk (progress travels through the log queue)."""
        if self._asn_busy:
            return
        self._asn_busy = True
        if self.asn_btn is not None:
            self.asn_btn.configure(state="disabled", text="Downloading…")
        self.status_var.set("Downloading the network (ASN) database…")

        import geodb

        def progress(done: int, total: int) -> None:
            text = f"downloading… {done / 1_048_576:.1f} MB"
            if total:
                text += f" / {total / 1_048_576:.0f} MB ({100 * done // total}%)"
            self._q.put(("asn", text))

        def worker():
            path, error = None, None
            try:
                path = geodb.fetch_asn_db(progress=progress)
            except Exception as exc:                 # surface, don't crash
                path, error = None, exc
            self._q.put(("asn-done", path, error))

        threading.Thread(target=worker, daemon=True).start()

    def _asn_progress(self, text: str) -> None:
        if self.asn_status is not None:
            self.asn_status.set(text)

    def _asn_finished(self, path, error) -> None:
        self._asn_busy = False
        if self.asn_status is not None:
            self.asn_status.set(str(error) if error
                                else self._asn_status_text())
        if self.asn_btn is not None and self.asn_btn.winfo_exists():
            self.asn_btn.configure(state="normal", text="Refresh")
        if error is not None:
            self.status_var.set(str(error))
            self._append_log(time.time(), "error", f"ASN database: {error}")
            return
        self._append_log(time.time(), "info", f"network database installed: "
                                              f"{path}")
        self.status_var.set("Network database installed — run "
                            "“Verify exits now” to apply the flags")

    def _open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG)
        win.geometry("430x620")
        win.transient(self)
        win.grab_set()
        self.settings_win = win

        vars_: dict[str, tk.StringVar] = {}
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        for key, label, kind in SETTINGS_SPEC:
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label, bg=BG, fg=TEXT, width=26,
                     anchor="w").pack(side="left")
            if kind == "bool":
                var = tk.BooleanVar(value=bool(self.engine.settings[key]))
                vars_[key] = (var, kind)
                ttk.Checkbutton(row, variable=var).pack(side="right")
                continue
            var = tk.StringVar(value=str(self.engine.settings[key]))
            vars_[key] = (var, kind)
            ttk.Entry(row, textvariable=var, width=16).pack(side="right")

        # ---- the database behind the State column and the State box -----
        import geodb
        installed = geodb.city_available()
        geo = tk.Frame(body, bg=BG)
        geo.pack(fill="x", pady=(10, 0))
        tk.Label(geo, text="State/city database", bg=BG, fg=TEXT, width=26,
                 anchor="w").pack(side="left")
        self.geo_status = tk.StringVar(value=self._geo_status_text())
        tk.Label(geo, textvariable=self.geo_status, bg=BG, fg=MUTED,
                 font=F["small"], anchor="w").pack(side="left")
        self.geo_btn = ttk.Button(geo, width=10,
                                  text="Refresh" if installed else "Download",
                                  command=self._download_geo)
        self.geo_btn.pack(side="right")

        # ---- the database behind the exit risk flags ---------------------
        installed_asn = geodb.asn_available()
        net = tk.Frame(body, bg=BG)
        net.pack(fill="x", pady=(6, 0))
        tk.Label(net, text="Network (ASN) database", bg=BG, fg=TEXT,
                 width=26, anchor="w").pack(side="left")
        self.asn_status = tk.StringVar(value=self._asn_status_text())
        tk.Label(net, textvariable=self.asn_status, bg=BG, fg=MUTED,
                 font=F["small"], anchor="w").pack(side="left")
        self.asn_btn = ttk.Button(net, width=10,
                                  text="Refresh" if installed_asn
                                  else "Download",
                                  command=self._download_asn)
        self.asn_btn.pack(side="right")

        note = ("Settings apply the next time you start the proxy.\n"
                "The port change always requires a restart.\n"
                "“Rotate exit on status” is a comma-separated list of the\n"
                "statuses that make a request retry from another exit IP\n"
                "(429/403 blocks; empty switches it off).\n"
                "“Refresh list from URL(s)” auto-reloads fresh proxies\n"
                "from plain-text lists while running (empty switches off).\n"
                "“State/city database” fills the State column and the State\n"
                "box of Exit via: one ~60 MB download (DB-IP City Lite, CC\n"
                "BY 4.0) into "
                f"{geodb.city_path().parent}, then every state\n"
                "lookup is offline.\n"
                "“Network (ASN) database” says who runs an exit's address\n"
                "(one ~5 MB download, DB-IP ASN Lite, CC BY 4.0) and powers\n"
                "the hosting/VPN risk flags of “Verify exits now”.\n")
        tk.Label(body, text=note, bg=BG, fg=MUTED, justify="left",
                 font=F["small"]).pack(anchor="w", pady=(12, 0))

        def save():
            parsed = {}
            for key, (var, kind) in vars_.items():
                if kind == "bool":
                    parsed[key] = bool(var.get())
                    continue
                raw = var.get().strip()
                try:
                    value = int(raw) if kind == "int" else (
                        float(raw) if kind == "float" else raw)
                except ValueError:
                    messagebox.showerror("Settings",
                                         f"{raw!r} is not a valid number",
                                         parent=win)
                    return
                if kind == "int" and value < 1:
                    messagebox.showerror("Settings",
                                         f"{key} must be at least 1", parent=win)
                    return
                if key == "port" and not 1 <= value <= 65535:
                    messagebox.showerror("Settings", "port must be 1-65535",
                                         parent=win)
                    return
                parsed[key] = value

            was_running = self.engine.running
            try:
                if was_running:
                    self.engine.stop()
                self.engine.configure(**parsed)
            except (RuntimeError, ValueError) as exc:
                messagebox.showerror("Settings", str(exc), parent=win)
                return
            self._save_state()
            win.destroy()
            if was_running:
                self.status_var.set("Settings saved — restarting…")
                self._run_async(self.engine.start,
                                on_done=lambda: self.status_var.set("Restarted."))

        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=18, pady=(0, 16))
        ttk.Button(row, text="Cancel", style="Ghost.TButton",
                   command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Save", style="Accent.TButton",
                   command=save).pack(side="right")

    # ----------------------------------------------------------- apps dialog
    APP_LAYERS = (
        ("env", "Terminal & system apps",
         "curl, wget, Python and other GIO/GTK apps (Thunar, file managers, "
         "…) follow http_proxy. New logins inherit it from ~/.profile and "
         "~/.xsessionrc."),
        ("firefox", "Firefox",
         "Writes the proxy into every Firefox profile's user.js. Restart "
         "Firefox to pick it up; running ./proxyctl firefox off reverts it."),
        ("browsers", "Brave / Chromium",
         "Adds a '<browser> (via rotating proxy)' launcher to your menu that "
         "starts the browser with --proxy-server, plus its New Window and "
         "Incognito actions."),
    )

    def _open_apps(self):
        """Point browsers, the terminal and system apps at this proxy."""
        if self.apps_win and self.apps_win.winfo_exists():
            self.apps_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Point apps at this proxy")
        win.configure(bg=BG)
        win.geometry("640x545")
        win.transient(self)
        win.grab_set()
        self.apps_win = win

        proxyctl.STATE_FILE = STATE_FILE        # follow this panel's port

        on_fns = {"env": proxyctl.env_on, "firefox": proxyctl.firefox_on,
                  "browsers": proxyctl.browsers_on}
        off_fns = {"env": proxyctl.env_off, "firefox": proxyctl.firefox_off,
                   "browsers": proxyctl.browsers_off}
        status_fns = {"env": proxyctl.env_status,
                      "firefox": proxyctl.firefox_status,
                      "browsers": proxyctl.browsers_status}

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 6))

        # ---- proxy address header ------------------------------------
        url = proxyctl.proxy_url()
        listening = proxyctl.is_listening()
        tk.Label(body, text="PROXY ADDRESS", bg=BG, fg=MUTED,
                 font=F["heading"]).pack(anchor="w")
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x", pady=(3, 12))
        tk.Label(top, text=url, bg=BG, fg=ACCENT,
                 font=F["mono"]).pack(side="left")
        tk.Label(top,
                 text="● accepting connections" if listening
                      else "● not running — press Start",
                 bg=BG, fg=GREEN if listening else RED,
                 font=F["small"]).pack(side="left", padx=(14, 0))

        # ---- one row per layer ---------------------------------------
        statuses: dict[str, tk.Label] = {}

        def refresh():
            for key, lab in statuses.items():
                try:
                    text = status_fns[key]()
                except Exception as exc:                    # pragma: no cover
                    text = f"error: {exc}"
                lab.configure(text=text,
                              fg=GREEN if text.startswith("on") else MUTED)

        names = {key: title for key, title, _ in self.APP_LAYERS}

        def apply(key: str, want: bool, announce: bool = True):
            try:
                (on_fns[key] if want else off_fns[key])()
            except Exception as exc:
                messagebox.showerror("Point apps", str(exc), parent=win)
                return False
            refresh()
            if announce:
                self.status_var.set(f"{names[key]} turned "
                                    f"{'on' if want else 'off'}.")
            return True

        for key, title, blurb in self.APP_LAYERS:
            row = tk.Frame(body, bg=PANEL_2, highlightbackground=BORDER,
                           highlightthickness=1)
            row.pack(fill="x", pady=(0, 9))

            left = tk.Frame(row, bg=PANEL_2)
            left.pack(side="left", fill="x", expand=True, padx=13, pady=10)
            tk.Label(left, text=title, bg=PANEL_2, fg=TEXT,
                     font=F["bold"]).pack(anchor="w")
            tk.Label(left, text=blurb, bg=PANEL_2, fg=MUTED, font=F["small"],
                     justify="left", wraplength=340).pack(anchor="w",
                                                          pady=(3, 0))

            right = tk.Frame(row, bg=PANEL_2)
            right.pack(side="right", padx=13, pady=10)
            status = tk.Label(right, text="", bg=PANEL_2, fg=MUTED,
                              font=F["small"])
            status.pack(anchor="e")
            statuses[key] = status
            btns = tk.Frame(right, bg=PANEL_2)
            btns.pack(anchor="e", pady=(5, 0))
            _btn(btns, "On", lambda k=key: apply(k, True), "Go",
                 width=5).pack(side="left", padx=(0, 5))
            _btn(btns, "Off", lambda k=key: apply(k, False), "Halt",
                 width=5).pack(side="left")

        # ---- bulk actions --------------------------------------------
        tk.Label(body,
                 text=("The environment layer only affects *new* sessions.\n"
                       "To switch the shell you are in right now, run:"),
                 bg=BG, fg=MUTED, font=F["small"],
                 justify="left").pack(anchor="w", pady=(4, 0))

        cmd = f". {proxyctl.home_hint(proxyctl.ENV_FILE)}"
        cmdrow = tk.Frame(body, bg=PANEL_2, highlightbackground=BORDER,
                          highlightthickness=1)
        cmdrow.pack(fill="x", pady=(5, 0))
        tk.Label(cmdrow, text=cmd, bg=PANEL_2, fg=CYAN, font=F["mono"]
                 ).pack(side="left", padx=10, pady=7)

        def copy_cmd():
            self.clipboard_clear()
            self.clipboard_append(cmd)
            self.status_var.set("Command copied to clipboard")

        _btn(cmdrow, "Copy", copy_cmd, "Ghost").pack(side="right", padx=8,
                                                     pady=4)

        def apply_all(want: bool):
            for key, _, _ in self.APP_LAYERS:
                if not apply(key, want, announce=False):
                    return
            self.status_var.set("All layers turned "
                                f"{'on' if want else 'off'}.")

        # ---- footer ---------------------------------------------------
        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=18, pady=(6, 16))
        ttk.Button(foot, text="Close", style="Ghost.TButton",
                   command=win.destroy).pack(side="right", padx=(6, 0))
        _btn(foot, "Take everything off", lambda: apply_all(False),
             "Ghost").pack(side="right", padx=6)
        _btn(foot, "Point everything at this proxy", lambda: apply_all(True),
             "Accent").pack(side="right")

        refresh()

        # centre the dialog over the main window
        win.update_idletasks()
        win.geometry("+{}+{}".format(
            max(self.winfo_rootx()
                + (self.winfo_width() - win.winfo_width()) // 2, 0),
            max(self.winfo_rooty() + 30, 0)))

    def _about(self):
        messagebox.showinfo(
            "About Rotating Proxy",
            "Rotating Proxy\n\n"
            "A local HTTP/HTTPS proxy that rotates every connection across a "
            "pool of upstream proxies.\n\n"
            f"Configured upstreams: {self.engine.snapshot()['total']}\n"
            "State file: " + STATE_FILE.name,
            parent=self)

    # ------------------------------------------------------------- shutdown
    def _on_close(self):
        if self._closing:                 # Quit, SIGTERM and the X button can
            return                        # all land here -- save exactly once
        self._closing = True
        try:
            self._save_state()
        finally:
            if self.engine.running:
                threading.Thread(target=self.engine.stop, daemon=True).start()
            try:
                self.destroy()
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------
def _duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def _live_badge(n: dict) -> str:
    """"Alive · Strong", or the verification flag that outranks it.

    The Status column buys about 14 characters, so a flag replaces the
    tier rather than growing the string: knowing that traffic leaves from
    somewhere else (or from a network services already distrust) is worth
    more than the tier, which the detail line still carries.
    """
    if n.get("mismatch"):
        return "Alive · geo≠"          # traffic leaves from another country
    risk = n.get("risk") or []
    if "hosting" in risk and "vpn" in risk:
        return "Alive · DC+VPN"        # datacentre/cloud address
    if "hosting" in risk:
        return "Alive · DC"
    if "vpn" in risk:
        return "Alive · VPN"
    return f"Alive · {n.get('strength', 'New')}"


def _flags_of(n: dict) -> tuple[str, ...]:
    """Every verification flag a row carries, for colour and filtering."""
    flags = []
    if n.get("mismatch"):
        flags.append("geo")
    if "hosting" in (n.get("risk") or []):
        flags.append("dc")
    if "vpn" in (n.get("risk") or []):
        flags.append("vpn")
    return tuple(flags)


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    return f"{int(delta // 3600)}h ago"


def _ip_key(label: str):
    """Sort key that orders addresses numerically, so 10.0.0.2 comes before
    10.0.0.10 -- a plain string sort puts them the wrong way round.

    Hostnames and unparseable labels fall back to text order, and every
    branch returns the same shape so the keys stay comparable."""
    try:
        _proto, host, port = parse_entry(label)
    except (ValueError, TypeError):
        return (1, 0, 0, 0, 0, 0, label)
    octets = host.split(".")
    if len(octets) == 4 and all(o.isdigit() for o in octets):
        nums = tuple(int(o) for o in octets)
        if all(0 <= o <= 255 for o in nums):
            return (0, *nums, port, label)
    return (1, 0, 0, 0, 0, port, label)


def _country_of(node: dict) -> str:
    """\"Germany (DE)\" / \"unknown\" for a node dict from `engine.nodes()`."""
    name = node.get("country") or ""
    cc = node.get("cc") or ""
    if cc:
        return f"{name} ({cc})" if name else cc
    return name or "unknown"


def main() -> int:
    app = ProxyGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
