#!/usr/bin/env python3
"""
Rotating Proxy — desktop control panel (Tkinter).

A light, card-based dashboard in the style of the commercial proxy
managers (GoLogin et al.): white panels on a light background, a left
navigation rail with the connection controls, blue primary actions and
colour-coded status — while every feature of the engine stays reachable:

  * start / stop / restart the local proxy
  * live stats: served, active, failures, pool health, uptime, traffic
  * pool table with per-upstream status, strength tier, latency, region,
    hits and last error — filterable by status, region and free text
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
from engine import DEFAULTS, RotatingProxy, parse_entry, region_of
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
    """One big number with a caption, drawn as a white card."""

    def __init__(self, parent, caption: str):
        super().__init__(parent, bg=PANEL, padx=16, pady=11,
                         highlightbackground=BORDER, highlightthickness=1,
                         highlightcolor=ACCENT)
        self.value = tk.Label(self, text="—", bg=PANEL, fg=TEXT, font=F["big"])
        self.caption = tk.Label(self, text=caption.upper(), bg=PANEL, fg=MUTED,
                                font=F["cap"])
        self.value.pack(anchor="w")
        self.caption.pack(anchor="w")

    def set(self, text: str, color: str = TEXT) -> None:
        self.value.configure(text=text, fg=color)


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
        self._region_var = tk.StringVar(value="All regions")
        self._region_choices: tuple = ("All regions",)
        self._country_var = tk.StringVar(value="Anywhere")
        self._country_values: dict[str, str] = {"Anywhere": ""}
        self._country_choices: tuple = ()
        self._country_names: dict[str, str] = {}   # ISO code -> display name
        self._https_var = tk.BooleanVar(value=False)
        self._level_var = tk.StringVar(value="Info & up")
        self._auto_scroll = tk.BooleanVar(value=True)
        self._closing = False

        _init_fonts(self)

        settings, proxies = self._load_state()
        # restored before the widgets are built, so the checkbox and the
        # "Exit via" picker open showing what the engine actually does
        self._https_var.set(bool(settings.get("https_only")))
        self.engine = RotatingProxy(logger=self._on_engine_log,
                                    proxies=proxies, **settings)

        self._build_styles()
        self._build_menu()
        self._build_sidebar()
        self._main = tk.Frame(self, bg=BG)
        self._main.pack(side="left", fill="both", expand=True)
        self._build_cards()
        self._build_body()
        self._build_statusbar()
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

        # ---- exit country: where traffic leaves, not just what we show ----
        tk.Label(bar, text="Exit via", bg=PANEL_2, fg=MUTED,
                 font=F["small"]).pack(side="left", padx=(16, 4))
        self.country_combo = ttk.Combobox(
            bar, textvariable=self._country_var, width=20, state="readonly",
            values=["Anywhere"])
        self.country_combo.pack(side="left")
        self.country_combo.bind("<<ComboboxSelected>>", self._on_country_pick)

        # ---- only route through upstreams that can tunnel HTTPS ----------
        self.https_check = ttk.Checkbutton(
            bar, text="HTTPS only", variable=self._https_var,
            style="TCheckbutton", command=self._on_https_toggle)
        self.https_check.pack(side="left", padx=(14, 0))

        filt = tk.Frame(pool, bg=PANEL_2, padx=12, pady=7)
        filt.pack(fill="x")
        tk.Label(filt, text="Filter", bg=PANEL_2, fg=MUTED).pack(side="left")
        entry = ttk.Entry(filt, textvariable=self._filter_var, width=17)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda e: self._render_pool(force=True))
        combo = ttk.Combobox(filt, textvariable=self._status_var, width=10,
                             state="readonly",
                             values=["All", "Alive", "Dead", "Other", "HTTPS",
                                     "Strong", "Fast"])
        combo.pack(side="left", padx=(0, 6))
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_pool(force=True))
        tk.Label(filt, text="Region", bg=PANEL_2, fg=MUTED).pack(side="left",
                                                                 padx=(8, 4))
        self.region_combo = ttk.Combobox(filt, textvariable=self._region_var,
                                         width=15, state="readonly",
                                         values=list(self._region_choices))
        self.region_combo.pack(side="left")
        self.region_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._render_pool(force=True))
        self.pool_count = tk.Label(filt, text="", bg=PANEL_2, fg=MUTED,
                                   anchor="e")
        self.pool_count.pack(side="right", padx=(10, 0))

        cols = ("proxy", "kind", "country", "status", "latency", "checked",
                "hits", "error")
        self.tree = ttk.Treeview(pool, columns=cols, show="headings",
                                 selectmode="extended")
        # widths sum to 656px -- checked against the real font metrics, so
        # every heading and badge fits even at the window's minimum size
        # (the proxy and error columns stretch to fill the rest)
        headings = {"proxy": ("Proxy", 130, "w"), "kind": ("Type", 62, "center"),
                    "country": ("Country", 68, "center"),
                    "status": ("Status", 104, "center"),
                    "latency": ("Latency", 66, "e"),
                    "checked": ("Last check", 84, "center"),
                    "hits": ("Served", 60, "e"),
                    "error": ("Last error", 82, "w")}
        for col, (title, width, anchor) in headings.items():
            self.tree.heading(col, text=title,
                              command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor,
                             minwidth=44, stretch=(col in ("proxy", "error")))
        # healthy rows stay neutral like the commercial tables; only
        # trouble (dead/checking) and the zebra striping get a colour
        for tag, colour in (("alive", TEXT), ("dead", RED),
                            ("checking", AMBER), ("unknown", TEXT),
                            ("zebra", "#fafbfd")):
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

        # ---- the activity card underneath -------------------------------
        nb_card = tk.Frame(body, bg=PANEL, highlightbackground=BORDER,
                           highlightthickness=1, height=215)
        nb_card.pack(fill="x")
        nb_card.pack_propagate(False)

        self.notebook = ttk.Notebook(nb_card)
        self.notebook.pack(fill="both", expand=True)

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
            return latency is not None and latency <= 300
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
        return lambda n: _ip_key(n["label"])   # proxy / fallback

    def _render_pool(self, force: bool = False):
        nodes = self.engine.nodes()
        needle = self._filter_var.get().strip().lower()
        region = self._region_var.get()
        visible = [n for n in nodes
                   if self._status_filter_ok(n)
                   and (region in ("", "All regions")
                        or region_of(n.get("cc", "")) == region)
                   and (not needle or needle in n["label"].lower()
                        or needle in n.get("kind", "").lower()
                        or needle in n.get("country", "").lower()
                        or needle in n.get("cc", "").lower()
                        or needle in n.get("region", "").lower()
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
                tags = (n["status"], "zebra") if idx % 2 else (n["status"],)
                self.tree.insert("", "end", iid=n["label"],
                                 values=self._row_values(n), tags=tags)
            self._row_labels = labels
            keep = [l for l in labels if l in selected]
            if keep:
                self.tree.selection_set(keep)
        else:
            # same rows, same order -> cheap in-place value refresh
            for idx, n in enumerate(visible):
                values = self._row_values(n)
                if self.tree.item(n["label"], "values") != values:
                    tags = (n["status"], "zebra") if idx % 2 else (n["status"],)
                    self.tree.item(n["label"], values=values, tags=tags)

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
        code = self.engine.country
        if code:
            tally.append(f"exit {code}")
        if self.engine.https_only:
            tally.append("HTTPS only")
        self.pool_count.configure(text=" · ".join(tally))
        if force:
            self.tree.update_idletasks()

    def _refresh_countries(self, snap: dict):
        """Rebuild the "Exit via" picker from the countries the pool has."""
        scope = snap.get("pool_countries") or {}
        choices = [("", "Anywhere")]
        for e in sorted(scope.values(),
                        key=lambda v: (-int(v.get("alive", 0)),
                                       -int(v.get("total", 0)),
                                       str(v.get("name") or v["cc"]).lower())):
            cc = e["cc"]
            name = e.get("name") or cc
            choices.append((cc, f"{name} ({cc}) · "
                                f"{e.get('alive', 0)}/{e.get('total', 0)}"))
        labels = [text for _, text in choices]
        if tuple(labels) != self._country_choices:
            self._country_choices = tuple(labels)
            self._country_values = {text: cc for cc, text in choices}
            self.country_combo.configure(values=labels)
        names = {cc: (e.get("name") or cc) for cc, e in scope.items()}
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
        """Rebuild the Region filter from the areas the pool covers."""
        scope = snap.get("pool_countries") or {}
        present = sorted({region_of(cc) for cc in scope} - {""},
                         key=str.lower)
        values = ["All regions"] + present
        if tuple(values) != self._region_choices:
            self._region_choices = tuple(values)
            current = self._region_var.get()
            if current not in values:
                # keep a stale pick visible rather than silently widening
                # the filter -- the countries it covered really are gone
                values = values + [current]
                self._region_choices = tuple(values)
            self.region_combo.configure(values=values)

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
        self.status_var.set(f"Exit country: "
                            f"{self._country_var.get()}")
        self._refresh_countries(self.engine.snapshot())
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
        self.status_var.set("HTTPS-only routing is "
                            f"{'on' if self.engine.https_only else 'off'}.")
        self._render_pool(force=True)

    @staticmethod
    def _row_values(n: dict) -> tuple[str, ...]:
        """All strings -- Tk stores/displays everything as text, and keeping
        the types stable lets us cheap-compare rows for changes.

        The status cell doubles as the strength badge: "Alive · Strong" is
        the tier users actually filter and sort on, and the Treeview can
        only colour whole rows, not individual cells."""
        latency = "—" if n["latency"] is None else f"{n['latency']:.0f} ms"
        status = n["status"]
        if status == "alive":
            badge = f"Alive · {n.get('strength', 'New')}"
        elif status == "checking":
            badge = "Checking…"
        elif status == "dead":
            badge = "Dead"
        else:
            badge = "Unverified"
        return (n["label"], n.get("kind", "HTTP"),
                n.get("cc") or "—", badge, latency,
                _ago(n["last_check"]), str(n["hits"]),
                (n["last_error"] or "")[:70])

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
            if n.get("blocks"):
                parts.append(f"blocked by targets: {n['blocks']}×")
            if n["last_error"]:
                parts.append(f"last error: {n['last_error']}")
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

        # traffic history
        rate = snap["served"] - self._prev_served
        self._prev_served = snap["served"]
        self._hist.append((max(rate, 0), snap["active"]))

        self._update_buttons()
        self._refresh_countries(snap)
        self._refresh_regions(snap)

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
    def _open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG)
        win.geometry("430x455")
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

        note = ("Settings apply the next time you start the proxy.\n"
                "The port change always requires a restart.\n"
                "“Rotate exit on status” is a comma-separated list of the\n"
                "statuses that make a request retry from another exit IP\n"
                "(429/403 blocks; empty switches it off).")
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
