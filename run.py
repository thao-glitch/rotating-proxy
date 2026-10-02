#!/usr/bin/env python3
"""
Rotating Proxy — entry point.

    python3 run.py                 launch the desktop control panel (default)
    python3 run.py --cli           headless, prints live stats until Ctrl+C
    python3 run.py --check         probe every upstream and print the result
    python3 run.py --port 9000     override the listen port
    python3 run.py --host 0.0.0.0  listen on all interfaces (use with care)
    python3 run.py --no-autostart  open the panel without starting the proxy
    python3 run.py --country DE    only ever exit through Germany
    python3 run.py --https-only    only use upstreams that tunnel HTTPS

Point any HTTP/HTTPS client (browser, curl, scraper, …) at the address the
panel shows -- by default http://127.0.0.1:8888.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from engine import DEFAULTS, RotatingProxy
from proxylist import PROXY_LIST


class _Help(argparse.ArgumentDefaultsHelpFormatter,
            argparse.RawDescriptionHelpFormatter):
    """Keep our epilog's line breaks *and* show the defaults."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Real-time rotating HTTP/HTTPS proxy with a desktop panel.",
        formatter_class=_Help,
        epilog="once it is running, point the rest of the machine at it:\n"
               "  ./proxyctl on       shell/GTK apps, Firefox, Brave/Chromium\n"
               "  ./proxyctl off      undo all of that\n"
               "  ./proxyctl status   what is pointed where\n"
               "(the panel has the same switches behind its Apps button)")
    p.add_argument("--cli", action="store_true",
                   help="run headless instead of opening the GUI")
    p.add_argument("--check", action="store_true",
                   help="health-check the whole list, print it and exit")
    p.add_argument("--no-autostart", action="store_true",
                   help="open the GUI without starting the proxy")
    p.add_argument("--host", default=DEFAULTS["host"],
                   help="address to bind")
    p.add_argument("--port", type=int, default=DEFAULTS["port"],
                   help="port to bind")
    p.add_argument("--check-interval", type=int,
                   default=DEFAULTS["check_interval"],
                   help="seconds between automatic health sweeps")
    p.add_argument("--max-retries", type=int, default=DEFAULTS["max_retries"],
                   help="upstream candidates tried per request")
    p.add_argument("--log-level", default="info",
                   choices=["debug", "info", "warn", "error"],
                   help="minimum level printed in --cli mode")
    p.add_argument("--proxy", action="append", metavar="HOST:PORT",
                   help="use only this upstream (repeatable) instead of the "
                        "list; accepts socks4:// and socks5:// prefixes")
    p.add_argument("--country", metavar="CODE",
                   help="only use upstreams in this country (ISO code or "
                        "name); empty for anywhere")
    p.add_argument("--https-only", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="only use upstreams that can tunnel HTTPS "
                        "(needed by most https:// sites); --no-https-only "
                        "turns it off again")
    p.add_argument("--rotate-on", metavar="CODES",
                   default=DEFAULTS["rotate_on"],
                   help="comma-separated status codes that make a plain-HTTP "
                        "request retry through a different exit because this "
                        "one's IP was refused (default: %(default)s); "
                        "empty switches the rotation off")
    return p


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
def run_check(args) -> int:
    labels = args.proxy or list(PROXY_LIST)
    engine = RotatingProxy(proxies=labels, probe_timeout=4.0)
    if args.country:
        # resolve the name/code against the pool we just built, then drop
        # everything outside it so the probe only covers that country
        try:
            engine.set_country(args.country)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        want = engine.country
        engine.set_proxies([n["label"] for n in engine.nodes()
                            if n["cc"] == want])
        print(f"Exit country: {want} "
              f"({len(engine.proxies())} upstreams)")
    https_only = bool(args.https_only)
    if https_only:
        print("HTTPS-only: will report the upstreams that can tunnel")
    labels = engine.proxies()
    print(f"Probing {len(labels)} upstreams…")
    engine.check_now(reason="cli")
    while engine.checking:
        done, total = engine.snapshot()["progress"]
        if total:
            print(f"\r  {done}/{total}", end="", flush=True)
        time.sleep(0.2)
    print()

    all_nodes = sorted(engine.nodes(), key=lambda n: (n["status"] != "alive",
                                                      n["latency"] or 1e9))
    socks = sum(1 for n in all_nodes if n["proto"] != "http")
    tunnels = sum(1 for n in all_nodes if n["connect_ok"])
    dead_all = sum(1 for n in all_nodes if n["status"] != "alive")

    nodes = all_nodes
    if https_only:
        # the probe has now decided True/False for everything it reached
        nodes = [n for n in nodes if n["connect_ok"] is True]
        print(f"Showing the {len(nodes)} that can open an HTTPS tunnel")
    width = max((len(n["label"]) for n in nodes), default=10)
    for n in nodes:
        if n["status"] == "alive":
            https = {True: " https", False: "", None: ""}[n["connect_ok"]]
            print(f"  ALIVE  {n['label']:<{width}}  {n['kind']:<7} "
                  f"{n['cc'] or '--':<2}  {n['latency']:6.0f} ms{https}")
    print(f"\n{len(all_nodes) - dead_all} alive, {dead_all} dead "
          f"of {len(all_nodes)}"
          f"  ({socks} socks, {tunnels} https-capable)")
    if args.proxy and nodes and nodes[0]["status"] != "alive":
        print(f"  reason: {nodes[0]['last_error']}")
        return 1
    return 0 if (nodes and nodes[0]["status"] == "alive") else 1


# ---------------------------------------------------------------------------
# --cli
# ---------------------------------------------------------------------------
def run_cli(args) -> int:
    settings = dict(host=args.host, port=args.port,
                    check_interval=args.check_interval,
                    max_retries=args.max_retries,
                    rotate_on=args.rotate_on)
    engine = RotatingProxy(proxies=args.proxy or list(PROXY_LIST), **settings)
    rank = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    threshold = rank[args.log_level]

    def log(level, message):
        if rank.get(level, 1) >= threshold:
            print(f"[{time.strftime('%H:%M:%S')}] [{level.upper():5}] {message}")

    engine._log_cb = log

    if args.country:
        try:
            engine.set_country(args.country)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.https_only is not None:
        try:
            engine.set_https_only(args.https_only)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        engine.start()
        engine.start_health_loop()
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    scope = (f"  exit:   {engine.country}\n" if engine.country else "")
    tunnel = ("  https:  only upstreams that can tunnel\n"
              if engine.https_only else "")
    print(f"\n  proxy:  http://{engine.host}:{engine.port}\n"
          f"  pool:   {len(engine.proxies())} upstreams configured\n"
          f"{scope}{tunnel}"
          f"  press Ctrl+C to stop\n")
    previous = 0
    try:
        while True:
            time.sleep(2)
            s = engine.snapshot()
            delta = s["served"] - previous
            previous = s["served"]
            print(f"\r  served {s['served']:<6} (+{delta:<3}) "
                  f"active {s['active']:<3} failed {s['failed']:<5} "
                  f"alive {s['pool_alive']}/{s['total']:<4} "
                  f"in {s['bytes_in'] / 1_048_576:6.1f} MB "
                  f"out {s['bytes_out'] / 1_048_576:6.1f} MB   ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\n")
        engine.stop()
        return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui(args) -> int:
    try:
        import gui
    except Exception as exc:                       # noqa: BLE001
        print(f"could not start the GUI: {exc}", file=sys.stderr)
        print("try `python3 run.py --cli` for a headless mode",
              file=sys.stderr)
        return 1

    gui.POLL_MS = 600
    app = gui.ProxyGUI(auto_start=not args.no_autostart)
    if args.proxy:
        app.engine.set_proxies(args.proxy)      # replaces the configured list
    # explicit command-line flags win over whatever the state file had
    overrides = {"host": args.host, "port": args.port,
                 "check_interval": args.check_interval,
                 "max_retries": args.max_retries}
    for key, value in overrides.items():
        if value != DEFAULTS[key]:
            try:
                app.engine.configure(**{key: value})
            except (RuntimeError, ValueError) as exc:
                print(f"ignoring --{key}: {exc}", file=sys.stderr)
    if args.country:
        try:
            app.engine.set_country(args.country)
        except ValueError as exc:
            print(f"ignoring --country: {exc}", file=sys.stderr)
    if args.https_only is not None:
        try:
            app.engine.set_https_only(args.https_only)
        except ValueError as exc:
            print(f"ignoring --https-only: {exc}", file=sys.stderr)

    # A restart (pkill, reboot, logout) sends SIGTERM.  Turn it into the
    # normal close so whatever was picked -- exit country, HTTPS-only,
    # added proxies -- is written back instead of silently vanishing.
    def _graceful(_signum, _frame):
        try:
            app.after(0, app._on_close)
        except Exception:                       # window already gone
            pass

    for sig in (getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):           # not the main thread, etc.
            pass

    app.mainloop()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        return run_check(args)
    if args.cli:
        return run_cli(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
