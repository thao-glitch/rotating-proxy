#!/usr/bin/env python3
"""
proxyctl -- point this machine's apps, browsers and terminal at the rotating
proxy, and take them off again.

    ./proxyctl on                do all three layers below
    ./proxyctl off               undo all three
    ./proxyctl status            what is currently pointed where
    ./proxyctl env on|off        shell, terminal and GIO/GTK apps
    ./proxyctl firefox on|off    Firefox's own proxy preferences
    ./proxyctl browsers on|off   the system proxy, or .desktop launchers
    ./proxyctl printenv          print the export block (for a one-off shell)

Every change is reversible and marked so it can be found and removed again.
The proxy address is read from proxy_state.json, so if you change the port in
the GUI, `./proxyctl on` follows it.  (The Windows build ships this script as
`proxyctl.exe`, so drop the `./` there; macOS and Linux run `./proxyctl`.)

What the three layers *are* follows the OS this copy runs on, because the
settings each platform keeps in a different place:

    Windows   setx for the environment, HKCU\\Environment + WinINET keys for
              the system proxy, %APPDATA%\\Mozilla\\Firefox for profiles
    macOS     ~/.zshrc + ~/.bashrc, `networksetup` for the system proxy,
              ~/Library/Application Support/Firefox for profiles
    Linux     ~/.profile + ~/.xsessionrc, .desktop launchers, ~/.mozilla

`status` and the panel's "Point apps here" dialog also print the addresses a
phone or another device can dial -- but only when the proxy is configured to
listen on something other than 127.0.0.1 (Settings -> Listen interface).
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
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

# rc files that decide what a login/GUI session inherits -- Linux; the
# per-OS view of these paths is `rc_files()` / `firefox_base()` below
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
# which OS are we pointing at?
# ---------------------------------------------------------------------------
# Everything below branches on it.  A Windows or macOS build used to run the
# Linux code unchanged -- splicing ~/.profile, hunting for ~/.mozilla and
# writing .desktop launchers -- so nothing it did could ever take effect on
# the machine it was installed on.  Tests patch PLATFORM to walk the other
# platforms from this one.
PLATFORM = sys.platform

# where the environment layer lives, and where Firefox keeps its profiles
WIN_FF_BASE = Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox"
MAC_FF_BASE = Path.home() / "Library" / "Application Support" / "Firefox"
MAC_RC_FILES = (Path.home() / ".zshrc", Path.home() / ".bashrc")


def is_windows() -> bool:
    return PLATFORM == "win32"


def is_macos() -> bool:
    return PLATFORM == "darwin"


def rc_files() -> tuple[Path, ...]:
    """The files a new session reads to pick up environment settings.

    Empty on Windows, which keeps per-user environment variables in the
    registry instead (see `_win_env_on`) -- there is no rc file to splice.
    """
    if is_windows():
        return ()
    if is_macos():
        return MAC_RC_FILES
    return RC_FILES


def firefox_base() -> Path:
    """Where `profiles.ini` lives on this OS (tested through FF_BASE on
    Linux, so a sandbox still redirects it)."""
    if is_windows():
        return WIN_FF_BASE
    if is_macos():
        return MAC_FF_BASE
    return FF_BASE


# ---------------------------------------------------------------------------
# proxy address
# ---------------------------------------------------------------------------
def _settings() -> dict:
    """The engine's settings as stored, or {} when there is no readable one."""
    try:
        settings = json.loads(STATE_FILE.read_text(encoding="utf-8"))["settings"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _raw_bind() -> tuple[str, int]:
    """(host, port) exactly as stored -- 0.0.0.0 and friends kept, unlike
    `listen_address()`, which maps them to the loopback address we dial."""
    settings = _settings()
    host = str(settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(settings.get("port", 8888))
    except (TypeError, ValueError):
        port = 8888
    if not 0 < port < 65536:
        port = max(1, min(port, 65535))
    return host, port


def bind_host() -> str:
    """The interface the engine binds, exactly as configured.

    Unlike `listen_address()` this keeps 0.0.0.0 / :: -- the "can a phone
    reach this?" answer depends on the real bind, not on the loopback
    address we dial it from.
    """
    return _raw_bind()[0]


def listen_address() -> tuple[str, int]:
    """(host, port) the engine is configured to bind on."""
    host, port = _raw_bind()
    if host in ("0.0.0.0", "::", "*", ""):
        host = "127.0.0.1"                 # bind-any -> reach it on loopback
    return host, port


def proxy_url() -> str:
    host, port = listen_address()
    if ":" in host:                        # literal IPv6 needs brackets
        host = f"[{host}]"
    return f"http://{host}:{port}"


# the real lookup is polled by the panel roughly once a second, and a
# machine's addresses do not change by the second -- so remember them for
# a quarter of a minute (tests replace this function wholesale)
REACHABLE_TTL = 15.0
_reachable_cache: list[str] = []
_reachable_at = 0.0


def reachable_addresses() -> list[str]:
    """Addresses another device on this network can reach us on.

    The first entry is the address the routing table would send packets out
    with -- a UDP `connect` only asks the table that question, no packet
    ever leaves.  The rest come from a hostname lookup, which is how a
    laptop is found on a phone hotspot that hands out no names.  Loopback
    and link-local addresses are useless to anyone else, so they are left
    out of the list rather than offered as a working address.
    """
    global _reachable_at
    now = time.time()
    if now - _reachable_at < REACHABLE_TTL:
        return list(_reachable_cache)
    found: list[str] = []

    def add(value: str) -> None:
        if not value or value in found:
            return
        if value.startswith(("127.", "169.254.", "::1", "fe80:")):
            return
        found.append(value)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 80))       # route lookup, no packet
            add(probe.getsockname()[0])
    except OSError:
        pass                                    # no route out at all
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass
    _reachable_cache[:] = found
    _reachable_at = now
    return found


def _lan_note(address: str) -> str:
    """What sort of network this address belongs to, for the phone hint."""
    if address.startswith(("172.20.10.", "192.168.43.")):
        return "phone hotspot"                 # iPhone / Android tethering
    if address.startswith(("192.168.", "10.")):
        return "local network"
    if address.startswith("172."):
        try:
            inside = 16 <= int(address.split(".")[1]) <= 31
        except (IndexError, ValueError):
            inside = False
        return "local network" if inside else "this machine"
    return "this machine"


def device_urls(host: str | None = None,
                port: int | None = None) -> tuple[list[tuple[str, str]], str]:
    """(`(url, note)` for every address a phone can dial, and a hint).

    An empty list with a hint means the proxy only listens on this computer:
    there is nothing to hand a phone, and saying so beats letting someone
    type an address that could never work.  `host`/`port` default to the
    bind stored in the state file; the panel passes the engine's *live*
    bind instead, so a Settings change that has not been saved yet is
    described the way it actually is.
    """
    if host is None or port is None:
        raw_host, raw_port = _raw_bind()
        host = raw_host if host is None else host
        port = raw_port if port is None else port
    host = str(host or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8888
    if not 0 < port < 65536:
        port = max(1, min(port, 65535))

    if host not in ("0.0.0.0", "::"):
        if host in ("127.0.0.1", "localhost", "::1"):
            return [], ("this proxy only listens on this computer — set "
                        "Listen host to 0.0.0.0 in Settings to reach it "
                        "from a phone or another device")
        return [(f"http://{host}:{port}", _lan_note(host))], ""

    entries = [(f"http://{address}:{port}", _lan_note(address))
               for address in reachable_addresses()]
    if not entries:
        return [], ("this machine has no address another device can reach "
                    "(no network connection?)")
    return entries, ""


def exit_scope() -> str:
    """Where traffic is allowed to leave, read from the state file.

    Mirrors the panel's cascade (Region -> Country -> State) plus the hard
    switches, so `proxyctl status` answers "what is my exit scope?" without
    having to talk to the running proxy.  `anywhere` means nothing is
    restricting the pool; `unknown` means there is no readable state file.
    """
    try:
        settings = json.loads(STATE_FILE.read_text(encoding="utf-8"))["settings"]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown (no state file)"
    bits = []
    region = str(settings.get("region") or "").strip()
    if region:
        bits.append(f"region={region}")
    country = str(settings.get("country") or "").strip()
    if country and country != "-":
        bits.append(f"country={country.upper()}")
    state = str(settings.get("state") or "").strip()
    if state:
        bits.append(f"state={state}")
    https = settings.get("https_only")
    if isinstance(https, str):              # a hand-edited state file may
        https = https.strip().lower() in ("1", "true", "yes", "on")
    if https:
        bits.append("HTTPS-only")
    use = str(settings.get("use_only") or "").strip()
    if use:
        bits.append(f"use={use}")
    return " · ".join(bits) if bits else "anywhere"


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


# -- the Windows half of layer 1 -------------------------------------------
def _win_set_var(name: str, value: str) -> bool:
    """`setx` writes one variable into HKCU\\Environment (per user, no admin)."""
    try:
        proc = subprocess.run(["setx", name, value], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[proxyctl] could not set {name}: {exc}")
        return False
    if proc.returncode:
        print(f"[proxyctl] could not set {name}: "
              f"{(proc.stderr or proc.stdout or '').strip()}")
        return False
    return True


def _win_del_var(name: str) -> bool:
    """`reg delete` removes it again; False when it was not set to begin with."""
    try:
        proc = subprocess.run(
            ["reg", "delete", r"HKCU\Environment", "/v", name, "/f"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[proxyctl] could not unset {name}: {exc}")
        return False
    return proc.returncode == 0


def _win_env_vars() -> dict[str, str]:
    """The user's environment as stored in the registry, {} where unavailable."""
    try:
        import winreg
    except ImportError:                     # not on Windows
        return {}
    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[str(name)] = str(value)
                index += 1
    except OSError:
        return {}
    return values


def _win_env_on() -> list[str]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(_env_script(), encoding="utf-8")
    OFF_FILE.write_text(_off_script(), encoding="utf-8")
    ENABLED.touch()
    wanted = {name: proxy_url() for name in PROXY_VARS}
    wanted.update({name: NO_PROXY_VALUE for name in NO_PROXY_VARS})
    current = _win_env_vars()               # so a second run really is a no-op
    return [name for name, value in wanted.items()
            if current.get(name) != value and _win_set_var(name, value)]


def _win_env_off() -> list[str]:
    ENABLED.unlink(missing_ok=True)
    ENV_FILE.unlink(missing_ok=True)
    OFF_FILE.unlink(missing_ok=True)
    return [name for name in PROXY_VARS + NO_PROXY_VARS
            if _win_del_var(name)]


def _win_env_status() -> str:
    if any(name in _win_env_vars() for name in PROXY_VARS):
        return "on (new windows)"
    if ENABLED.exists():
        return "installed but switched off"
    return "off"


def env_on() -> list[str]:
    if is_windows():
        return _win_env_on()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(_env_script(), encoding="utf-8")
    OFF_FILE.write_text(_off_script(), encoding="utf-8")
    ENABLED.touch()
    touched = [str(p) for p in rc_files() if _splice(p, _block())]
    return touched


def env_off() -> list[str]:
    if is_windows():
        return _win_env_off()
    touched = [str(p) for p in rc_files() if _splice(p, None)]
    ENABLED.unlink(missing_ok=True)
    ENV_FILE.unlink(missing_ok=True)
    OFF_FILE.unlink(missing_ok=True)
    return touched


def env_status() -> str:
    if is_windows():
        return _win_env_status()
    if ENABLED.exists():
        return "on (new logins)"
    for p in rc_files():
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
    base = firefox_base()
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
# layer 3: the system proxy (Windows / macOS) or the launchers (Linux)
# ---------------------------------------------------------------------------
def browsers_on() -> list[str]:
    """Point *every* app on this machine at the proxy, or say why we cannot.

    Windows and macOS keep one switch the whole system follows (WinINET /
    `networksetup`), so on those platforms there is no launcher to copy --
    writing .desktop files there would do nothing at all.
    """
    if is_windows():
        return _win_system_on()
    if is_macos():
        return _mac_system_on()
    return _launchers_on()


def browsers_off() -> list[str]:
    if is_windows():
        return _win_system_off()
    if is_macos():
        return _mac_system_off()
    return _launchers_off()


def browsers_status() -> str:
    if is_windows():
        return _win_system_status()
    if is_macos():
        return _mac_system_status()
    return _launchers_status()


# -- Windows: the WinINET values Edge, Chrome and WinHTTP apps follow ------
INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INET_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride")


def _win_inet_set(name: str, value, kind: str = "sz") -> bool:
    """Write one WinINET value under this user's hive (no admin needed)."""
    try:
        import winreg
    except ImportError:                     # not on Windows
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if kind == "dword":
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        return True
    except OSError as exc:
        print(f"[proxyctl] could not write {name}: {exc}")
        return False


def _win_inet_get(name: str):
    try:
        import winreg
    except ImportError:
        raise KeyError(name)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        raise KeyError(name)
    return value


def _win_inet_delete(name: str) -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False                        # already absent: nothing to undo


def _win_notify() -> None:
    """Tell Windows the settings changed -- apps read them lazily."""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, None, 0)      # settings changed
        wininet.InternetSetOptionW(0, 37, None, 0)      # refresh connections
    except (AttributeError, OSError, ValueError):
        pass                                            # best effort only


def _win_backup_file() -> Path:
    return APP_DIR / "win-inet.json"


def _win_save_backup() -> None:
    """Remember the system proxy as it was -- once, before the first write."""
    path = _win_backup_file()
    if path.exists():
        return
    prior = {}
    for name in INET_NAMES:
        try:
            prior[name] = _win_inet_get(name)
        except KeyError:
            prior[name] = None               # was absent -> removed again on off
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prior, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[proxyctl] could not save the previous settings: {exc}")


def _win_system_on() -> list[str]:
    host, port = listen_address()
    target = f"{host}:{port}"
    wanted = {"ProxyServer": target,
              "ProxyEnable": 1,
              "ProxyOverride": f"{NO_PROXY_VALUE};<local>"}
    labels = {"ProxyServer": f"system proxy -> {target}",
              "ProxyEnable": "system proxy enabled",
              "ProxyOverride": f"bypass list -> {NO_PROXY_VALUE}"}
    current: dict = {}
    for name in wanted:
        try:
            current[name] = _win_inet_get(name)
        except KeyError:
            current[name] = None
    _win_save_backup()
    touched = []
    for name, value in wanted.items():
        if current.get(name) == value:
            continue                        # already in that state
        kind = "dword" if name == "ProxyEnable" else "sz"
        if _win_inet_set(name, value, kind):
            touched.append(labels[name])
    if touched:
        _win_notify()
    return touched


def _win_system_off() -> list[str]:
    path = _win_backup_file()
    prior: dict = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
    touched = []
    for name in INET_NAMES:
        value = prior.get(name) if prior else None
        if value is None:
            if _win_inet_delete(name):       # nothing of ours to put back
                touched.append(f"removed {name}")
        else:
            kind = "dword" if name == "ProxyEnable" else "sz"
            if _win_inet_set(name, value, kind):
                touched.append(f"restored {name}={value}")
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            print(f"[proxyctl] could not remove {path}: {exc}")
    _win_notify()
    return touched


def _win_system_status() -> str:
    try:
        if not _win_inet_get("ProxyEnable"):
            return "off"
    except KeyError:
        return "off"
    try:
        server = str(_win_inet_get("ProxyServer") or "").strip()
    except KeyError:
        server = ""
    return f"on ({server})" if server else "on (system)"


# -- macOS: `networksetup`, which speaks per network service ---------------
def _mac_run(args: list[str]) -> bool:
    """Run `networksetup <args>`; False when it refuses (stubbed in tests)."""
    try:
        proc = subprocess.run(["networksetup", *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[proxyctl] networksetup {args[0]}: {exc}")
        return False
    if proc.returncode:
        print(f"[proxyctl] networksetup {args[0]}: "
              f"{(proc.stderr or proc.stdout or '').strip()}")
        return False
    return True


def _mac_capture(args: list[str]) -> str:
    try:
        proc = subprocess.run(["networksetup", *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _mac_service() -> str:
    """The service carrying the default route (usually Wi-Fi).

    macOS settings belong to a *service*, not to the machine, so pointing a
    service the user is not on would look like it worked while doing
    nothing for the network they are actually using.
    """
    interface = ""
    try:
        proc = subprocess.run(["route", "-n", "get", "default"],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.strip().startswith("interface:"):
                    interface = line.split(":", 1)[1].strip()
                    break
    except (OSError, subprocess.SubprocessError):
        pass
    lines = _mac_capture(["-listnetworkserviceorder"]).splitlines()
    first = ""
    for index, line in enumerate(lines):
        match = re.match(r"\s*\(\d+\)\s+(.+)", line)
        if not match:
            continue
        name = match.group(1).strip()
        first = first or name
        if interface and index + 1 < len(lines) and \
                f"Device: {interface}" in lines[index + 1]:
            return name
    return first                            # no route info -> first service


def _mac_system_on() -> list[str]:
    service = _mac_service()
    if not service:
        print("[proxyctl] no active network service to point at the proxy")
        return []
    host, port = listen_address()
    target = f"{host}:{port}"
    bypass = [d.strip() for d in NO_PROXY_VALUE.split(",") if d.strip()]
    commands = [
        ("http proxy", ["-setwebproxy", service, host, str(port)], target),
        ("https proxy", ["-setsecurewebproxy", service, host, str(port)],
         target),
        ("socks proxy", ["-setsocksfirewallproxy", service, host, str(port)],
         target),
        ("bypass list", ["-setproxybypassdomains", service, *bypass],
         NO_PROXY_VALUE),
    ]
    return [f"{name} -> {value}" for name, args, value in commands
            if _mac_run(args)]


def _mac_system_off() -> list[str]:
    service = _mac_service()
    if not service:
        return []
    commands = (("http proxy", ["-setwebproxystate", service, "off"]),
                ("https proxy", ["-setsecurewebproxystate", service, "off"]),
                ("socks proxy",
                 ["-setsocksfirewallproxystate", service, "off"]))
    return [f"{name} switched off" for name, args in commands
            if _mac_run(args)]


def _mac_system_status() -> str:
    service = _mac_service()
    if not service:
        return "off"
    out = _mac_capture(["-getwebproxy", service])
    enabled, server, port = False, "", ""
    for line in out.splitlines():
        if line.startswith("Enabled:"):
            enabled = "Yes" in line
        elif line.startswith("Server:"):
            server = line.split(":", 1)[1].strip()
        elif line.startswith("Port:"):
            port = line.split(":", 1)[1].strip()
    if not enabled:
        return "off"
    return f"on ({server}:{port})" if server else "on (system)"


# -- Linux: a per-browser .desktop launcher that passes --proxy-server ------
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


def _launchers_on() -> list[str]:
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


def _launchers_off() -> list[str]:
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


def _launchers_status() -> str:
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
def layers() -> tuple[tuple[str, str, str], ...]:
    """(key, title, blurb) for each layer, in *this* platform's own words.

    The panel used to ship the Kali wording to every build, so a Windows
    install told people to edit ~/.xsessionrc and look for Thunar menu
    entries -- instructions that mean nothing on that machine, which is
    exactly how "it still showed Kali apps" came about.
    """
    if is_windows():
        return (
            ("env", "Terminal & apps",
             "Sets http_proxy and friends for your Windows user, so new "
             "cmd, PowerShell, Python and curl windows use the proxy."),
            ("firefox", "Firefox",
             "Writes the proxy into every Firefox profile under %APPDATA%"
             "\\Mozilla\\Firefox. Restart Firefox to pick it up; running "
             "./proxyctl firefox off reverts it."),
            ("browsers", "Edge / Chrome (system proxy)",
             "Turns on the Windows system proxy those browsers follow, "
             "and puts back whatever was set before."),
        )
    if is_macos():
        return (
            ("env", "Terminal & apps",
             "Adds the proxy to ~/.zshrc and ~/.bashrc, so new terminal "
             "and GUI sessions inherit http_proxy."),
            ("firefox", "Firefox",
             "Writes the proxy into every Firefox profile under "
             "~/Library/Application Support/Firefox. Restart Firefox to "
             "pick it up; ./proxyctl firefox off reverts it."),
            ("browsers", "Safari / Chrome (system proxy)",
             "Points the network service you are using at the proxy with "
             "`networksetup`; turning it off switches that back."),
        )
    return (                                # Linux, Kali, *BSD
        ("env", "Terminal & system apps",
         "curl, wget, Python and other GIO/GTK apps (Thunar, file "
         "managers, …) follow http_proxy. New logins inherit it from "
         "~/.profile and ~/.xsessionrc."),
        ("firefox", "Firefox",
         "Writes the proxy into every Firefox profile's user.js. Restart "
         "Firefox to pick it up; running ./proxyctl firefox off reverts "
         "it."),
        ("browsers", "Brave / Chromium",
         "Adds a '<browser> (via rotating proxy)' launcher to your menu "
         "that starts the browser with --proxy-server, plus its New "
         "Window and Incognito actions."),
    )


def _layer_title(key: str) -> str:
    for name, title, _blurb in layers():
        if name == key:
            return title
    return key


def _apply(key: str, want: bool) -> list[str]:
    """Run one layer's on/off function by name."""
    fns = {"env": (env_on, env_off),
           "firefox": (firefox_on, firefox_off),
           "browsers": (browsers_on, browsers_off)}
    on_fn, off_fn = fns[key]
    return on_fn() if want else off_fn()


def _status_of(key: str) -> str:
    fns = {"env": env_status,
           "firefox": firefox_status,
           "browsers": browsers_status}
    return fns[key]()


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
    for key, _title, _blurb in layers():
        _report(key, _apply(key, True), _layer_title(key), "on")
    if not is_listening():
        print(f"\n  ! nothing is answering on {url} yet.  Start the proxy first\n"
              "    (python3 run.py), otherwise everything you just pointed at\n"
              "    it will have no internet.\n")
    entries, hint = device_urls()
    if entries:
        print("From a phone or another device on the same network:")
        for address, note in entries:
            suffix = f"   ({note})" if note else ""
            print(f"    {address}{suffix}")
        print("Set it as a manual HTTP proxy on the device, same port.\n")
    elif hint:
        print(f"  · other devices cannot reach this proxy yet: {hint}\n")
    if is_windows():
        print(f"""
The environment layer applies to new command windows; the one you are in
now still has the old values, so open a fresh window -- or run this to
point the current one too:

    set http_proxy={url}
    set https_proxy={url}
    set all_proxy={url}

Firefox needs a restart. Edge and Chrome follow the system proxy you just
turned on; running `./proxyctl off` puts back whatever was set before it.
""")
    elif is_macos():
        print(f"""
The environment layer only applies to *new* sessions. For the shell you are
in right now, run:

    . {home_hint(ENV_FILE)}

Firefox needs a restart. Safari, Chrome and every other app follow the
network-service proxy you just turned on; `./proxyctl off` restores the
previous settings.
""")
    else:
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
    for key, _title, _blurb in layers():
        _report(key, _apply(key, False), _layer_title(key), "off")
    if is_windows():
        print("""
This command window still carries the old values; open a new one and they
are gone.  To clear this window too:

    set http_proxy=
    set https_proxy=
    set all_proxy=
""")
    else:
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
    print(f"exit       {exit_scope()}")
    if listening:
        print(f"answering  {'yes' if works() else 'no -- health check may '
                                             'still be running'}")
    for key, _title, _blurb in layers():
        print(f"{key:<10} {_status_of(key)}")
    entries, hint = device_urls()
    if entries:
        for address, note in entries:
            suffix = f"   ({note})" if note else ""
            print(f"phone      {address}{suffix}")
    elif hint:
        print(f"phone      not reachable -- {hint}")
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
        touched = _apply(layer, want_on)
        _report(layer, touched, _layer_title(layer),
                "on" if want_on else "off")
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
