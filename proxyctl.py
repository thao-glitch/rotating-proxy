#!/usr/bin/env python3
"""
proxyctl -- point this machine's apps, browsers and terminal at the rotating
proxy, and take them off again.

    ./proxyctl on                do all three layers below
    ./proxyctl off               undo all three
    ./proxyctl status            what is currently pointed where
    ./proxyctl env on|off        shell, terminal and GIO/GTK apps
    ./proxyctl firefox on|off    Firefox's own proxy preferences
    ./proxyctl browsers on|off   Brave / Chromium desktop launcher
    ./proxyctl printenv          print the export block (for a one-off shell)

Every change is reversible and marked so it can be found and removed again.
The proxy address is read from proxy_state.json, so if you change the port in
the GUI, `./proxyctl on` follows it.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

from appstate import state_file

APP_DIR = Path.home() / ".config" / "rotating-proxy"
ENV_FILE = APP_DIR / "env.sh"
OFF_FILE = APP_DIR / "off.sh"
ENABLED = APP_DIR / "enabled"
# next to the script in a checkout; the user's config directory in a
# packaged build, where the install location is read-only (see appstate.py)
STATE_FILE = state_file()

MARK_START = "# >>> rotating-proxy >>>"
MARK_END = "# <<< rotating-proxy <<<"

# rc files that decide what a login/GUI session inherits
RC_FILES = (Path.home() / ".profile", Path.home() / ".xsessionrc")

DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"
# where the *stock* browser launchers live, before we copy one
SYSTEM_DESKTOP_DIRS = (Path("/usr/share/applications"),
                       Path("/usr/local/share/applications"))
FF_BASE = Path.home() / ".mozilla" / "firefox"
# (display name, system .desktop file) -- first match wins per browser
BROWSERS = (
    ("Brave", ("brave-browser.desktop", "com.brave.Browser.desktop")),
    ("Chromium", ("chromium.desktop",)),
    ("Chrome", ("google-chrome.desktop", "google-chrome-stable.desktop")),
)

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")
NO_PROXY_VARS = ("no_proxy", "NO_PROXY")

NO_PROXY_VALUE = "127.0.0.1,localhost,::1,.local"


# ---------------------------------------------------------------------------
# proxy address
# ---------------------------------------------------------------------------
def listen_address() -> tuple[str, int]:
    """(host, port) the engine is configured to bind on."""
    host, port = "127.0.0.1", 8888
    try:
        settings = json.loads(STATE_FILE.read_text(encoding="utf-8"))["settings"]
        host = str(settings.get("host", host)).strip() or host
        port = int(settings.get("port", port))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if host in ("0.0.0.0", "::", "*", ""):
        host = "127.0.0.1"                 # bind-any -> reach it on loopback
    if not 0 < port < 65536:
        port = max(1, min(port, 65535))
    return host, port


def proxy_url() -> str:
    host, port = listen_address()
    if ":" in host:                        # literal IPv6 needs brackets
        host = f"[{host}]"
    return f"http://{host}:{port}"


def home_hint(path: Path) -> str:
    """`~/…` form of an absolute path, for pasting into a shell or a label."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def is_listening(timeout: float = 0.6) -> bool:
    """True when something is accepting connections on the proxy port."""
    try:
        with socket.create_connection(listen_address(), timeout=timeout):
            return True
    except OSError:
        return False


def works(timeout: float = 6.0, attempts: int = 3) -> bool:
    """True when the proxy answers a real request.

    One shot can land on a dead upstream mid-rotation, so retry a few times
    before reporting the proxy as broken.
    """
    proxy = urllib.request.ProxyHandler({"http": proxy_url()})
    opener = urllib.request.build_opener(proxy)
    for attempt in range(attempts):
        try:
            with opener.open("http://example.com/", timeout=timeout) as resp:
                if 200 <= resp.status < 400:
                    return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# layer 1: environment variables
# ---------------------------------------------------------------------------
def _env_script() -> str:
    url = proxy_url()
    lines = ["# written by proxyctl -- do not edit by hand", ""]
    for var in PROXY_VARS:
        lines.append(f"export {var}='{url}'")
    for var in NO_PROXY_VARS:
        lines.append(f"export {var}='{NO_PROXY_VALUE}'")
    lines.append("")
    return "\n".join(lines)


def _off_script() -> str:
    names = " ".join(PROXY_VARS + NO_PROXY_VARS)
    return ("# written by proxyctl -- do not edit by hand\n"
            f"unset {names}\n")


def _block() -> str:
    env, off = ENV_FILE, OFF_FILE
    return "\n".join([
        MARK_START,
        f'if [ -e "{ENABLED}" ]; then',
        f'  [ -f "{env}" ] && . "{env}"',
        "else",
        f'  [ -f "{off}" ] && . "{off}"',
        "fi",
        MARK_END,
    ])


def _splice(path: Path, block: str | None) -> bool:
    """Put our marked block into `path` (or take it out again).

    Returns True when the file actually changed.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as exc:
        print(f"[proxyctl] could not read {path}: {exc}")
        return False

    text = original
    if MARK_START in text:
        head, _, rest = text.partition(MARK_START)
        _, _, tail = rest.partition(MARK_END)
        text = head.rstrip("\n") + tail

    body = text.rstrip("\n")
    if block is None:
        new = body + "\n" if body else ""
    else:
        new = (body + "\n\n" if body else "") + block + "\n"

    if new == original:
        return False                      # nothing to do
    try:
        if not new.strip():
            # only our block was ever in there -- remove the file itself
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8")
    except OSError as exc:
        print(f"[proxyctl] could not write {path}: {exc}")
        return False
    return True


def env_on() -> list[str]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(_env_script(), encoding="utf-8")
    OFF_FILE.write_text(_off_script(), encoding="utf-8")
    ENABLED.touch()
    touched = [str(p) for p in RC_FILES if _splice(p, _block())]
    return touched


def env_off() -> list[str]:
    touched = [str(p) for p in RC_FILES if _splice(p, None)]
    ENABLED.unlink(missing_ok=True)
    ENV_FILE.unlink(missing_ok=True)
    OFF_FILE.unlink(missing_ok=True)
    return touched


def env_status() -> str:
    if ENABLED.exists():
        return "on (new logins)"
    for p in RC_FILES:
        try:
            if MARK_START in p.read_text(encoding="utf-8"):
                return "installed but switched off"
        except OSError:
            pass
    return "off"


# ---------------------------------------------------------------------------
# layer 2: Firefox
# ---------------------------------------------------------------------------
def _firefox_profiles() -> list[Path]:
    """Every Firefox profile directory, in profiles.ini order."""
    base = FF_BASE
    ini = base / "profiles.ini"
    if not ini.exists():
        return []
    found: list[Path] = []
    path, relative = "", "1"
    for raw in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            _flush_ff(found, path, relative, base)
            path, relative = "", "1"
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "Path":
                path = value.strip()
            elif key.strip() == "IsRelative":
                relative = value.strip()
    _flush_ff(found, path, relative, base)

    # never touch the stock profiles that ship with Firefox
    return [p for p in found
            if p.is_dir() and p.name not in ("default-release",)]


def _flush_ff(found: list[Path], path: str, relative: str, base: Path) -> None:
    if not path:
        return
    directory = Path(base / path) if relative == "1" else Path(path)
    if directory.is_dir() and directory not in found:
        found.append(directory)


FF_MARK = "// written by proxyctl"
FF_MARK_FULL = "// written by proxyctl -- remove with: ./proxyctl firefox off"
FF_KEYS = ("network.proxy.type", "network.proxy.http",
           "network.proxy.http_port", "network.proxy.ssl",
           "network.proxy.ssl_port", "network.proxy.share_proxy_settings",
           "network.proxy.no_proxies_on", "network.proxy.ftp",
           "network.proxy.ftp_port")


def firefox_prefs() -> str:
    host, port = listen_address()
    lines = [FF_MARK_FULL,
             'user_pref("network.proxy.type", 1);',
             f'user_pref("network.proxy.http", "{host}");',
             f'user_pref("network.proxy.http_port", {port});',
             f'user_pref("network.proxy.ssl", "{host}");',
             f'user_pref("network.proxy.ssl_port", {port});',
             'user_pref("network.proxy.share_proxy_settings", true);',
             f'user_pref("network.proxy.no_proxies_on", "{NO_PROXY_VALUE}");',
             'user_pref("network.proxy.ftp", "");',
             'user_pref("network.proxy.ftp_port", 0);']
    return "\n".join(lines)


def _strip_our_prefs(text: str) -> str:
    """Drop only the block proxyctl wrote; leave everything else untouched."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip().startswith(FF_MARK):
            skipping = True
            continue
        if skipping:
            if any(line.startswith(f'user_pref("{key}') for key in FF_KEYS):
                continue
            skipping = False
        out.append(line)
    return "\n".join(out)


def firefox_on() -> list[str]:
    profiles = _firefox_profiles()
    if not profiles:
        return []
    prefs = firefox_prefs()
    touched = []
    for profile in profiles:
        target = profile / "user.js"
        backup = profile / "user.js.proxybak"
        try:
            current = (target.read_text(encoding="utf-8", errors="replace")
                       if target.exists() else "")
            if current and FF_MARK not in current and not backup.exists():
                backup.write_text(current, encoding="utf-8")   # safety net
            body = _strip_our_prefs(current).rstrip("\n")
            with target.open("w", encoding="utf-8") as fh:
                if body:
                    fh.write(body + "\n\n")
                fh.write(prefs + "\n")
            touched.append(str(target))
        except OSError as exc:
            print(f"[proxyctl] could not write {target}: {exc}")
    return touched


def firefox_off() -> list[str]:
    touched = []
    for profile in _firefox_profiles():
        target = profile / "user.js"
        backup = profile / "user.js.proxybak"
        try:
            if target.exists():
                current = target.read_text(encoding="utf-8", errors="replace")
                if FF_MARK in current:
                    kept = _strip_our_prefs(current).rstrip("\n")
                    if kept:
                        target.write_text(kept + "\n", encoding="utf-8")
                    else:
                        target.unlink()
                    touched.append(str(target))
            if backup.exists():
                backup.unlink()          # only a safety net, never auto-restored
        except OSError as exc:
            print(f"[proxyctl] could not update {target}: {exc}")
    return touched


def firefox_status() -> str:
    profiles = _firefox_profiles()
    if not profiles:
        return "not installed"
    live = 0
    for profile in profiles:
        try:
            text = (profile / "user.js").read_text(encoding="utf-8",
                                                    errors="replace")
        except OSError:
            continue
        if FF_MARK in text:
            live += 1
    if not live:
        return "off"
    return f"on ({live}/{len(profiles)} profiles)"


# ---------------------------------------------------------------------------
# layer 3: Chromium / Brave launcher
# ---------------------------------------------------------------------------
def _exec_with_proxy(exec_line: str) -> str:
    url = proxy_url()
    if "--proxy-server" in exec_line:
        return exec_line
    parts = exec_line.split()
    if not parts:
        return exec_line
    flag = f"--proxy-server={url}"
    # put the flag straight after the binary, before %U / field codes
    for i, part in enumerate(parts[1:], start=1):
        if part.startswith("%") or part.startswith("--"):
            return " ".join(parts[:i] + [flag] + parts[i:])
    return " ".join(parts + [flag])


def browsers_on() -> list[str]:
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    touched = []
    for label, candidates in BROWSERS:
        source = _find_desktop(candidates)
        if source is None:
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = []
        in_action = False
        patched_exec = False
        for line in text.splitlines():
            if line.startswith("[Desktop Action"):
                in_action = True               # only the action sub-blocks
            key, sep, value = line.partition("=")
            if key == "Exec":
                # every action (New Window, Incognito…) must proxy too
                line = "Exec=" + _exec_with_proxy(value)
                patched_exec = True
            elif not in_action and (key == "Name" or key.startswith("Name[")):
                line = f"{key}{sep}{value} (via rotating proxy)"
            lines.append(line)
        if not patched_exec:
            continue
        target = DESKTOP_DIR / f"{source.stem}-rotating-proxy.desktop"
        try:
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            _chmod_x(target)
            touched.append(str(target))
        except OSError as exc:
            print(f"[proxyctl] could not write {target}: {exc}")
    return touched


def browsers_off() -> list[str]:
    touched = []
    for path in DESKTOP_DIR.glob("*-rotating-proxy.desktop"):
        try:
            path.unlink()
            touched.append(str(path))
        except OSError as exc:
            print(f"[proxyctl] could not remove {path}: {exc}")
    return touched


def _find_desktop(names) -> Path | None:
    for name in names:
        for folder in SYSTEM_DESKTOP_DIRS + (DESKTOP_DIR,):
            candidate = folder / name
            if candidate.exists():
                return candidate
    return None


def _chmod_x(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass


def browsers_status() -> str:
    found = sorted(p.name for p in DESKTOP_DIR.glob("*-rotating-proxy.desktop"))
    if not found:
        return "off"
    names = []
    for filename in found:
        stem = filename.removesuffix("-rotating-proxy.desktop")
        stem = stem.replace("-browser", "").replace("google-", "google ")
        names.append(stem.title())
    return "on (" + ", ".join(names) + ")"


# ---------------------------------------------------------------------------
# everything
# ---------------------------------------------------------------------------
def _report(name: str, touched, what: str, state: str = "on") -> None:
    if touched:
        print(f"  {name:<12} {state:<5} <- {what}")
        for item in touched:
            print(f"                 {item}")
    else:
        print(f"  {name:<12} {state:<5} <- {what} (already in that state)")


def on() -> None:
    url = proxy_url()
    print(f"Pointing apps at {url}\n")
    _report("environment", env_on(), "shell / terminal / GIO apps", "on")
    _report("firefox", firefox_on(), "user.js", "on")
    _report("browsers", browsers_on(), "desktop launcher", "on")
    if not is_listening():
        print(f"\n  ! nothing is answering on {url} yet.  Start the proxy first\n"
              "    (python3 run.py), otherwise everything you just pointed at\n"
              "    it will have no internet.\n")
    print(f"""
The environment layer only applies to *new* sessions. For the shell you are
in right now, run:

    . {home_hint(ENV_FILE)}

Firefox needs a restart. Brave/Chromium appear in your menu as
"<browser> (via rotating proxy)" -- or start them from a terminal with

    brave-browser --proxy-server={url}
""")


def off() -> None:
    print("Taking apps off the proxy\n")
    _report("environment", env_off(), "rc files", "off")
    _report("firefox", firefox_off(), "user.js", "off")
    _report("browsers", browsers_off(), "desktop launcher", "off")
    print("""
For the shell you are in right now, run:

    unset http_proxy https_proxy all_proxy ftp_proxy \\
          HTTP_PROXY HTTPS_PROXY ALL_PROXY FTP_PROXY no_proxy NO_PROXY
""")


def status() -> int:
    host, port = listen_address()
    listening = is_listening()
    print(f"proxy      {proxy_url()}   "
          f"({'accepting connections' if listening else 'NOT running'})")
    if listening:
        print(f"answering  {'yes' if works() else 'no -- health check may '
                                             'still be running'}")
    print(f"env        {env_status()}")
    print(f"firefox    {firefox_status()}")
    print(f"browsers   {browsers_status()}")
    print(f"no_proxy   {NO_PROXY_VALUE}")
    return 0 if listening else 1


def printenv() -> None:
    sys.stdout.write(_env_script())


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
HELP = __doc__.strip()


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd = args[0].lower()

    if cmd == "status":
        return status()
    if cmd == "printenv":
        printenv()
        return 0
    if cmd in ("on", "enable"):
        on()
        return 0
    if cmd in ("off", "disable"):
        off()
        return 0

    if cmd in ("env", "firefox", "browsers", "browser"):
        layer = "browsers" if cmd == "browser" else cmd
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            print(f"usage: proxyctl {layer} on|off")
            return 2
        want_on = args[1].lower() == "on"
        table = {"env": (env_on, env_off),
                 "firefox": (firefox_on, firefox_off),
                 "browsers": (browsers_on, browsers_off)}
        on_fn, off_fn = table[layer]
        touched = on_fn() if want_on else off_fn()
        _report(layer, touched, layer, "on" if want_on else "off")
        if layer == "env":
            if want_on:
                print(f"\n  . {home_hint(ENV_FILE)}          # current shell")
            else:
                print("\n  unset http_proxy https_proxy all_proxy ftp_proxy \\")
                print("        HTTP_PROXY HTTPS_PROXY ALL_PROXY FTP_PROXY no_proxy NO_PROXY")
        return 0

    print(f"unknown command: {cmd}\n\n{HELP}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
