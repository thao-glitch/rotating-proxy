# Rotating HTTP/SOCKS Proxy — desktop panel

A local forwarding proxy that rotates across a pool of free upstream proxies
(HTTP, SOCKS4 and SOCKS5), with a dark Tkinter dashboard in front of it.

* Point any application at `http://127.0.0.1:8888` — or run `./proxyctl on`
  to point the whole machine at it (shell/GTK apps, Firefox, Brave/Chromium)
* The pool is health-checked in the background and dead upstreams are evicted
  automatically; requests fail over to the next candidate
* HTTPS works through upstreams that support tunnels — and those are preferred
  over ones that don't, so a single HTTPS request can't burn its retries on
  proxies that answer `407 Proxy Authentication Required`

```
$ python3 run.py            # opens the desktop panel
$ python3 run.py --cli      # headless, logs to the terminal
$ python3 run.py --check    # probe the pool once and print a report
```

## Installing

Every `v*` tag publishes installers for all four platforms on the
[releases page](../../releases) — download the one for your machine and
open it:

| Platform | Asset | What opening it does |
| --- | --- | --- |
| Windows | `RotatingProxy-<v>-win64.exe` | runs the setup wizard (per-user, no admin prompt) and launches the panel when finished |
| macOS (Apple silicon) | `RotatingProxy-<v>-macos-silicon.pkg` | the standard Installer drops *Rotating Proxy.app* into `/Applications` |
| macOS (Intel) | `RotatingProxy-<v>-macos-intel.pkg` | same, for Intel machines |
| Debian / Ubuntu | `rotating-proxy_<v>_amd64.deb` | opens in the software centre — one click on *Install*; then `rotating-proxy` or the menu entry |
| any Linux | `RotatingProxy-<v>-x86_64.AppImage` | `chmod +x`, then double-click — runs as a single file, installs nothing |
| Android | `RotatingProxy-<v>-android.apk` | installs a small bootstrap app that sets the proxy up inside [Termux](https://github.com/termux/termux-app) and starts it |

Worth knowing:

* **Nothing is written into the install folder.** Installed builds keep
  their state in the per-user config directory — `%APPDATA%\Rotating
  Proxy` (Windows), `~/.config/rotating-proxy` (Linux), `~/Library/
  Application Support/Rotating Proxy` (macOS). From a checkout it stays
  `proxy_state.json` next to the scripts, exactly as before.
* **The Windows and macOS builds are unsigned**: SmartScreen offers
  *More info → Run anyway* for the setup file, and macOS wants a
  right-click → *Open* on the first `.pkg`. Both can be signed — see
  [Cutting a release](#cutting-a-release).
* **Android runs headless** (there is no Tkinter on Android): the APK
  bootstraps Termux, installs Python, fetches this repo and starts the
  proxy with `run.py --cli`; control it in Termux with
  `rotating-proxy start|stop|status|log`. Two one-time switches are
  required by Termux itself and the APK walks you through them: enable
  *Allow external apps* in Termux's settings, and grant *Run commands in
  Termux environment* to the installer (Android Settings → Apps →
  Rotating Proxy Installer → Permissions → Additional permissions).
* Manual Termux install, from inside Termux:

  ```bash
  curl -fsSL https://github.com/OWNER/REPO/releases/latest/download/rotating-proxy-install-termux.sh | bash
  ```

## Files

| File | What it is |
| --- | --- |
| `run.py` | entry point and command-line arguments |
| `engine.py` | the proxy core: listener, rotation, failover, health checks |
| `gui.py` | the Tkinter dashboard |
| `proxyctl.py` | `./proxyctl` — points the shell, Firefox and browsers at the proxy |
| `appstate.py` | where `proxy_state.json` lives (next to the scripts in a checkout, in the user's config directory once installed) |
| `geodb.py` | offline IP → country lookup (GeoLite Country via `python3-geoip`) |
| `proxylist.py` | the upstream list (300 HTTP + 300 SOCKS4) |
| `test_engine.py` | 120 engine checks (local target + mock HTTP/SOCKS upstreams) |
| `test_gui.py` | 122 dashboard checks (headless, run under `xvfb-run`) |
| `test_proxyctl.py` | 115 `proxyctl` checks (runs entirely in a throwaway HOME) |
| `packaging/` | PyInstaller spec, the installer sources for every platform (Inno Setup, deb, AppImage, pkg, APK, Termux script) and the generated icons |
| `.github/workflows/release.yml` | runs the tests and publishes all installers when a `v*` tag is pushed |

## Quick start

```bash
python3 run.py
```

Then point your terminal, Firefox and the browsers at it too:

```bash
./proxyctl on
./proxyctl status
```

Or point a single application at the proxy:

```bash
curl -x http://127.0.0.1:8888 http://example.com/
curl -x http://127.0.0.1:8888 https://example.com/

# git / wget
git config --global http.proxy http://127.0.0.1:8888
wget -e use_proxy=yes -e http_proxy=http://127.0.0.1:8888 http://example.com/

# Python
export HTTPS_PROXY=http://127.0.0.1:8888 HTTP_PROXY=http://127.0.0.1:8888
```

### Command line

```
--host HOST            interface to bind           (default 127.0.0.1)
--port PORT            port to bind                (default 8888)
--check-interval SECS  seconds between health sweeps (default 120)
--max-retries N        upstream candidates tried per request (default 5)
--proxy HOST:PORT      use only this upstream (repeatable), may be
                       socks4://… or socks5://…
--country CODE         only use upstreams in this country
                       (ISO code or name, e.g. `DE` or `Germany`)
--https-only           only use upstreams that can tunnel HTTPS
--no-https-only        drop that restriction again
--log-level LEVEL      debug | info | warn | error   (--cli mode)
--cli                  run headless, print the log
--check                probe the whole pool once and exit
--no-autostart         open the panel without starting the proxy
```

Both scope flags work in all three modes: they override whatever the state
file had, scope the panel's picker, the `--cli` pool, and the set `--check`
probes. Exit codes from `--check`: `0` if at least one upstream is alive (and,
with `--https-only`, can tunnel), `1` otherwise.

## Pointing apps, browsers and the whole system at it

`proxyctl` flips three independent, reversible switches:

| Switch | What it changes | Turn off with |
| --- | --- | --- |
| **environment** | exports `http_proxy`, `https_proxy`, `all_proxy` and `no_proxy` from `~/.config/rotating-proxy/env.sh`, plus a guarded block in `~/.profile` and `~/.xsessionrc` so every new login and GUI session inherits them | `./proxyctl env off` |
| **Firefox** | appends `network.proxy.*` prefs to `user.js` in every Firefox profile (the first run also drops a `user.js.proxybak` safety net) | `./proxyctl firefox off` |
| **browsers** | copies the Brave/Chromium launcher to `~/.local/share/applications/…-rotating-proxy.desktop` with `--proxy-server=…` on *every* `Exec=`, including its New Window and Incognito actions | `./proxyctl browsers off` |

```bash
./proxyctl on            # all three at once
./proxyctl off           # undo all three
./proxyctl status        # what is pointed where, and is the proxy answering
./proxyctl env on        # or one layer at a time
./proxyctl firefox on
./proxyctl browsers on
./proxyctl printenv      # the export block, for a one-off shell
```

The same three switches live in the panel: press **🌐 Apps** in the header
(or *Proxy → Point apps at this proxy…*), which shows the live status of each
layer and has **Point everything at this proxy** / **Take everything off**.

Worth knowing:

* The environment layer only reaches **new** sessions. For the shell you are
  already in, run `. ~/.config/rotating-proxy/env.sh` — the Apps dialog has a
  *Copy* button for that exact line.
* Firefox must be restarted to pick up `user.js`.
* Browsers that ignore `http_proxy` (Brave/Chromium) get a second menu entry
  instead — *Brave Web Browser (via rotating proxy)*. Starting one from a
  terminal works the same way:
  `brave-browser --proxy-server=http://127.0.0.1:8888`
* `no_proxy` keeps `127.0.0.1,localhost,::1,.local` direct, so the panel and
  local services are never sent through the pool.
* The address is read from `proxy_state.json`, so change the port in
  *Settings* and re-run `./proxyctl on` to keep every layer in step.

## The upstream list

`proxylist.py` holds the pool. Entries come in two shapes:

```
1.2.3.4:8080              HTTP proxy (the default when there is no scheme)
socks4://1.2.3.4:1080     SOCKS4  (batch 3 of the list)
socks5://1.2.3.4:1080     SOCKS5
```

Only non-HTTP upstreams carry a prefix, so the common case stays readable and
the labels round-trip cleanly through export/import and the state file.

**Adding more** — the *Add* button and *File → Import* both accept anything
that looks like a proxy list:

* one `host:port` per line, or comma separated
* `socks4://host:port` / `socks5://host:port`
* whole TSV/CSV exports such as

  ```
  14.136.67.106	1080	HK	Hong Kong	Socks4	Anonymous	Yes	1 min ago
  ```

  The first two columns become the upstream and a `Socks4`/`Socks5`/`HTTP`
  column anywhere in the row sets its type. A two-letter column (`HK`) is
  remembered as that row's exit country when it could not be resolved
  otherwise — see [Choosing the exit country](#choosing-the-exit-country).

Your edits are persisted to `proxy_state.json` next to the scripts. Delete that
file to go back to the shipped list.

## Choosing the exit country

**Exit via**, in the header of the *Upstream pool* panel, restricts where
traffic leaves your machine. The list only contains countries the pool
actually has, each with its `alive/total` count, sorted so the usable ones
come first. Picking one applies immediately — no restart and no rebind — and
picking **Anywhere** puts it back.

* **Where the country comes from** — every upstream's address is resolved
  against the GeoLite Country database
  (`/usr/share/GeoIP/GeoIP.dat`, from `geoip-database` + `python3-geoip`),
  entirely offline and cached. No network call, no rate limit.
* **Fallback** — if no database is installed, a `HK`-style column in a pasted
  export is used instead, so TSV imports still work on a bare machine.
* **It really routes** — candidate selection only ever sees upstreams of the
  chosen country, so a German exit can never silently become an American one.
* **An empty scope fails honestly** — when none of that country's upstreams
  are alive you get a `502` and a throttled *"no usable upstream in Germany
  (DE)"* warning, not traffic from somewhere you did not ask for.
* Each health sweep reports the scope too, e.g.
  `health check done in 37.9s — 179 alive … · Germany (DE): 4 alive of 22`.

The **Country** column shows the resolved code (`—` when nothing could be
resolved), is sortable, and the filter box matches it: type `germany`, `DE`
or `socks5` to narrow the table down. While a restriction is active the pool
tally ends with `· exit DE`.

## Routing through HTTPS-capable upstreams

About three quarters of free proxies will refuse an HTTPS `CONNECT` tunnel,
which is what every `https://` site needs. The **HTTPS only** checkbox, right
next to *Exit via*, keeps rotation on upstreams that can actually tunnel:

* **It knows** — a health check reports each node's tunnel support
  (`probe_connect`, on by default); a node that proved it *cannot* tunnel is
  dropped, nodes not yet probed stay in play until they are checked.
* **It stacks with the country** — Germany + HTTPS only means both, and the
  log says so: `selection scope: exit Germany (DE) · HTTPS-capable only`.
* **The tally follows** — `237/600 alive · 86 https-capable · exit DE ·
  HTTPS only`, and each sweep reports `HTTPS-only: 497 can tunnel of 600`.
* **It persists** like every other setting, and `--https-only` /
  `--no-https-only` override it from the command line.
* **The status dropdown's `HTTPS` entry** shows only the tunnellers in the
  table when you want to see what you are working with.
* Turning it on with nothing capable left fails honestly: `502` plus
  *no upstream here can tunnel HTTPS*, never a silent fallback.

```bash
python3 run.py --https-only        # panel / --cli / --check, all three
python3 run.py --check --country DE --https-only
```

The choice is written to `proxy_state.json` **the moment you pick it** — not
when the window closes — and `run.py` treats `SIGTERM` (what `pkill`, a logout
or a reboot sends) as a normal close, so a restart always comes back showing
exactly the exit you set. A saved scope whose country has left the pool is
still displayed (`Germany (DE) · not in pool`) instead of silently claiming
*Anywhere*.

Missing the database? Install it and restart the panel:

```bash
sudo apt install geoip-database python3-geoip
```

## The dashboard

* **Header** — running/stopped pill plus cards for requests served, active
  connections, failures, pool health, uptime and traffic.
* **Upstream pool** — sortable table (`Proxy`, `Type`, `Country`, `Status`,
  `Latency`, `Last check`, `Served`, `Last error`) with the **Exit via**
  country picker and the **HTTPS only** routing checkbox in its header.
  Addresses sort numerically (`10.0.0.2` ahead of `10.0.0.10`), in every
  sort order — including the default status view, where equal ranks tiebreak
  by address. Type `socks4` in the filter box to see just the SOCKS4 entries,
  `germany` to see one country, and use the status dropdown to narrow it to
  alive/dead/other/HTTPS.
* **Log** — timestamped engine log with level filtering and colour.
* **Traffic** — rolling sparkline of bytes in/out.
* **Settings** — listen address, health-check interval, retries, timeouts,
  parallel probes, and whether to also probe HTTPS tunnel support.
* **Apps** — the three switches described above, with their live status and a
  one-click *Point everything at this proxy* / *Take everything off*.

## What was fixed relative to the original snippet

* A `CONNECT` no longer carries a 10-second socket timeout into the relay, so
  idle HTTPS connections and websockets survive.
* Plain HTTP forwards the real method, headers and body (GET/POST/PUT, both
  `Content-Length` and chunked) instead of assuming a hardcoded `GET`.
* Health checks run in parallel — 300 upstreams take seconds, not 20 minutes —
  and the listener binds immediately rather than waiting for the sweep.
* One lock guards the pool and the statistics; dead upstreams keep their status
  instead of silently disappearing; sockets are tracked and closed on shutdown.
* A 407/502/503/504 (or garbage) from an upstream fails over to the next
  candidate instead of being handed to the client.
* Data buffered alongside a CONNECT's `200` is no longer dropped.
* `stop()` really releases the port: the accept loop now uses `select` with a
  short timeout and closes the listener itself, instead of leaving a blocked
  `accept()` holding the socket alive after shutdown.

## Caveat on free proxies

Free proxies are unreliable and some of them intercept TLS, which makes
certificate verification fail (curl reports *"self-signed certificate in
certificate chain"*). That is the upstream, not this proxy — for a quick test
`curl -k` gets past it, but don't route anything you care about through them.
A tunnel can also be dropped the moment it is established (curl's
*"TLS connect error / unexpected eof"*): the engine evicts that node, and the
next request rotates onto another one — expect an occasional retry on a free
pool.

## Cutting a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

That is the whole procedure: the `release` workflow runs the 357 checks
first, then builds every installer on a native runner (Windows, macOS
Intel, macOS Apple silicon, Linux, Android) and publishes them together
as a GitHub Release for the tag. Re-running a finished workflow
re-uploads the assets for that same tag.

Optional repository secrets:

| Secret | Effect |
| --- | --- |
| `MACOS_INSTALLER_IDENTITY` | signs the `.pkg` with a *Developer ID Installer* certificate |
| `ANDROID_KEYSTORE_B64` + `ANDROID_KEYSTORE_PASSWORD` / `ANDROID_KEYSTORE_ALIAS` / `ANDROID_KEY_PASSWORD` | signs the APK with your own key (`base64` of the `.jks` file); without them CI generates a throwaway key and caches it, which keeps updates consistent as long as the cache survives |

## Tests

```bash
python3 test_engine.py                 # 120 checks
xvfb-run -a python3 test_gui.py        # 122 checks (needs a display or Xvfb)
python3 test_proxyctl.py               # 115 checks
```

357 checks in total. `test_gui.py` starts from the shipped defaults (it backs
up and removes `proxy_state.json` first, then restores it), so a leftover exit
country or HTTPS-only flag from a real session can never leak into the checks.

The first two suites build everything locally (a target HTTP server, a mock
HTTP proxy and mock SOCKS4/SOCKS5 servers), so no internet access is required
for them. `test_proxyctl.py` redirects every path it writes to a throwaway
temp directory, so it never touches your real `~/.profile`, Firefox profiles
or desktop launchers.
