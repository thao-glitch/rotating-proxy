# Rotating HTTP/SOCKS Proxy — desktop panel

A local forwarding proxy that rotates across a pool of free upstream proxies
(HTTP, SOCKS4 and SOCKS5), with a light, GoLogin-style Tkinter dashboard
in front of it.

* Point any application at `http://127.0.0.1:8888` — or run `./proxyctl on`
  to point the whole machine at it (shell/GTK apps, Firefox, Brave/Chromium)
* The pool is health-checked in the background and dead upstreams are evicted
  automatically; requests fail over to the next candidate
* HTTPS works through upstreams that support tunnels — and those are preferred
  over ones that don't, so a single HTTPS request can't burn its retries on
  proxies that answer `407 Proxy Authentication Required`
* **Sites that block proxies get a second chance**: DNS resolves at the exit,
  only clean headers cross the proxy, and an answer of `403`/`429`/`999`
  rotates the request onto a different exit IP
* **Every upstream is categorised** — a rolling score (Strong / Good / Weak /
  New), latency with a *Fast* filter, a world *Region* filter, HTTPS support
  and live country counts, all sortable and filterable in the pool table
* **The pool restocks itself**: fresh proxies are pulled from plain-text
  lists every few hours, long-dead entries are dropped, and *Proxy →
  Refresh proxy list now* does it immediately

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
  curl -fsSL https://github.com/thao-glitch/rotating-proxy/releases/latest/download/rotating-proxy-install-termux.sh | bash
  ```

## Files

| File | What it is |
| --- | --- |
| `run.py` | entry point and command-line arguments |
| `engine.py` | the proxy core: listener, rotation, failover, health checks, list auto-refresh |
| `gui.py` | the Tkinter dashboard |
| `proxyctl.py` | `./proxyctl` — points the shell, Firefox and browsers at the proxy |
| `appstate.py` | where `proxy_state.json` lives (next to the scripts in a checkout, in the user's config directory once installed) |
| `geodb.py` | offline IP → country lookup (GeoLite Country via `python3-geoip`) |
| `proxylist.py` | the upstream list (300 HTTP + 300 SOCKS4) |
| `test_engine.py` | 192 engine checks (local target + mock HTTP/SOCKS upstreams) |
| `test_gui.py` | 135 dashboard checks (headless, run under `xvfb-run`) |
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
--use FLAGS            comma-separated routing scope from http, socks4,
                       socks5, strong, fast (empty = every upstream)
--rotate-on CODES      comma-separated statuses that make a plain-HTTP
                       request retry through a different exit because
                       this one's IP was refused (default 403,429,999;
                       empty switches the rotation off)
--refresh-url URLS     comma-separated plain-text proxy list URLs to
                       auto-reload into the pool while running (empty
                       disables the refresh)
--refresh-interval N   seconds between automatic list refreshes
                       (default 21600 = 6 h, minimum 60)
--log-level LEVEL      debug | info | warn | error   (--cli mode)
--cli                  run headless, print the log
--check                probe the whole pool once and exit
--no-autostart         open the panel without starting the proxy
```

Both scope mechanisms work in all three modes: they override whatever the
state file had, scope the panel's picker, the `--cli` pool, and the set
`--check` probes. Exit codes from `--check`: `0` if at least one upstream is
alive (and, with `--https-only` or `--use`, matches the scope), `1` otherwise.

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

The same three switches live in the panel: press **🌐 Apps** in the sidebar
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

**Exit via**, in the toolbar of the *Upstream pool* panel, restricts where
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
which is what every `https://` site needs. The **HTTPS** chip in the pool's
*Use* row keeps rotation on upstreams that can actually tunnel:

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

## Picking what the engine may use

The pool toolbar's **Use** row — `HTTP`, `HTTPS`, `SOCKS4`, `SOCKS5`,
`Strong`, `Fast` — scopes *which proxies the engine actually routes
through*. The dropdowns and the filter box only decide what the table
*shows*; these chips decide what carries traffic:

* **Protocol chips** (`HTTP`, `SOCKS4`, `SOCKS5`) — click one to use only
  that protocol, click several to allow several, and clear them all to
  allow every kind of upstream again.
* **HTTPS** — the same switch as `--https-only` above, in chip form.
* **Strong / Fast** — only upstreams in the Strong strength tier, resp.
  with a measured latency of at most 300 ms (`fast`).
* **They stack** with the exit country and with each other, work while
  the proxy is running, and save to `proxy_state.json` the moment you
  click. The log confirms the scope: `upstream selection: only
  socks5,strong`.
* **It fails honestly** — a scope nothing matches warns like the country
  scope does (`no upstream matches the strong routing filter — requests
  will 502 until one comes back`) instead of silently routing anywhere,
  and the status bar shows `Routing scope: socks5, strong` while active.
* From the command line: `python3 run.py --use socks5,strong` in every
  mode; an unknown flag is rejected up front with the list of valid ones.

```bash
python3 run.py --use socks5            # only SOCKS5 exits
python3 run.py --use strong,fast       # only quick, proven exits
```

## Passing sites that block proxies

Sites such as shops, forums and booking engines refuse whole ranges of
proxy IPs. The engine attacks that from four directions:

* **DNS resolves at the exit** — SOCKS5 always sent the hostname on, and
  SOCKS4 now speaks SOCKS4a for names too (with a local-resolve retry for
  ancient servers that refuse it). The target's DNS lookup therefore happens
  beside the proxy's own IP, instead of leaking where you really are.
* **Only clean headers cross the proxy** — `Connection` tokens (RFC 7230),
  `Proxy-*`, `Keep-Alive`, `TE` and `Expect` are hop-by-hop and are dropped;
  everything end-to-end (cookies, `User-Agent`, `Accept`, `Authorization`) is
  forwarded byte for byte. `Expect: 100-continue` is answered locally so
  uploads don't stall.
* **A blocked answer rotates the exit** — when the *target itself* answers
  `403`, `429` or `999` to a bodiless request (GET/HEAD/…), that exit's IP
  is probably on a blocklist, so the engine logs
  `… refused via 1.2.3.4:8080 (429) — rotating to another exit` and retries
  through a different upstream, up to `max_retries`. Only when every
  candidate is blocked does the answer reach the client; requests with a
  body are never replayed (their bytes are already gone), they pass through
  untouched. The status list is configurable — *Rotate exit on status* in
  *Settings*, `--rotate-on` on the command line, `""` to switch it off.
* **Strong exits go first** — rotation offers the scored-strong upstreams
  before weak ones (see below), and a block demotes the node so the next
  request prefers a cleaner IP.

Honest limits: a plain TCP relay cannot disguise the TLS fingerprint of the
client behind it, so sites that block by TLS/JA3 rather than by IP still see
through — there is no way for any forward proxy to change that. What this
engine buys you is the IP reputation, DNS, header hygiene and rotation part.

## Categorising the pool: strength, speed, region

Every upstream carries more than alive/dead:

* **Strength tiers** — each outcome (health probe, served request, failure,
  target block) folds into an exponential moving average of success. The
  table's *Status* cell shows it as a badge — `Alive · Strong`,
  `Alive · Good`, `Alive · Weak`, `Unverified` — and the **Strong** entry in
  the status dropdown filters to only the proven ones. The score behind it
  (`100% over 24 checks`) is in the detail line when you select a row, and
  candidate selection offers Strong → Good → New → Weak in that order.
* **Speed** — the *Latency* column shows the last measured round trip, the
  **Fast** status entry filters to ≤ 300 ms, and the pool tally counts them
  (`142/600 alive · 17 strong · 9 fast`).
* **Region** — beyond the per-country picker, a *Region* dropdown groups
  the pool into Europe / Asia / Africa / North & South America / Oceania
  (derived offline from the exit codes). It is a table filter, and the free-
  text filter matches it too: type `europe`, `strong`, `socks5` or `germany`
  to narrow the list.
* **HTTPS and protocol** — the `HTTPS` status entry shows only tunnellers,
  `Type` sorts/groups HTTP vs SOCKS4 vs SOCKS5, and each row's health sweep
  keeps `Last check` / `Last error` current.

## Keeping the pool stocked

Free proxies die constantly — a static list is dead within weeks, so the
engine reloads fresh ones from plain-text lists **while it runs**:

* **Automatic** — every `refresh_interval` (default 6 hours) it fetches the
  configured URLs, merges everything new (deduplicated; comments, junk rows
  and duplicate addresses ignored), drops long-dead entries in the same pass,
  and health-checks the additions straight away. Each pass is logged:
  `proxy list refreshed (interval): +132 new, 18 stale dropped (731
  configured)`.
* **Built-in sources** — two well-maintained public lists (HTTP and SOCKS5)
  ship as the default, so a fresh install restocks itself out of the box.
  Point it at your own lists with *Refresh list from URL(s)* in *Settings*
  or `--refresh-url` on the command line (comma-separated; `""` switches the
  feature off). A URL whose name contains `socks5`/`socks4` tags bare
  `host:port` lines with that scheme; everything else is read as HTTP.
* **Bounded growth** — an entry that stays dead for 10 consecutive probes is
  pruned on the next refresh, so the pool neither stagnates nor grows
  without bound. A later list may re-add it; it then starts over as *New*
  and earns its place again.
* **Manual** — *Proxy → Refresh proxy list now* pulls immediately, without
  waiting for the interval.
* **Failure-proof** — a source that is offline or gone is logged and
  retried next round; it never takes the proxy down or kills the refresh
  loop. Downloads are capped at 4 MB.

Rows that carry credentials (`user:pass@host:port` or
`host:port:user:pass`) are skipped: the engine does not authenticate to
upstreams, so they could only ever fail.

## The dashboard

A light, GoLogin-style panel: a left sidebar rail for status and actions,
full-width cards underneath.

* **Sidebar** — the running/stopped pill with *Start* / *Stop* / *Check
  now*, the listen endpoint, and entry points for *Apps* and *Settings*
  (with *About* under the *Help* menu). Cards across the top cover requests
  served, active connections, failures, pool health, uptime and traffic.
* **Upstream pool** — sortable table (`Proxy`, `Type`, `Country`, `Status`,
  `Latency`, `Last check`, `Served`, `Last error`) with the **Exit via**
  country picker, the **Use** row of routing chips (`HTTP`, `HTTPS`,
  `SOCKS4`, `SOCKS5`, `Strong`, `Fast`) and the **Region** dropdown in its
  toolbar. The *Status* cell shows the strength badge
  (`Alive · Strong`), addresses sort numerically (`10.0.0.2` ahead of
  `10.0.0.10`) in every order — and in the default status view, equal ranks
  tiebreak by strength, then by address. Type `socks4` in the filter box to
  see just the SOCKS4 entries, `germany` for one country, `europe` for the
  whole region, `strong` for the proven exits, and use the status dropdown
  to narrow it to alive/dead/other/HTTPS/strong/fast.
* **Log** — timestamped engine log with level filtering and colour.
* **Traffic** — rolling sparkline of requests and concurrent connections.
* **Settings** — listen address, health-check interval, retries, timeouts,
  parallel probes, whether to also probe HTTPS tunnel support, the
  *Rotate exit on status* list behind the block-rotation feature, and the
  list-refresh URL(s)/interval behind the auto-restock feature.
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
* Request bodies were dropped when the first read returned nothing (empty
  `Expect` handling) — the forwarder now always reads the body it was handed,
  so POSTs reach upstreams intact.
* Hop-by-hop headers (`Connection` tokens, `Proxy-*`, `Keep-Alive`, `TE`,
  `Expect`) are stripped before relaying, so strict upstreams and targets no
  longer see headers that RFC 7230 says must not be forwarded.
* SOCKS4 now speaks SOCKS4a, so hostnames reach the exit unresolved instead
  of leaking a locally-resolved address.

## Caveat on free proxies

Free proxies are unreliable and some of them intercept TLS, which makes
certificate verification fail (curl reports *"self-signed certificate in
certificate chain"*). That is the upstream, not this proxy — for a quick test
`curl -k` gets past it, but don't route anything you care about through them.
A tunnel can also be dropped the moment it is established (curl's
*"TLS connect error / unexpected eof"*): the engine evicts that node, and the
next request rotates onto another one — expect an occasional retry on a free
pool. See *Keeping the pool stocked* above: the auto-refresh keeps replacing
the dead ones for you.

## Cutting a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

That is the whole procedure: the `release` workflow runs the 473 checks
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
python3 test_engine.py                 # 207 checks
xvfb-run -a python3 test_gui.py        # 151 checks (needs a display or Xvfb)
python3 test_proxyctl.py               # 115 checks
```

473 checks in total. `test_gui.py` starts from the shipped defaults (it backs
up and removes `proxy_state.json` first, then restores it), so a leftover exit
country or HTTPS-only flag from a real session can never leak into the checks.

The first two suites build everything locally (a target HTTP server, a mock
HTTP proxy and mock SOCKS4/SOCKS5 servers), so no internet access is required
for them. `test_proxyctl.py` redirects every path it writes to a throwaway
temp directory, so it never touches your real `~/.profile`, Firefox profiles
or desktop launchers.
