#!/usr/bin/env python3
"""
GUI smoke tests.

Exercises the Tkinter panel without a human: builds the window, drives its
poll loop, checks the pool table / log / stat cards / dialogs, and pushes
real traffic through a started engine.

    python3 test_gui.py
"""

import socket
import sys
import time
import traceback
from pathlib import Path

from test_engine import start_mock, start_target, _free_port

import gui
from gui import ProxyGUI

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))


def pump(app, ticks=6, delay=30):
    """Let real time pass so Tk's `after` timers fire, then process events."""
    for _ in range(ticks):
        time.sleep(delay / 1000)
        try:
            app.update()
        except Exception:
            break


def wait_until(predicate, app, timeout=12.0, ticks=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(app, ticks)
        if predicate():
            return True
    return False


def http_get(port, host, path, timeout=8):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(f"GET http://{host}{path} HTTP/1.1\r\nHost: {host}\r\n"
                  f"Connection: close\r\n\r\n".encode())
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


def test_construction():
    print("\nwindow construction")
    app = ProxyGUI(auto_start=False)
    pump(app)
    check("window created", app.winfo_exists())
    check("title", app.title() == "Rotating Proxy", app.title())
    check("stat cards built", len(app.cards) == 6, str(list(app.cards)))
    check("pool table built", app.tree.winfo_exists())
    check("log widget built", app.log_text.winfo_exists())
    check("notebook tabs", app.notebook.index("end") == 2,
          str(app.notebook.tabs()))
    check("endpoint hint shown", "127.0.0.1:8888" in app.endpoint_var.get(),
          app.endpoint_var.get())
    check("stopped pill", "STOPPED" in app.pill.cget("text"),
          app.pill.cget("text"))
    check("start enabled", str(app.btn_start["state"]) == "normal")
    check("stop disabled", str(app.btn_stop["state"]) == "disabled")
    return app


def test_pool_render(app):
    print("\npool table")
    app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 41)])
    pump(app, ticks=3)
    rows = app.tree.get_children()
    check("40 rows rendered", len(rows) == 40, str(len(rows)))
    check("count label", app.pool_count.cget("text").endswith("/40 alive"),
          app.pool_count.cget("text"))
    first = app.tree.item(rows[0], "values")
    check("row shape", len(first) == 8, str(first))
    check("default type column", first[1] == "HTTP", str(first[1]))
    check("private ip has no country", first[2] == "—", str(first[2]))

    # text filtering (substring match on proxy or error)
    app._filter_var.set("10.0.0.7")
    app._render_pool(force=True)
    check("filter '10.0.0.7' -> 1 row", len(app.tree.get_children()) == 1,
          str(len(app.tree.get_children())))
    app._filter_var.set("10.0.0.1")
    app._render_pool(force=True)
    check("filter '10.0.0.1' -> 11 rows (.1, .10-.19)",
          len(app.tree.get_children()) == 11,
          str(len(app.tree.get_children())))
    app._filter_var.set("")
    app._render_pool(force=True)

    # status filter (nothing marked yet -> only 'Other' should match)
    app._status_var.set("Alive")
    app._render_pool(force=True)
    check("status filter hides unknown", len(app.tree.get_children()) == 0,
          str(len(app.tree.get_children())))
    app._status_var.set("All")
    app._render_pool(force=True)

    # sorting -- addresses order numerically, not as text
    def ip_tuple(label):
        return tuple(int(p) for p in label.split(":")[0].split("."))

    app._sort_by("proxy")
    labels = [app.tree.item(i, "values")[0] for i in app.tree.get_children()]
    ips = [ip_tuple(l) for l in labels]
    check("ascending sort is numeric", ips == sorted(ips), labels[:3])
    check("2 sorts before 10",
          labels.index("10.0.0.2:8080") < labels.index("10.0.0.10:8080"),
          str(labels[:5]))
    app._sort_by("proxy")
    back = [app.tree.item(i, "values")[0] for i in app.tree.get_children()]
    check("descending sort reverses it", back == labels[::-1], back[:3])
    app._sort_by("proxy")
    app._sort_by("status")     # equal ranks tiebreak on the address
    labels = [app.tree.item(i, "values")[0] for i in app.tree.get_children()]
    check("status ties break numerically",
          labels.index("10.0.0.2:8080") < labels.index("10.0.0.10:8080"),
          str(labels[:6]))
    check("ties order from the first address", labels[0] == "10.0.0.1:8080",
          str(labels[:3]))

    # scheme labels + the Type column
    app.engine.set_proxies(["10.0.0.1:8080", "socks4://10.0.0.2:1080",
                            "socks5://10.0.0.3:1080"])
    app._filter_var.set("")
    app._render_pool(force=True)
    kinds = sorted(app.tree.item(i, "values")[1] for i in app.tree.get_children())
    check("type column filled", kinds == ["HTTP", "SOCKS4", "SOCKS5"], str(kinds))
    labels = [app.tree.item(i, "values")[0] for i in app.tree.get_children()]
    check("schemes preserved", "socks4://10.0.0.2:1080" in labels, str(labels))
    app._filter_var.set("socks4")
    app._render_pool(force=True)
    check("filter by type", len(app.tree.get_children()) == 1,
          str(len(app.tree.get_children())))

    app._filter_var.set("")
    app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 41)])
    app._render_pool(force=True)


def test_country(app):
    print("\nexit country picker")
    import geodb
    real = geodb.lookup
    GEO = {"14.136.67.106": ("HK", "Hong Kong"),
           "8.8.8.8": ("US", "United States"),
           "41.220.16.209": ("ZW", "Zimbabwe")}
    geodb.lookup = lambda h: GEO.get(h, ("", ""))
    try:
        app.engine.set_proxies(["14.136.67.106:1080", "8.8.8.8:8080",
                                "41.220.16.209:80", "10.0.0.5:8080"])
        pump(app, ticks=3)
        rows = {app.tree.item(i, "values")[0]: app.tree.item(i, "values")
                for i in app.tree.get_children()}
        check("country column filled", rows["14.136.67.106:1080"][2] == "HK",
              str(rows))
        check("unknown country shown as em dash",
              rows["10.0.0.5:8080"][2] == "—", str(rows["10.0.0.5:8080"]))

        snap = app.engine.snapshot()
        tally = {k: v["total"] for k, v in snap["pool_countries"].items()}
        check("country tally in snapshot", tally == {"HK": 1, "US": 1, "ZW": 1},
              str(tally))
        check("picker refreshed by the poll",
              len(app._country_choices) == 4, str(app._country_choices))
        check("picker starts on Anywhere",
              app._country_var.get() == "Anywhere", app._country_var.get())

        hk = next(text for text, code in app._country_values.items()
                  if code == "HK")
        app._country_var.set(hk)
        app._on_country_pick()
        check("restriction applied", app.engine.country == "HK",
              str(app.engine.country))
        check("picker keeps the choice", app._country_var.get() == hk,
              app._country_var.get())

        # the whole point of the fix: a restart must come back as HK,
        # so the pick has to hit the disk immediately, not on close
        import json
        saved = json.loads(Path(gui.STATE_FILE).read_text(encoding="utf-8"))
        check("pick written to the state file right away",
              saved["settings"].get("country") == "HK",
              str(saved["settings"].get("country")))

        for n in app.engine._nodes:
            n.status = "alive"
        check("candidates scoped to the pick",
              [n.label for n in app.engine._candidates()]
              == ["14.136.67.106:1080"],
              str([n.label for n in app.engine._candidates()]))
        app._render_pool(force=True)
        check("tally mentions the restriction",
              "exit" in app.pool_count.cget("text"),
              app.pool_count.cget("text"))

        app._filter_var.set("hong kong")
        app._render_pool(force=True)
        check("filter by country name", len(app.tree.get_children()) == 1,
              str(len(app.tree.get_children())))
        app._filter_var.set("")
        app._render_pool(force=True)

        app._sort_by("country")
        order = [app.tree.item(i, "values")[2]
                 for i in app.tree.get_children()]
        check("sort by country column", order == ["HK", "US", "ZW", "—"],
              str(order))

        app._country_var.set("Anywhere")
        app._on_country_pick()
        check("restriction cleared", app.engine.country == "",
              str(app.engine.country))
        check("candidates restored", len(app.engine._candidates()) == 4,
              str(len(app.engine._candidates())))

        # a scope whose country has left the pool must still be displayed,
        # never silently reported as "Anywhere" while the engine restricts
        app.engine.settings["country"] = "XX"
        pump(app, ticks=3)
        check("stale scope shown honestly",
              app._country_var.get() != "Anywhere"
              and "XX" in app._country_var.get(),
              app._country_var.get())
        app.engine.settings["country"] = ""
        app._refresh_countries(app.engine.snapshot())
    finally:
        geodb.lookup = real
        app.engine.set_country("")
        app._sort = ("status", False)
        app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 41)])
        app._render_pool(force=True)


def test_https_toggle(app):
    print("\nHTTPS-only toggle")
    import json
    app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 6)])
    for i, n in enumerate(app.engine._nodes):
        n.status = "alive"
        n.connect_ok = (i % 2 == 0)          # 3 tunnel, 2 do not
    try:
        check("starts off", app._https_var.get() is False
              and app.engine.https_only is False)
        check("checkbox shows off",
              not app.https_check.instate(["selected"]))

        app._https_var.set(True)
        app._on_https_toggle()
        check("toggle reaches the engine", app.engine.https_only is True)
        check("checkbox follows", app.https_check.instate(["selected"]))
        pick = [n.label for n in app.engine._candidates()]
        check("rotation drops non-tunnellers",
              len(pick) == 3 and all(
                  n["connect_ok"] for n in app.engine.nodes()
                  if n["label"] in pick), str(pick))

        saved = json.loads(Path(gui.STATE_FILE).read_text(encoding="utf-8"))
        check("toggle written to the state file",
              saved["settings"].get("https_only") is True,
              str(saved["settings"].get("https_only")))
        app._render_pool(force=True)
        check("tally shows the restriction",
              "HTTPS only" in app.pool_count.cget("text"),
              app.pool_count.cget("text"))

        # the status dropdown can show just the tunnellers
        app._status_var.set("HTTPS")
        app._render_pool(force=True)
        check("HTTPS view filter", len(app.tree.get_children()) == 3,
              str(len(app.tree.get_children())))
        app._status_var.set("All")
        app._render_pool(force=True)

        app._https_var.set(False)
        app._on_https_toggle()
        check("toggle off", app.engine.https_only is False
              and not app.https_check.instate(["selected"]))
        check("candidates restored", len(app.engine._candidates()) == 5,
              str(len(app.engine._candidates())))
        saved = json.loads(Path(gui.STATE_FILE).read_text(encoding="utf-8"))
        check("off is persisted too",
              saved["settings"].get("https_only") is False,
              str(saved["settings"].get("https_only")))
    finally:
        app._https_var.set(False)
        app.engine.set_https_only(False)
        app._status_var.set("All")
        app._render_pool(force=True)


def test_log(app):
    print("\nlog panel")
    pump(app, ticks=3)          # flush anything earlier tests queued
    app._clear_log()
    for level in ("debug", "info", "warn", "error"):
        app._on_engine_log(level, f"message {level}")
    pump(app, ticks=3)
    body = app.log_text.get("1.0", "end")
    check("debug hidden by default", "message debug" not in body, body[:120])
    check("info shown", "message info" in body)
    check("warn shown", "message warn" in body)
    check("error shown", "message error" in body)
    check("coloured tags", "error" in app.log_text.tag_names())
    check("buffer retained", len(app._log_lines) == 4, str(len(app._log_lines)))

    app._level_var.set("Errors only")
    app._rebuild_log()
    body = app.log_text.get("1.0", "end")
    check("level filter rebuilds", "message error" in body and
          "message info" not in body, body[:120])

    app._level_var.set("All")
    app._rebuild_log()
    body = app.log_text.get("1.0", "end")
    check("show-all restores debug", "message debug" in body)

    app._clear_log()
    app._level_var.set("Info & up")


def test_dialogs(app):
    print("\ndialogs")
    app._open_settings()
    pump(app, ticks=4)
    win = app.settings_win
    check("settings opens", win is not None and win.winfo_exists())
    check("settings has 8 fields", len(win.winfo_children()) >= 1)
    spec_keys = [k for k, _, _ in gui.SETTINGS_SPEC]
    check("settings lists the list-refresh options",
          "refresh_url" in spec_keys and "refresh_interval" in spec_keys,
          str(spec_keys))
    win.destroy()
    pump(app, ticks=2)

    app._open_add()
    pump(app, ticks=4)
    add = app.add_win
    check("add dialog opens", add is not None and add.winfo_exists())
    add.destroy()
    pump(app, ticks=2)


def test_every_button(app):
    """Nothing on the panel may be a dead control: every menu entry has a
    command wired to it, and the safe ones are actually fired here."""
    print("\nevery button")
    import tempfile
    from tkinter import filedialog, messagebox

    work = Path(tempfile.mkdtemp(prefix="proxgui-"))
    src = work / "import.txt"
    src.write_text("192.0.2.55:8080\n192.0.2.56:8080\n", encoding="utf-8")
    out = work / "export.txt"

    fired: list = []
    real = (messagebox.showinfo, messagebox.showerror, messagebox.askyesno,
            filedialog.asksaveasfilename, filedialog.askopenfilename)

    def note(title):
        def call(*_a, **_k):
            fired.append(title)
            return True
        return call

    messagebox.showinfo = note("showinfo")
    messagebox.showerror = note("showerror")
    messagebox.askyesno = note("askyesno")
    filedialog.asksaveasfilename = lambda **_k: str(out)
    filedialog.askopenfilename = lambda **_k: str(src)

    try:
        # ---- menu bar: every entry must carry a real command ------------
        menubar = app.nametowidget(str(app["menu"]))
        wired, skipped = {}, []
        for i in range(menubar.index("end") + 1):
            if menubar.type(i) != "cascade":
                continue
            label = menubar.entrycget(i, "label")
            sub = menubar.nametowidget(menubar.entrycget(i, "menu"))
            for j in range(sub.index("end") + 1):
                if sub.type(j) != "command":
                    continue
                name = sub.entrycget(j, "label")
                wired[f"{label}/{name}"] = bool(sub.entrycget(j, "command"))
                skipped.append(f"{label}/{name}")
        check("menu entries found", len(wired) >= 9, str(sorted(wired)))
        check("every menu entry is wired",
              all(wired.values()),
              str([k for k, v in wired.items() if not v]))

        # ---- fire the ones that are safe to run headless ----------------
        def invoke(cascade, entry):
            for i in range(menubar.index("end") + 1):
                if menubar.type(i) != "cascade":
                    continue
                sub = menubar.nametowidget(menubar.entrycget(i, "menu"))
                if menubar.entrycget(i, "label") != cascade:
                    continue
                for j in range(sub.index("end") + 1):
                    if (sub.type(j) == "command"
                            and sub.entrycget(j, "label") == entry):
                        # no Menu.entryinvoke in this Tkinter: ask Tcl
                        sub.tk.call(str(sub), "invoke", j)
                        return True
            return False

        before = len(app.engine.proxies())
        check("File/Export fires", invoke("File", "Export alive proxies…"))
        check("export writes a file",
              out.exists() and out.read_text(encoding="utf-8").strip() != "",
              str(out))
        check("File/Import fires", invoke("File", "Import proxies…"))
        check("import adds upstreams",
              len(app.engine.proxies()) == before + 2,
              f"{before} -> {len(app.engine.proxies())}")

        check("Help/About fires", invoke("Help", "About"))
        check("about asked for a dialog", "showinfo" in fired, str(fired))

        check("Proxy/Settings fires", invoke("Proxy", "Settings…"))
        pump(app, ticks=3)
        check("settings window opened",
              app.settings_win is not None and app.settings_win.winfo_exists())
        app.settings_win.destroy()
        pump(app, ticks=2)

        app.engine.configure(refresh_url="")     # no network in tests
        check("Proxy/Refresh list fires",
              invoke("Proxy", "Refresh proxy list now"))
        check("refresh without a URL points at Settings",
              "Settings" in app.status_var.get(), app.status_var.get())

        check("Proxy/Point apps fires",
              invoke("Proxy", "Point apps at this proxy…"))
        pump(app, ticks=3)
        check("apps window opened",
              app.apps_win is not None and app.apps_win.winfo_exists())
        # one block per layer: env, Firefox, browsers
        body = app.apps_win.winfo_children()[0]
        check("apps dialog built", len(body.winfo_children()) >= 5,
              str(len(body.winfo_children())))
        app.apps_win.destroy()
        pump(app, ticks=2)
        check("Quit left alone on purpose",
              "File/Quit" in wired and wired["File/Quit"])

        # ---- log bar ----------------------------------------------------
        for level in ("All", "Info & up", "Warnings & up", "Errors only"):
            app._level_var.set(level)
            app._rebuild_log()
        check("all four log levels render", True)
        app._level_var.set("Info & up")
        app._rebuild_log()

        app._on_engine_log("info", "line to copy")
        pump(app, ticks=3)
        app._copy_log()
        check("copy log fills the clipboard",
              "line to copy" in app.clipboard_get(), "empty clipboard")
        app._clear_log()
        app._auto_scroll.set(False)
        app._rebuild_log()
        check("auto-scroll toggles", app._auto_scroll.get() is False)
        app._auto_scroll.set(True)

        # ---- notebook + graph -------------------------------------------
        app.notebook.select(1)
        pump(app, ticks=3)
        check("traffic tab draws",
              len(app.graph.find_all()) > 0, str(app.graph.find_all()[:4]))
        app.notebook.select(0)
        pump(app, ticks=2)

        # ---- pool controls ----------------------------------------------
        check("Delete key bound",
              bool(app.tree.bind("<Delete>")), str(app.tree.bind("<Delete>")))
        for choice in ("All", "Alive", "Dead", "Other", "HTTPS"):
            app._status_var.set(choice)
            app._render_pool(force=True)
        check("all five status filters render", True)
        app._status_var.set("All")
        app._render_pool(force=True)

        for col in ("proxy", "kind", "country", "status", "latency",
                    "checked", "hits", "error"):
            app._sort_by(col)
            app._sort_by(col)          # and the reverse direction
            check(f"sort by {col}", len(app.tree.get_children()) > 0,
                  str(len(app.tree.get_children())))
        app._sort = ("status", False)

        rows = app.tree.get_children()
        if rows:
            app.tree.selection_set(rows[0])
            app._on_select()
            check("single selection shows details",
                  app.detail.cget("text") != "Select an upstream to see details",
                  app.detail.cget("text")[:60])
            if len(rows) > 1:
                app.tree.selection_set(*rows[:2])
                app._on_select()
                check("multi selection counted",
                      "upstreams selected" in app.detail.cget("text"),
                      app.detail.cget("text")[:60])
            app.tree.selection_set(rows[0])     # one row -> no confirmation
            app._remove_selected()
            check("remove fires", len(app.tree.get_children()) == len(rows) - 1,
                  str(len(app.tree.get_children())))
            pump(app, ticks=2)
    finally:
        messagebox.showinfo, messagebox.showerror, messagebox.askyesno = real[:3]
        (filedialog.asksaveasfilename,
         filedialog.askopenfilename) = real[3], real[4]
        app._status_var.set("All")
        app._filter_var.set("")
        app._sort = ("status", False)
        app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 41)])
        app._render_pool(force=True)
        for path in work.glob("*"):
            path.unlink(missing_ok=True)
        work.rmdir()


def test_categories(app):
    """Strength tiers, region filter and the category badges."""
    print("\nstrength / region / category filters")
    import geodb
    real = geodb.lookup
    geo = {"8.8.8.8": ("US", "United States"),
           "9.9.9.9": ("DE", "Germany"),
           "1.1.1.1": ("AU", "Australia")}
    geodb.lookup = lambda h: geo.get(h, ("", ""))
    try:
        app.engine.set_proxies(["8.8.8.8:8080", "9.9.9.9:8080",
                                "1.1.1.1:8080"])
        pump(app, ticks=8)
        by_label = {n.label: n for n in app.engine._nodes}
        strong = by_label["9.9.9.9:8080"]
        strong.status, strong.latency = "alive", 120.0
        strong.sample(1.0)
        strong.sample(1.0)
        weak = by_label["8.8.8.8:8080"]
        weak.status, weak.latency = "alive", 900.0
        weak.sample(0.0)
        weak.sample(0.0)
        weak.sample(0.0)
        # 1.1.1.1 stays unverified on purpose
        app._render_pool(force=True)

        def rows():
            return list(app.tree.get_children())

        badges = {iid: app.tree.item(iid, "values")[3]
                  for iid in rows()}
        check("status cell carries the strength badge",
              badges.get("9.9.9.9:8080") == "Alive · Strong",
              str(badges))
        check("unverified badge for fresh nodes",
              badges.get("1.1.1.1:8080") == "Unverified", str(badges))

        app._status_var.set("Strong")
        app._render_pool(force=True)
        check("Strong filter",
              rows() == ["9.9.9.9:8080"], str(rows()))
        app._status_var.set("Fast")
        app._render_pool(force=True)
        check("Fast filter (<=300 ms)", rows() == ["9.9.9.9:8080"],
              str(rows()))
        app._status_var.set("All")
        app._render_pool(force=True)

        check("region choices built from the pool",
              "Europe" in app._region_choices
              and "North America" in app._region_choices
              and "Oceania" in app._region_choices,
              str(app._region_choices))
        app._region_var.set("Europe")
        app._render_pool(force=True)
        check("region filter narrows the table",
              rows() == ["9.9.9.9:8080"], str(rows()))
        app._region_var.set("All regions")
        app._render_pool(force=True)

        app._filter_var.set("strong")
        app._render_pool(force=True)
        check("text filter matches the strength word",
              rows() == ["9.9.9.9:8080"], str(rows()))
        app._filter_var.set("oceania")
        app._render_pool(force=True)
        check("text filter matches the region word",
              rows() == ["1.1.1.1:8080"], str(rows()))
        app._filter_var.set("")
        app._render_pool(force=True)

        tally = app.pool_count.cget("text")
        check("tally shows the strong count", "strong" in tally, tally)

        app.tree.selection_set("9.9.9.9:8080")
        app._on_select()
        detail = app.detail.cget("text")
        check("detail line carries strength and region",
              "strength: Strong" in detail and "region: Europe" in detail,
              detail[:90])
    finally:
        geodb.lookup = real
        app._status_var.set("All")
        app._region_var.set("All regions")
        app._filter_var.set("")
        app._sort = ("status", False)
        app.engine.set_proxies([f"10.0.0.{i}:8080" for i in range(1, 41)])
        app._render_pool(force=True)


def test_engine_via_gui(app, target_port, mock_port):
    print("\nlifecycle driven from the UI")
    app.engine.set_proxies([f"127.0.0.1:{mock_port}"])
    app._render_pool(force=True)
    # use an ephemeral port so the test never fights a real listener
    app.engine.configure(port=_free_port())

    app._start()
    started = wait_until(lambda: app.engine.running, app, timeout=10)
    check("start command works", started)
    pump(app, ticks=6)
    check("running pill", "RUNNING" in app.pill.cget("text") or
          "CHECKING" in app.pill.cget("text"), app.pill.cget("text"))
    check("stop now enabled", str(app.btn_stop["state"]) == "normal")
    check("start now disabled", str(app.btn_start["state"]) == "disabled")

    port = app.engine.port
    try:
        raw = http_get(port, f"127.0.0.1:{target_port}", "/gui")
        check("serves real traffic", b"TARGET-GET /gui" in raw,
              raw[:80].decode(errors="replace"))
    except OSError as exc:
        check("serves real traffic", False, str(exc))

    wait_until(lambda: app.engine.snapshot()["served"] >= 1, app, timeout=6)
    served_text = app.cards["served"].value.cget("text")
    check("served card updated", served_text not in ("—", "0"), served_text)

    # health check button
    app._check_now()
    wait_until(lambda: not app.engine.checking, app, timeout=20)
    nodes = {n["label"]: n for n in app.engine.nodes()}
    label = f"127.0.0.1:{mock_port}"
    check("health check ran", nodes[label]["status"] in ("alive", "dead"),
          str(nodes[label]["status"]))

    app._stop()
    stopped = wait_until(lambda: not app.engine.running, app, timeout=10)
    check("stop command works", stopped)
    pump(app, ticks=6)
    check("stopped pill", "STOPPED" in app.pill.cget("text"),
          app.pill.cget("text"))
    check("start re-enabled", str(app.btn_start["state"]) == "normal")


def test_add_remove(app):
    print("\nadd / remove from the UI")
    before = len(app.engine.proxies())
    app.engine.add_proxies("203.0.113.10:8080\n203.0.113.11:8080, junk, 203.0.113.10:8080")
    app._render_pool(force=True)
    after = len(app.engine.proxies())
    check("two unique added", after == before + 2, f"{before} -> {after}")
    check("junk ignored", not any("junk" in p for p in app.engine.proxies()))
    app.engine.remove_proxies(["203.0.113.10:8080", "203.0.113.11:8080"])
    app._render_pool(force=True)
    check("removed again", len(app.engine.proxies()) == before)


def test_persistence(tmp_state):
    print("\nstate persistence")
    import json
    from engine import DEFAULTS

    class Host:
        """Minimal stand-in: _load_state only needs _coerce and the module
        global STATE_FILE, so we can test it without opening a window."""
        _coerce = staticmethod(ProxyGUI._coerce)

    host = Host()
    original = gui.STATE_FILE
    gui.STATE_FILE = tmp_state
    try:
        tmp_state.write_text(json.dumps({
            "settings": {"port": 9999, "check_interval": 30, "host": "127.0.0.1"},
            "proxies": ["192.0.2.1:80", "192.0.2.2:80"],
        }), encoding="utf-8")
        settings, proxies = ProxyGUI._load_state(host)
        check("port restored", settings["port"] == 9999, str(settings["port"]))
        check("interval restored", settings["check_interval"] == 30)
        check("proxies restored", proxies == ["192.0.2.1:80", "192.0.2.2:80"],
              str(proxies))

        # corrupt values must fall back to defaults, not break start-up
        tmp_state.write_text(json.dumps({
            "settings": {"port": "not-a-port", "check_interval": -5,
                         "host": "", "probe_timeout": "abc",
                         "unknown_key": 1},
            "proxies": ["ok:80", 12345],
        }), encoding="utf-8")
        settings, proxies = ProxyGUI._load_state(host)
        check("bad port falls back", settings["port"] == DEFAULTS["port"],
              str(settings["port"]))
        check("bad interval falls back",
              settings["check_interval"] == DEFAULTS["check_interval"],
              str(settings["check_interval"]))
        check("bad host falls back", settings["host"] == DEFAULTS["host"])
        check("bad timeout falls back",
              settings["probe_timeout"] == DEFAULTS["probe_timeout"])
        check("unknown key ignored", "unknown_key" not in settings)
        check("proxies coerced to str",
              all(isinstance(p, str) for p in proxies), str(proxies))

        # booleans survive (and reject) a hand-edited state file
        tmp_state.write_text(json.dumps({
            "settings": {"probe_connect": "off"}, "proxies": ["ok:80"],
        }), encoding="utf-8")
        settings, _ = ProxyGUI._load_state(host)
        check("bool parsed from text", settings["probe_connect"] is False,
              str(settings["probe_connect"]))
        tmp_state.write_text(json.dumps({
            "settings": {"probe_connect": "maybe"}, "proxies": ["ok:80"],
        }), encoding="utf-8")
        settings, _ = ProxyGUI._load_state(host)
        check("bad bool falls back", settings["probe_connect"] is True,
              str(settings["probe_connect"]))

        # exit country + HTTPS-only scope survive a restart
        tmp_state.write_text(json.dumps({
            "settings": {"country": "DE", "https_only": True},
            "proxies": ["ok:80"],
        }), encoding="utf-8")
        settings, _ = ProxyGUI._load_state(host)
        check("country restored", settings["country"] == "DE",
              str(settings["country"]))
        check("https_only restored", settings["https_only"] is True,
              str(settings["https_only"]))
        tmp_state.write_text(json.dumps({
            "settings": {"country": "not a code", "https_only": "banana"},
            "proxies": ["ok:80"],
        }), encoding="utf-8")
        settings, _ = ProxyGUI._load_state(host)
        check("bad scope values fall back",
              settings["country"] == "not a code"       # engine validates it
              and settings["https_only"] is False,
              f"{settings['country']!r} {settings['https_only']!r}")

        # a truncated file is tolerated
        tmp_state.write_text("{not json", encoding="utf-8")
        settings, proxies = ProxyGUI._load_state(host)
        check("corrupt file tolerated",
              settings["port"] == DEFAULTS["port"] and len(proxies) > 100)
    finally:
        gui.STATE_FILE = original
        tmp_state.unlink(missing_ok=True)


def main():
    gui.POLL_MS = 80          # drive the refresh loop fast enough to observe
    state_path = gui.STATE_FILE
    had_state = state_path.exists()
    backup = state_path.read_bytes() if had_state else None
    if had_state:
        # start from shipped defaults: a leftover exit country or
        # https_only from a real session must not leak into the checks
        state_path.unlink()

    target = start_target()
    target_port = target.server_address[1]
    mock_port = start_mock(alive=True)

    app = None
    try:
        app = test_construction()
        test_pool_render(app)
        test_country(app)
        test_https_toggle(app)
        test_categories(app)
        test_log(app)
        test_dialogs(app)
        test_every_button(app)
        test_engine_via_gui(app, target_port, mock_port)
        test_add_remove(app)
        test_persistence(Path(str(state_path) + ".test"))
    except Exception:
        traceback.print_exc()
        RESULTS.append(("unhandled", False, "crashed"))
    finally:
        if app is not None:
            try:
                if app.engine.running:
                    app.engine.stop()
                app.destroy()
            except Exception:
                pass
        target.shutdown()
        # don't leave test state behind
        if had_state:
            state_path.write_bytes(backup)
        else:
            state_path.unlink(missing_ok=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'='*56}\n{passed}/{len(RESULTS)} checks passed")
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for name, detail in failed:
        print(f"  - {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
