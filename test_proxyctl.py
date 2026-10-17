#!/usr/bin/env python3
"""
Tests for proxyctl -- the "point my apps at this proxy" helper.

Everything runs inside a throwaway HOME, so the real ~/.profile, ~/.xsessionrc,
Firefox profiles and desktop launchers are never touched.

    python3 test_proxyctl.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path

import proxyctl

RESULTS = []

# every module global a sandbox has to take over
SAVED = ("APP_DIR", "ENV_FILE", "OFF_FILE", "ENABLED", "RC_FILES",
         "DESKTOP_DIR", "SYSTEM_DESKTOP_DIRS", "FF_BASE", "STATE_FILE",
         "PLATFORM")

PROXY_URL = "http://127.0.0.1:8888"


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))


class Sandbox:
    """Redirect every path proxyctl writes to a temp directory."""

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxyctl-"))
        self.saved = {n: getattr(proxyctl, n) for n in SAVED}
        cfg = self.root / "cfg" / "rotating-proxy"
        proxyctl.APP_DIR = cfg
        proxyctl.ENV_FILE = cfg / "env.sh"
        proxyctl.OFF_FILE = cfg / "off.sh"
        proxyctl.ENABLED = cfg / "enabled"
        proxyctl.RC_FILES = (self.root / ".profile", self.root / ".xsessionrc")
        proxyctl.DESKTOP_DIR = self.root / "share" / "applications"
        proxyctl.SYSTEM_DESKTOP_DIRS = (self.root / "system-apps",)
        proxyctl.FF_BASE = self.root / ".mozilla" / "firefox"
        proxyctl.STATE_FILE = self.root / "proxy_state.json"
        for folder in (proxyctl.APP_DIR, proxyctl.DESKTOP_DIR,
                       proxyctl.SYSTEM_DESKTOP_DIRS[0], proxyctl.FF_BASE):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(proxyctl, name, value)
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    # ---- fixtures -------------------------------------------------------
    def write_state(self, payload: str) -> None:
        proxyctl.STATE_FILE.write_text(payload, encoding="utf-8")

    def write_profile(self, name: str, files: dict) -> None:
        folder = proxyctl.FF_BASE / name
        folder.mkdir(parents=True, exist_ok=True)
        for filename, body in files.items():
            (folder / filename).write_text(body, encoding="utf-8")

    def write_firefox(self, ini: str, profiles: dict) -> None:
        (proxyctl.FF_BASE / "profiles.ini").write_text(ini, encoding="utf-8")
        for name, files in profiles.items():
            self.write_profile(name, files)

    def write_desktop(self, name: str, body: str) -> Path:
        path = proxyctl.SYSTEM_DESKTOP_DIRS[0] / name
        path.write_text(body, encoding="utf-8")
        return path

def run(fn, *args):
    """Call fn, swallowing and capturing everything it prints."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


@contextlib.contextmanager
def _platform(name: str, **stubs):
    """Run a block as `name`, with that platform's OS calls faked.

    Windows and macOS helpers shell out or touch the registry; neither
    exists on the machine running these tests, so the test supplies the
    answers and checks what proxyctl asked for.
    """
    saved_platform = proxyctl.PLATFORM
    proxyctl.PLATFORM = name
    saved = {key: getattr(proxyctl, key) for key in stubs}
    for key, value in stubs.items():
        setattr(proxyctl, key, value)
    try:
        yield
    finally:
        proxyctl.PLATFORM = saved_platform
        for key, value in saved.items():
            setattr(proxyctl, key, value)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rc_fixture() -> tuple[Path, Path]:
    profile = proxyctl.RC_FILES[0]
    xsession = proxyctl.RC_FILES[1]
    profile.write_text("# my profile\nPATH=$PATH:~/bin\n", encoding="utf-8")
    return profile, xsession


PROFILE_INI = """[Profile1]
Name=default
IsRelative=1
Path=aaa.default
Default=1

[Profile0]
Name=esr
IsRelative=1
Path=bbb.esr

[General]
StartWithLastProfile=1
"""

BRAVE_DESKTOP = """[Desktop Entry]
Version=1.0
Name=Brave Web Browser
Comment=Browse the web
Exec=/usr/bin/brave-browser-stable %U
Icon=brave-browser
Type=Application
Categories=Network;WebBrowser;
Actions=new-window;new-private-window;

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/brave-browser-stable

[Desktop Action new-private-window]
Name=New Incognito Window
Exec=/usr/bin/brave-browser-stable --incognito
"""


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_listen_address():
    with Sandbox() as sb:
        check("defaults when no state file", proxyctl.listen_address()
              == ("127.0.0.1", 8888), str(proxyctl.listen_address()))
        check("proxy_url default", proxyctl.proxy_url() == PROXY_URL)

        sb.write_state('{"settings": {"host": "10.0.0.5", "port": 1234}}')
        check("reads host+port", proxyctl.listen_address()
              == ("10.0.0.5", 1234), str(proxyctl.listen_address()))

        sb.write_state('{"settings": {"host": "0.0.0.0", "port": 9999}}')
        check("bind-any becomes loopback", proxyctl.listen_address()
              == ("127.0.0.1", 9999), str(proxyctl.listen_address()))

        sb.write_state('{"settings": {"host": "", "port": "nope"}}')
        check("corrupt state tolerated", proxyctl.listen_address()
              == ("127.0.0.1", 8888), str(proxyctl.listen_address()))

        sb.write_state('{"settings": {"host": "::", "port": 81}}')
        check("ipv6-any becomes loopback", proxyctl.listen_address()
              == ("127.0.0.1", 81), str(proxyctl.listen_address()))


def test_home_hint():
    check("home-relative hint",
          proxyctl.home_hint(Path.home() / "x.sh") == "~/x.sh",
          proxyctl.home_hint(Path.home() / "x.sh"))
    with Sandbox() as sb:
        outside = proxyctl.ENV_FILE
        check("absolute outside HOME kept",
              proxyctl.home_hint(outside) == str(outside),
              proxyctl.home_hint(outside))


def test_env_script():
    with Sandbox():
        text = proxyctl._env_script()
        for var in proxyctl.PROXY_VARS + proxyctl.NO_PROXY_VARS:
            check(f"env script sets {var}", f"export {var}=" in text)
        check("env script uses the live address", PROXY_URL in text)
        check("no_proxy keeps loopback direct",
              f"no_proxy='{proxyctl.NO_PROXY_VALUE}'" in text)
        check("off script unsets everything",
              all(v in proxyctl._off_script() for v in proxyctl.PROXY_VARS))


def test_splice():
    with Sandbox() as sb:
        profile, xsession = _rc_fixture()
        check("creates a missing rc file",
              proxyctl._splice(xsession, proxyctl._block()))
        check("created rc contains the block",
              proxyctl.MARK_START in xsession.read_text(encoding="utf-8"))

        check("adds block to existing rc",
              proxyctl._splice(profile, proxyctl._block()))
        text = profile.read_text(encoding="utf-8")
        check("keeps the original content",
              "PATH=$PATH:~/bin" in text and "# my profile" in text)
        check("block appears exactly once",
              text.count(proxyctl.MARK_START) == 1)

        check("adding twice is a no-op",
              not proxyctl._splice(profile, proxyctl._block()))
        check("still one block",
              profile.read_text(encoding="utf-8").count(
                  proxyctl.MARK_START) == 1)

        check("removes the block", proxyctl._splice(profile, None))
        left = profile.read_text(encoding="utf-8")
        check("original content survives removal", "PATH=$PATH:~/bin" in left)
        check("no trace of the marker", proxyctl.MARK_START not in left)

        check("removing twice is a no-op", not proxyctl._splice(profile, None))

        only = sb.root / "only-block"
        proxyctl._splice(only, proxyctl._block())
        proxyctl._splice(only, None)
        check("block-only file is deleted", not only.exists())

        gone = sb.root / "never-existed"
        check("removing from a missing file is a no-op",
              not proxyctl._splice(gone, None) and not gone.exists())


def test_env_toggle():
    with Sandbox() as sb:
        profile, xsession = _rc_fixture()
        first_on = proxyctl.env_on()
        check("env on touches both rc files", len(first_on) == 2,
              str(first_on))
        check("env on writes the export file",
              proxyctl.ENV_FILE.exists() and PROXY_URL
              in proxyctl.ENV_FILE.read_text(encoding="utf-8"))
        check("env on writes the unset file", proxyctl.OFF_FILE.exists())
        check("env on drops the enable flag", proxyctl.ENABLED.exists())
        check("env status on", proxyctl.env_status() == "on (new logins)",
              proxyctl.env_status())

        again, _ = run(proxyctl.env_on)
        check("env on is idempotent", again == [], str(again))

        touched = proxyctl.env_off()
        check("env off touches both rc files", len(touched) == 2, str(touched))
        check("env off removes the flag", not proxyctl.ENABLED.exists())
        check("env off removes the files",
              not proxyctl.ENV_FILE.exists() and not proxyctl.OFF_FILE.exists())
        check("env status off", proxyctl.env_status() == "off",
              proxyctl.env_status())
        check("env off keeps user rc content",
              "PATH=$PATH:~/bin" in profile.read_text(encoding="utf-8"))

        again, _ = run(proxyctl.env_off)
        check("env off is idempotent", again == [], str(again))

        proxyctl.env_on()
        text = profile.read_text(encoding="utf-8")
        check("enable flag sources env.sh", str(proxyctl.ENV_FILE) in text)
        check("disable branch sources off.sh", str(proxyctl.OFF_FILE) in text)


def test_strip_our_prefs():
    original = '// mine\nuser_pref("browser.startup.homepage", "x");\n'
    with Sandbox():
        ours = proxyctl.firefox_prefs()
        mixed = original + ours + "\n"
        check("only our prefs are stripped",
              proxyctl._strip_our_prefs(mixed).rstrip("\n")
              == original.rstrip("\n"),
              repr(proxyctl._strip_our_prefs(mixed)))
        check("stripping a foreign file is harmless",
              proxyctl._strip_our_prefs(original) == original.rstrip("\n"))
        check("marker written first",
              proxyctl.firefox_prefs().splitlines()[0].startswith(
                  proxyctl.FF_MARK))
        check("prefs cover http and ssl",
              all(k in ours for k in ("network.proxy.http",
                                      "network.proxy.ssl",
                                      "network.proxy.http_port",
                                      "network.proxy.ssl_port")))


def test_firefox_roundtrip():
    ini = PROFILE_INI
    with Sandbox() as sb:
        sb.write_firefox(ini, {
            "aaa.default": {"user.js": '// mine\nuser_pref("x.y", 1);\n'},
            "bbb.esr": {},
        })

        profiles = proxyctl._firefox_profiles()
        check("both profiles found", len(profiles) == 2, str(profiles))
        check("missing profile dirs skipped",
              not any("nope" in str(p) for p in profiles))

        check("firefox is off before we touch it",
              proxyctl.firefox_status() == "off",
              proxyctl.firefox_status())

        touched = proxyctl.firefox_on()
        check("writes both user.js", len(touched) == 2, str(touched))

        first = (proxyctl.FF_BASE / "aaa.default" / "user.js")
        body = first.read_text(encoding="utf-8")
        check("keeps the user's own prefs", 'user_pref("x.y", 1)' in body)
        check("adds our prefs", proxyctl.FF_MARK in body
              and 'user_pref("network.proxy.type", 1)' in body)
        check("points at the proxy", '"127.0.0.1"' in body and "8888" in body)
        check("safety-net backup written",
              (proxyctl.FF_BASE / "aaa.default" / "user.js.proxybak").exists())
        check("empty profile gets a fresh file",
              (proxyctl.FF_BASE / "bbb.esr" / "user.js").exists())

        proxyctl.firefox_on()
        body = first.read_text(encoding="utf-8")
        check("firefox on is idempotent",
              body.count(proxyctl.FF_MARK) == 1
              and body.count('user_pref("network.proxy.type"') == 1)
        check("firefox status on",
              proxyctl.firefox_status() == "on (2/2 profiles)",
              proxyctl.firefox_status())

        touched = proxyctl.firefox_off()
        check("firefox off writes both files", len(touched) == 2, str(touched))
        body = first.read_text(encoding="utf-8")
        check("our prefs removed", proxyctl.FF_MARK not in body
              and "network.proxy" not in body)
        check("the user's prefs restored intact",
              'user_pref("x.y", 1)' in body, repr(body))
        check("backup cleaned up",
              not (proxyctl.FF_BASE / "aaa.default"
                   / "user.js.proxybak").exists())
        check("firefox status off", proxyctl.firefox_status() == "off",
              proxyctl.firefox_status())

        again, _ = run(proxyctl.firefox_off)
        check("firefox off is idempotent", again == [], str(again))

        # a profile that no longer exists must not blow up
        shutil.rmtree(proxyctl.FF_BASE / "bbb.esr")
        proxyctl.firefox_on()
        check("ignores vanished profiles",
              proxyctl.firefox_status() == "on (1/1 profiles)",
              proxyctl.firefox_status())


def test_no_firefox():
    with Sandbox():
        shutil.rmtree(proxyctl.FF_BASE)
        check("no profiles -> nothing to write", proxyctl.firefox_on() == [])
        check("no profiles -> nothing to undo", proxyctl.firefox_off() == [])
        check("status says not installed",
              proxyctl.firefox_status() == "not installed",
              proxyctl.firefox_status())


def test_exec_injection():
    flag = f"--proxy-server={PROXY_URL}"
    check("flag before %U",
          proxyctl._exec_with_proxy("/usr/bin/brave %U")
          == f"/usr/bin/brave {flag} %U")
    check("flag before other switches",
          proxyctl._exec_with_proxy("/usr/bin/x --new-window %U")
          == f"/usr/bin/x {flag} --new-window %U")
    check("flag appended when no field code",
          proxyctl._exec_with_proxy("/usr/bin/x")
          == f"/usr/bin/x {flag}")
    check("never doubles up",
          proxyctl._exec_with_proxy(f"/usr/bin/x {flag} %U")
          == f"/usr/bin/x {flag} %U")
    check("empty exec untouched", proxyctl._exec_with_proxy("") == "")


def test_browsers_roundtrip():
    with Sandbox() as sb:
        sb.write_desktop("brave-browser.desktop", BRAVE_DESKTOP)
        check("no launcher before",
              proxyctl.browsers_status() == "off",
              proxyctl.browsers_status())

        touched = proxyctl.browsers_on()
        check("creates one launcher", len(touched) == 1, str(touched))
        target = Path(touched[0])
        body = target.read_text(encoding="utf-8")

        check("main Exec proxied",
              body.split("Exec=")[1].splitlines()[0]
              .startswith("/usr/bin/brave-browser-stable --proxy-server="))
        check("every Exec proxied",
              all(PROXY_URL in line for line in body.splitlines()
                  if line.startswith("Exec=")))
        check("main Name suffixed",
              "Name=Brave Web Browser (via rotating proxy)" in body)
        check("localised Name untouched",
              "Name[de]=Brave" in body or "Name[de]" not in body)
        check("action Names untouched",
              "Name=New Window\n" in body
              and "Name=New Incognito Window\n" in body)
        check("launcher is executable",
              bool(target.stat().st_mode & 0o111))
        check("browsers status on",
              proxyctl.browsers_status() == "on (Brave)",
              proxyctl.browsers_status())

        proxyctl.browsers_on()
        check("browsers on is idempotent",
              len(list(proxyctl.DESKTOP_DIR.glob("*-rotating-proxy.desktop")))
              == 1)

        touched = proxyctl.browsers_off()
        check("removes the launcher", len(touched) == 1, str(touched))
        check("launcher gone", not target.exists())
        check("stock launcher untouched",
              "Exec=/usr/bin/brave-browser-stable %U"
              in (proxyctl.SYSTEM_DESKTOP_DIRS[0]
                  / "brave-browser.desktop").read_text(encoding="utf-8"))
        check("browsers status off",
              proxyctl.browsers_status() == "off",
              proxyctl.browsers_status())

        again, _ = run(proxyctl.browsers_off)
        check("browsers off is idempotent", again == [], str(again))


def test_no_browser():
    with Sandbox():
        check("no stock launcher -> nothing", proxyctl.browsers_on() == [])


def test_status():
    with Sandbox() as sb:
        # port 1 is never listening, so status must report the proxy as down
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')
        down = "http://127.0.0.1:1"
        code, out = run(proxyctl.status)
        check("status fails when the proxy is down", code == 1, out)
        check("status prints the address", down in out, out)
        check("status lists every layer",
              all(k in out for k in ("env", "firefox", "browsers")), out)
        check("status shows no_proxy", proxyctl.NO_PROXY_VALUE in out, out)

        proxyctl.env_on()
        _, out = run(proxyctl.status)
        check("status reflects the env layer",
              "env        on" in out, out)

        code, out = run(proxyctl.status)
        check("status is repeatable", code == 1, out)

        # -- exit scope, read from the state file the engine writes -----
        check("unrestricted scope reads as anywhere",
              "exit       anywhere" in out, out)
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1,'
                       ' "region": "North America", "country": "us",'
                       ' "state": "California", "https_only": true,'
                       ' "use_only": "strong,socks5"}}')
        _, out = run(proxyctl.status)
        check("status prints the cascade",
              all(bit in out for bit in ("exit       region=North America",
                                         "country=US", "state=California")),
              out)
        check("status prints the hard switches",
              "HTTPS-only" in out and "use=strong,socks5" in out, out)
        sb.write_state("{not json")
        _, out = run(proxyctl.status)
        check("unreadable state file reported honestly",
              "unknown (no state file)" in out, out)
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')
        _, out = run(proxyctl.status)
        check("status says a phone cannot dial a loopback proxy",
              "phone      not reachable" in out, out)
        sb.write_state('{"settings": {"host": "0.0.0.0", "port": 1}}')
        _, out = run(proxyctl.status)
        check("status shows the phone row either way",
              any(line.startswith("phone") for line in out.splitlines()), out)
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')


def test_cli():
    with Sandbox() as sb:
        code, out = run(proxyctl.main, [])
        check("no args -> help", code == 0 and "proxyctl" in out, out)
        code, out = run(proxyctl.main, ["--help"])
        check("--help", code == 0 and "point this machine" in out, out)
        code, out = run(proxyctl.main, ["nonsense"])
        check("unknown command -> 2", code == 2 and "unknown" in out, out)
        code, out = run(proxyctl.main, ["env"])
        check("missing on/off -> 2", code == 2 and "on|off" in out, out)

        code, out = run(proxyctl.main, ["printenv"])
        check("printenv", code == 0 and PROXY_URL in out, out)

        code, out = run(proxyctl.main, ["env", "on"])
        check("env on", code == 0 and "env.sh" in out, out)
        check("env on really applied", proxyctl.env_status()
              == "on (new logins)", proxyctl.env_status())
        code, out = run(proxyctl.main, ["env", "on"])
        check("env on says it already did that",
              code == 0 and "already in that state" in out, out)
        code, out = run(proxyctl.main, ["env", "off"])
        check("env off", code == 0, out)

        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')
        code, out = run(proxyctl.main, ["status"])
        check("status subcommand", code == 1, out)

        # give `on` something for every layer to work on
        sb.write_firefox(PROFILE_INI, {"aaa.default": {}, "bbb.esr": {}})
        sb.write_desktop("brave-browser.desktop", BRAVE_DESKTOP)

        code, out = run(proxyctl.main, ["on"])
        check("on", code == 0 and "Pointing apps at" in out, out)
        check("on applied every layer",
              proxyctl.env_status() == "on (new logins)"
              and proxyctl.firefox_status() == "on (2/2 profiles)"
              and proxyctl.browsers_status() == "on (Brave)",
              f"{proxyctl.env_status()} / {proxyctl.firefox_status()} / "
              f"{proxyctl.browsers_status()}")
        check("on prints the current-shell hint", "env.sh" in out, out)
        check("on says what a phone would need",
              "other devices cannot reach" in out
              or "From a phone or another device" in out, out)

        code, out = run(proxyctl.main, ["off"])
        check("off", code == 0 and "Taking apps off" in out, out)
        check("off reverted every layer",
              proxyctl.env_status() == "off"
              and proxyctl.firefox_status() in ("off", "not installed")
              and proxyctl.browsers_status() == "off", out)


def test_platform_adaptation():
    """The same dialog must describe *this* machine's apps, not Kali's.

    The Windows build used to ship the Linux wording (edit ~/.xsessionrc,
    look for Thunar menu entries) *and* the Linux code paths, so nothing it
    offered could ever take effect there -- which is exactly how "it still
    showed the Kali apps" came about.
    """
    print("\nplatform adaptation")
    with Sandbox() as sb:
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')
        proxy = "http://127.0.0.1:1"

        # ---- the wording itself -----------------------------------------
        linux = " ".join(f"{t} {b}" for _, t, b in proxyctl.layers())
        check("linux wording is this machine's own",
              "xsessionrc" in linux and "Thunar" in linux
              and "Brave / Chromium" in linux, linux)

        with _platform("win32"):
            win = proxyctl.layers()
            win_text = " ".join(f"{t} {b}" for _, t, b in win)
            check("windows layers drop the linux wording",
                  not any(bit in win_text
                          for bit in ("xsessionrc", "Thunar", "~/.mozilla",
                                      "desktop launcher")),
                  win_text)
            check("windows layers name windows apps",
                  "PowerShell" in win[0][2] and "Edge" in win[2][1], win_text)
            check("windows has no rc files to splice",
                  proxyctl.rc_files() == (), str(proxyctl.rc_files()))
            check("windows firefox lives in APPDATA",
                  str(proxyctl.firefox_base()).endswith(
                      "AppData/Roaming/Mozilla/Firefox"),
                  str(proxyctl.firefox_base()))

        with _platform("darwin"):
            mac_text = " ".join(f"{t} {b}" for _, t, b in proxyctl.layers())
            check("macos wording is its own",
                  "~/.zshrc" in mac_text and "Safari" in mac_text
                  and "xsessionrc" not in mac_text, mac_text)
            check("macos shells are zsh and bash",
                  [p.name for p in proxyctl.rc_files()]
                  == [".zshrc", ".bashrc"], str(proxyctl.rc_files()))
            check("macos firefox lives in Library",
                  str(proxyctl.firefox_base()).endswith(
                      "Library/Application Support/Firefox"),
                  str(proxyctl.firefox_base()))

        # ---- layer 1 on Windows: setx + the user's registry --------------
        store: dict = {}
        set_calls: list = []
        del_calls: list = []

        def fake_set(name, value):
            store[name] = value
            set_calls.append(name)
            return True

        def fake_del(name):
            del_calls.append(name)
            return store.pop(name, None) is not None

        with _platform("win32", _win_set_var=fake_set,
                       _win_del_var=fake_del,
                       _win_env_vars=lambda: dict(store)):
            touched = proxyctl.env_on()
            check("windows env sets every variable",
                  touched == list(proxyctl.PROXY_VARS
                                  + proxyctl.NO_PROXY_VARS), str(touched))
            check("windows env points at the proxy",
                  store.get("http_proxy") == proxy
                  and store.get("HTTPS_PROXY") == proxy
                  and store.get("no_proxy") == proxyctl.NO_PROXY_VALUE,
                  str(store))
            check("windows env status reads the registry",
                  proxyctl.env_status() == "on (new windows)",
                  proxyctl.env_status())
            check("windows env on again changes nothing",
                  proxyctl.env_on() == [], str(set_calls))
            proxyctl.env_off()
            check("windows env off deletes every variable",
                  del_calls == list(proxyctl.PROXY_VARS
                                    + proxyctl.NO_PROXY_VARS)
                  and not store, str((del_calls, store)))
            check("windows env off reports off",
                  proxyctl.env_status() == "off", proxyctl.env_status())

        # ---- layer 3 on Windows: WinINET (the system proxy) --------------
        inet: dict = {}
        inet_calls: list = []
        notes: list = []

        def fake_inet_set(name, value, kind="sz"):
            inet[name] = value
            inet_calls.append((name, value, kind))
            return True

        def fake_inet_get(name):
            if name not in inet:
                raise KeyError(name)
            return inet[name]

        def fake_inet_delete(name):
            return inet.pop(name, None) is not None

        stubs = dict(_win_inet_set=fake_inet_set,
                     _win_inet_get=fake_inet_get,
                     _win_inet_delete=fake_inet_delete,
                     _win_notify=lambda: notes.append("notified"))
        with _platform("win32", **stubs):
            touched = proxyctl.browsers_on()
            check("windows system proxy set to host:port",
                  inet.get("ProxyServer") == "127.0.0.1:1", str(inet))
            check("windows system proxy enabled",
                  inet.get("ProxyEnable") == 1, str(inet))
            check("windows bypass list keeps local hosts local",
                  "<local>" in str(inet.get("ProxyOverride", "")), str(inet))
            check("windows status reads the WinINET values",
                  proxyctl.browsers_status() == "on (127.0.0.1:1)",
                  proxyctl.browsers_status())
            check("windows tells the os to re-read its settings",
                  notes == ["notified"], str(notes))
            check("windows writes no .desktop launchers",
                  not proxyctl.DESKTOP_DIR.exists()
                  or not list(proxyctl.DESKTOP_DIR.glob("*.desktop")),
                  "launchers written")
            check("previous system settings backed up",
                  proxyctl._win_backup_file().exists(),
                  str(proxyctl._win_backup_file()))
            check("windows system on again changes nothing",
                  proxyctl.browsers_on() == [], str(inet_calls))

            proxyctl.browsers_off()
            check("windows off removes what it added",
                  "ProxyEnable" not in inet and "ProxyServer" not in inet,
                  str(inet))
            check("windows off drops the backup",
                  not proxyctl._win_backup_file().exists(),
                  str(proxyctl._win_backup_file()))
            check("windows status after off",
                  proxyctl.browsers_status() == "off",
                  proxyctl.browsers_status())

            # a proxy that was already configured must come back unchanged
            inet.update({"ProxyEnable": 0, "ProxyServer": "corp:8080"})
            proxyctl.browsers_on()
            proxyctl.browsers_off()
            check("windows off restores the settings it found",
                  inet.get("ProxyEnable") == 0
                  and inet.get("ProxyServer") == "corp:8080"
                  and "ProxyOverride" not in inet, str(inet))

        # ---- layer 3 on macOS: networksetup ------------------------------
        mac_calls: list = []

        def fake_mac_run(args):
            mac_calls.append(list(args))
            return True

        with _platform("darwin", _mac_run=fake_mac_run,
                       _mac_service=lambda: "Wi-Fi",
                       _mac_capture=lambda args: "\n".join(
                           ("Enabled: Yes", "Server: 127.0.0.1", "Port: 1"))
                       if args[:1] == ["-getwebproxy"] else ""):
            proxyctl.browsers_on()
            check("macos points the active service at host:port",
                  ["-setwebproxy", "Wi-Fi", "127.0.0.1", "1"] in mac_calls
                  and ["-setsecurewebproxy", "Wi-Fi", "127.0.0.1", "1"]
                  in mac_calls, str(mac_calls))
            check("macos also sets socks and the bypass list",
                  len(mac_calls) == 4
                  and mac_calls[3][0] == "-setproxybypassdomains",
                  str(mac_calls))
            check("macos status reads networksetup",
                  proxyctl.browsers_status() == "on (127.0.0.1:1)",
                  proxyctl.browsers_status())
            mac_calls.clear()
            proxyctl.browsers_off()
            check("macos off switches each proxy off",
                  [c[0] for c in mac_calls]
                  == ["-setwebproxystate", "-setsecurewebproxystate",
                      "-setsocksfirewallproxystate"], str(mac_calls))

        # ---- which macOS service gets the settings -----------------------
        order = ("(1) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n\n"
                 "(2) USB LAN\n(Hardware Port: USB LAN, Device: en5)\n")

        class _Proc:
            def __init__(self, code, out=""):
                self.returncode, self.stdout = code, out
                self.stderr = ""

        def fake_exec(cmd, **_kw):
            if cmd and cmd[0] == "route":
                return _Proc(0 if route_ok else 1,
                             "   interface: en0\n" if route_ok else "")
            if cmd and cmd[0] == "networksetup":
                return _Proc(0, order)
            return _Proc(1, "")

        for route_ok, want, why in ((True, "Wi-Fi", "default route"),
                                    (False, "Wi-Fi", "first service")):
            fake_mod = type("fake_subprocess", (), {})()
            fake_mod.run = fake_exec
            fake_mod.SubprocessError = subprocess.SubprocessError
            with _platform("darwin", subprocess=fake_mod):
                got = proxyctl._mac_service()
            check(f"macos service follows the {why}",
                  got == want, got)


def test_device_urls():
    """A phone can only dial in when the proxy listens beyond loopback."""
    print("\naddresses for other devices")
    with Sandbox() as sb:
        sb.write_state('{"settings": {"host": "127.0.0.1", "port": 1}}')
        entries, hint = proxyctl.device_urls()
        check("loopback bind offers no address", entries == [], str(entries))
        check("loopback bind explains the setting to change",
              "0.0.0.0" in hint, hint)
        check("bind host reports the raw interface",
              proxyctl.bind_host() == "127.0.0.1", proxyctl.bind_host())
        check("listen address still maps bind-any to loopback",
              proxyctl.listen_address() == ("127.0.0.1", 1),
              str(proxyctl.listen_address()))

        sb.write_state('{"settings": {"host": "0.0.0.0", "port": 9050}}')
        with _platform("linux",
                       reachable_addresses=lambda: ["192.168.1.23"]):
            entries, hint = proxyctl.device_urls()
            check("bind-any lists an address another device can dial",
                  entries == [("http://192.168.1.23:9050", "local network")],
                  str((entries, hint)))
            check("no nagging when there is an address to give", hint == "",
                  hint)
            check("bind-any is kept raw for the reachability test",
                  proxyctl.bind_host() == "0.0.0.0", proxyctl.bind_host())

        with _platform("linux",
                       reachable_addresses=lambda: ["172.20.10.5"]):
            entries, _hint = proxyctl.device_urls()
            check("a phone hotspot is labelled as one",
                  entries == [("http://172.20.10.5:9050", "phone hotspot")],
                  str(entries))

        with _platform("linux", reachable_addresses=lambda: []):
            entries, hint = proxyctl.device_urls()
            check("nothing to give says so instead of inventing an address",
                  entries == [] and "no address" in hint,
                  str((entries, hint)))

        sb.write_state('{"settings": {"host": "10.1.2.3", "port": 9050}}')
        entries, hint = proxyctl.device_urls()
        check("a fixed interface is offered as-is",
              entries == [("http://10.1.2.3:9050", "local network")],
              str((entries, hint)))

        # the real lookup: loopback and link-local would never work for a phone
        addrs = proxyctl.reachable_addresses()
        check("reachable addresses are never loopback or link-local",
              isinstance(addrs, list)
              and not any(a.startswith(("127.", "169.254.")) for a in addrs),
              str(addrs))
        check("reachable addresses are listed once",
              len(addrs) == len(set(addrs)), str(addrs))


def main() -> int:
    tests = [
        test_listen_address,
        test_home_hint,
        test_env_script,
        test_splice,
        test_env_toggle,
        test_strip_our_prefs,
        test_firefox_roundtrip,
        test_no_firefox,
        test_exec_injection,
        test_browsers_roundtrip,
        test_no_browser,
        test_status,
        test_cli,
        test_platform_adaptation,
        test_device_urls,
    ]

    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            RESULTS.append((t.__name__, False, "crashed"))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'='*56}\n{passed}/{len(RESULTS)} checks passed")
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    if failed:
        print("failures:")
        for name, detail in failed:
            print(f"  - {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
