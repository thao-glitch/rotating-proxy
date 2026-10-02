#!/usr/bin/env python3
"""
Real-time rotating proxy engine.

Local HTTP/HTTPS proxy; every connection exits through a different upstream
proxy from a live pool, dead ones evicted automatically and revived by the
periodic health checker.

Fixes over the original snippet (behaviour otherwise unchanged):
  * CONNECT tunnels no longer carry a 10 s socket timeout into the relay,
    so idle HTTPS connections (open tabs, websockets) survive.
  * Plain HTTP forwards the client's real method + headers + body
    (GET/POST/PUT/DELETE, Content-Length and chunked), not a hardcoded GET.
  * Health checks run on a thread pool instead of serially, so startup is
    seconds rather than minutes; they also run in the background so the
    listener comes up immediately.
  * All pool/stats mutation happens under one lock -- no racy list rebinding.
  * Dead upstreams keep their slot and are re-probed, instead of being lost.
  * The accept loop survives a bad request line; sockets are tracked and
    closed on shutdown; bytes transferred are counted.

Anti-blocking behaviour (what makes blocked sites reachable):
  * DNS for hostnames happens at the exit (SOCKS4a / SOCKS5 domain
    addressing), so the lookup agrees with the proxy's location instead of
    leaking the local resolver.
  * Hop-by-hop headers never cross the proxy (Connection tokens,
    Proxy-*, Expect), while every end-to-end header is forwarded verbatim.
  * When the target itself answers 403/429/999 to a bodiless request, the
    exit's IP is probably on a blocklist: the engine rotates onto another
    upstream and retries, and only hands the block through when no
    candidate is left (configurable via `rotate_on`).
  * Each upstream carries a rolling success score (Strong / Good / Weak /
    New); rotation offers the strong ones first.

Thread model: the engine is driven by background threads and reports back
through a log callback. The callback may be called from any thread -- a GUI
caller must marshal it onto its own event loop (the Tkinter GUI does this
via a queue).
"""

from __future__ import annotations

import random
import re
import select
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from proxylist import PROXY_LIST

import geodb

# ---------------------------------------------------------------------------
# tunables (defaults; the GUI exposes all of them)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8888,
    "check_interval": 120,     # seconds between automatic health sweeps
    "max_retries": 5,          # upstream candidates tried per request
    "probe_timeout": 4.0,      # health-check connect timeout
    "connect_timeout": 6.0,    # upstream connect timeout
    "idle_timeout": 300.0,     # relay idle timeout before a pipe is dropped
    "health_workers": 64,      # parallel probes during a sweep
    "probe_connect": True,     # also test HTTPS (CONNECT) tunnel support
    "country": "",             # exit country (ISO 3166-1 alpha-2), "" = any
    "https_only": False,       # only use upstreams that can tunnel HTTPS
    "rotate_on": "403,429,999",  # origin statuses that rotate onto another exit
}

MAX_HEADER_BYTES = 65536
RELAY_CHUNK = 65536
BUFFER_CAP = 5000             # log lines kept in memory for the GUI

# upstream protocols we can speak
PROTO_ALIASES = {
    "http": "http", "https": "http", "proxy": "http", "": "http",
    "socks4": "socks4", "socks4a": "socks4", "socks4://": "socks4",
    "socks5": "socks5", "socks": "socks5", "socks5h": "socks5",
}
PROTO_NAMES = {"http": "HTTP", "socks4": "SOCKS4", "socks5": "SOCKS5"}

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_PORT_RE = re.compile(r"^\d{1,5}$")
_SPLIT_RE = re.compile(r"[\s,;|]+")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_proxy(p: str) -> tuple[str, int]:
    """'1.2.3.4:8080' -> ('1.2.3.4', 8080). Raises ValueError when malformed."""
    host, _, port = p.strip().rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"not host:port -> {p!r}")
    port = int(port)
    if not 0 < port < 65536:
        raise ValueError(f"port out of range -> {p!r}")
    return host, int(port)


def parse_entry(text: str) -> tuple[str, str, int]:
    """Parse one upstream reference into (protocol, host, port).

    Accepts 'host:port' (HTTP), 'http://host:port', 'socks4://host:port',
    'socks5://host:port' and plain 'socks4a://host:port'.
    """
    raw = str(text).strip().strip(",")
    proto = "http"
    if "://" in raw:
        scheme, _, raw = raw.partition("://")
        scheme = scheme.strip().lower()
        if scheme not in PROTO_ALIASES:
            raise ValueError(f"unsupported proxy type {scheme!r} -> {text!r}")
        proto = PROTO_ALIASES[scheme]
    host, port = parse_proxy(raw)
    return proto, host, port


def format_entry(proto: str, host: str, port: int) -> str:
    base = f"{host}:{port}"
    return base if proto == "http" else f"{proto}://{base}"


def resolve_country(host: str, hint: str = "") -> tuple[str, str]:
    """``(code, name)`` for an upstream address.

    GeoIP is authoritative for the address itself.  `hint` is the country a
    TSV import row declared, used only when no GeoIP database is installed.
    """
    cc, name = geodb.lookup(host)
    if cc:
        return cc, name
    hint = str(hint or "").strip().upper()
    if _CC_RE.match(hint):
        return hint, ""
    return "", ""


# metadata columns that tell us what kind of upstream a row is
_TYPE_KEYWORDS = {
    "socks4": "socks4", "socks4a": "socks4", "sock4": "socks4",
    "socks5": "socks5", "socks5h": "socks5", "sock5": "socks5",
    "http": "http", "https": "http", "http-proxy": "http", "proxy": "http",
}


def _guess_proto(fields) -> str:
    """Pick the protocol from a row's metadata columns ("Socks4", "HTTP"…)."""
    for field in fields:
        proto = _TYPE_KEYWORDS.get(str(field).strip().lower().rstrip(":,"))
        if proto:
            return proto
    return "http"


_CC_RE = re.compile(r"^[A-Za-z]{2}$")


# ---------------------------------------------------------------------------
# regions: coarse buckets over ISO country codes, for filtering the pool by
# world area without a second GeoIP database
# ---------------------------------------------------------------------------
_REGION_GROUPS = {
    "Europe": ("AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI "
               "GR HR HU IE IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT "
               "RO RS RU SE SI SJ SK SM UA VA XK"),
    "Asia": ("AE AF AM AZ BD BH BN BT CN GE HK ID IL IN IQ IR JO JP KG KH KP "
             "KR KW KZ LA LB LK MM MN MO MV MY NP OM PH PK PS QA SA SG SY TH "
             "TJ TL TM TR TW UZ VN YE"),
    "Africa": ("AO BF BI BJ BW CD CF CG CI CM CV DJ EG EH ER ET GA GH GM GN "
               "GQ GW KE KM LR LS LY MA MG ML MR MU MW MZ NA NE NG RE RW SC "
               "SD SH SL SN SO SS ST SZ TD TG TN UG YT ZA ZM ZW"),
    "North America": ("AG AI AW BB BL BM BQ BS BZ CA CR CU CW DM DO GD GL GP "
                      "GT HN HT JM KN KY LC MF MQ MS MX NI PA PR SV SX TC TT "
                      "VC VG VI US"),
    "South America": ("AR BO BR CL CO EC FK GF GY PE PY SR UY VE"),
    "Oceania": ("AU CC CX FJ FM GU HM KI MH MP NC NF NR NU NZ PF PG PN PW SB "
                "TK TO TV VU WF WS"),
}

REGION_OF: dict[str, str] = {
    cc: region for region, codes in _REGION_GROUPS.items()
    for cc in codes.split()
}


def region_of(cc: str) -> str:
    """"Europe" / "Asia" / … for an ISO country code, "" when unknown."""
    return REGION_OF.get(str(cc or "").strip().upper(), "")


def _guess_cc(fields) -> str:
    """Pick an ISO 3166-1 alpha-2 code out of a row's metadata columns.

    TSV exports carry it as its own column, e.g.::

        14.136.67.106\\t1080\\tHK\\tHong Kong\\tSocks4\\tAnonymous\\tYes\\t1 min ago

    A bare two-letter token is signal enough; anything containing a dot or
    a colon is the address or a scheme and is skipped.  This is only a
    fallback -- `geodb` answers authoritatively for every literal address,
    and this keeps country filtering working when no GeoIP database exists.
    """
    for field in fields:
        token = str(field).strip().strip(":,();")
        if "." in token or ":" in token:
            continue
        if _CC_RE.match(token):
            return token.upper()
    return ""


def _label_from_token(token: str, default_proto: str = "http") -> str | None:
    """A single whitespace/comma-delimited token, or None if unusable."""
    token = token.strip().strip(",;()[]'\"")
    if not token or ":" not in token:
        return None
    try:
        proto, host, port = parse_entry(token)
    except ValueError:
        return None
    if "://" not in token:
        proto = default_proto                # bare host:port inherits the row
    return format_entry(proto, host, port)


def iter_proxy_entries(blob) -> "list[tuple[str, str]]":
    """Pull ``(label, country_code)`` pairs out of arbitrary text.

    Accepts everything `iter_proxy_labels` does -- one-per-line lists,
    comma/whitespace separated blobs and tabular exports such as::

        14.136.67.106\\t1080\\tHK\\tHong Kong\\tSocks4\\tAnonymous\\tYes\\t1 min ago

    The code is taken from the row when it has one and is "" otherwise;
    `set_proxies` falls back to whatever GeoIP says about the address.
    Returns unique labels in first-seen order.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw in str(blob).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cc = _guess_cc(_SPLIT_RE.split(line))
        for label in _labels_from_line(line):
            if label not in seen:
                seen.add(label)
                out.append((label, cc))
    return out


def iter_proxy_labels(blob) -> "list[str]":
    """Pull usable upstreams out of arbitrary text (see the entry pairs)."""
    return [label for label, _ in iter_proxy_entries(blob)]


def _labels_from_line(line: str) -> list[str]:
    fields = [f for f in _SPLIT_RE.split(line) if f]
    if not fields:
        return []
    proto = _guess_proto(fields)

    # 1) direct 'host:port' / 'scheme://host:port' tokens
    direct = [lab for lab in (_label_from_token(t, proto) for t in fields) if lab]
    if direct:
        return direct

    # 2) tabular form: address-like field followed by a port field
    out: list[str] = []
    i = 0
    while i < len(fields) - 1:
        host, port = fields[i], fields[i + 1]
        if (_PORT_RE.match(port) and 0 < int(port) < 65536
                and (_IPV4_RE.match(host) or _HOSTNAME_RE.match(host))):
            out.append(format_entry(proto, host, int(port)))
            i += 2
        else:
            i += 1
    return out


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):                       # [2001:db8::1]:443
        close = target.find("]")
        host = target[1:close]
        rest = target[close + 1:]
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if target.count(":") == 1:
        host, _, port = target.partition(":")
        return host, int(port) if port.isdigit() else default_port
    return target, default_port


def _recv_headers(sock: socket.socket, limit: int = MAX_HEADER_BYTES) -> bytes:
    """Read from sock until the end of the header block (CRLFCRLF)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ValueError("header block too large")
    return buf


def _status_line_is_ok(line: bytes) -> bool:
    """True for any HTTP 2xx reply to a CONNECT."""
    parts = line.split(b" ", 2)
    return line.startswith(b"HTTP/") and len(parts) >= 2 and parts[1][:1] == b"2"


# Reply codes that mean "this upstream refused to proxy for us" rather than
# anything the target itself produced -- worth failing over to another node.
UPSTREAM_REJECTIONS = {b"407", b"502", b"503", b"504"}


def _is_upstream_rejection(status_line: bytes) -> bool:
    """True when the reply should make us try a different upstream.

    A non-HTTP or truncated reply counts as a rejection too: some free
    proxies close the connection instead of answering.
    """
    parts = status_line.split(b" ", 2)
    if not status_line.startswith(b"HTTP/") or len(parts) < 2:
        return True
    return parts[1] in UPSTREAM_REJECTIONS


def _status_code(status_line: bytes) -> int:
    """Numeric status code from a status line, -1 when unparseable."""
    parts = status_line.split(b" ", 2)
    if len(parts) >= 2 and parts[1][:1].isdigit():
        try:
            return int(parts[1][:3])
        except ValueError:
            return -1
    return -1


def _rotate_codes(raw) -> frozenset:
    """Parse the `rotate_on` setting ("403,429,999") into a set of codes.

    Tolerant by design: junk tokens are skipped so a hand-edited state
    file can never crash a request, and an empty result simply means
    "never rotate because of the target's answer".
    """
    out = set()
    for tok in str(raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit() and 100 <= int(tok) <= 999:
            out.add(int(tok))
    return frozenset(out)


def _validate_rotate_on(value) -> str:
    """Normalise `rotate_on` or raise ValueError (used by configure())."""
    text = str(value or "").strip()
    if not text:
        return ""
    for tok in text.replace(";", ",").split(","):
        tok = tok.strip()
        if tok and (not tok.isdigit() or not 100 <= int(tok) <= 999):
            raise ValueError(
                f"rotate_on must be comma-separated status codes "
                f"(e.g. 403,429), got {value!r}")
    return ",".join(tok.strip() for tok in text.replace(";", ",").split(",")
                    if tok.strip())


# Strength tiers: an exponentially weighted success score per upstream, so
# the panel can show Strong / Good / Weak instead of just alive / dead.
SCORE_ALPHA = 0.35           # EMA weight of the newest sample
TIER_ORDER = {"Strong": 0, "Good": 1, "New": 2, "Weak": 3}


def _tier(score, samples: int) -> str:
    if samples <= 0 or score is None:
        return "New"
    if score >= 0.9 and samples >= 2:
        return "Strong"
    if score >= 0.65:
        return "Good"
    return "Weak"


def _read_header_block(sock: socket.socket, limit: int = 65536) -> tuple[bytes, bytes]:
    """Read up to and including CRLFCRLF. Returns (header_block, remainder)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("upstream closed before finishing its headers")
        buf += chunk
        if len(buf) > limit:
            raise OSError("header block too large")
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


# ---------------------------------------------------------------------------
# SOCKS upstreams
# ---------------------------------------------------------------------------
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("upstream closed mid-handshake")
        buf += chunk
    return buf


def _as_ipv4(host: str) -> str | None:
    if not _IPV4_RE.match(host):
        return None
    try:
        socket.inet_aton(host)
    except OSError:
        return None
    return host if all(int(p) < 256 for p in host.split(".")) else None


def _resolve_ipv4(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    return infos[0][4][0] if infos else None


def _idna(host: str) -> bytes:
    try:
        return host.encode("idna")
    except (UnicodeError, LookupError):
        return host.encode("latin-1", "replace")


def _socks4_handshake(sock: socket.socket, host: str, port: int) -> None:
    """SOCKS4 CONNECT, speaking SOCKS4a for hostnames.

    Hostnames are handed to the upstream *as-is* instead of being resolved
    locally: the DNS lookup then happens at the exit, so the target sees a
    name/IP pair that agrees with the proxy's own location -- a local
    lookup would leak the real resolver and break geo checks.
    """
    ip = _as_ipv4(host)
    if ip is not None:
        payload = socket.inet_aton(ip) + b"\x00"          # empty userid
    else:
        # SOCKS4a: hand the proxy a hostname and let it do the lookup
        payload = (b"\x00\x00\x00\x01"                    # 0.0.0.1 == 4a marker
                   + b"\x00"                              # empty userid
                   + _idna(host) + b"\x00")
    sock.sendall(struct.pack("!BBH", 4, 1, port) + payload)
    reply = _recv_exact(sock, 8)
    if reply[0] != 0:
        raise OSError(f"not a SOCKS4 server (VN={reply[0]})")
    if reply[1] != 90:
        raise OSError(f"SOCKS4 refused (status {reply[1]})")


SOCKS5_ERRORS = {1: "general failure", 2: "connection not allowed",
                 3: "network unreachable", 4: "host unreachable",
                 5: "connection refused", 6: "TTL expired",
                 7: "command not supported", 8: "address type not supported"}


def _socks5_handshake(sock: socket.socket, host: str, port: int) -> None:
    """SOCKS5 CONNECT, no authentication (the only kind free lists offer)."""
    sock.sendall(b"\x05\x01\x00")                          # ver, 1 method, no-auth
    greeting = _recv_exact(sock, 2)
    if greeting[0] != 5:
        raise OSError(f"not a SOCKS5 server (ver {greeting[0]})")
    if greeting[1] == 2:
        raise OSError("SOCKS5 requires credentials")
    if greeting[1] != 0:
        raise OSError(f"SOCKS5 auth method {greeting[1]} not supported")

    ipv4 = _as_ipv4(host)
    if ipv4:
        body = b"\x01" + socket.inet_aton(ipv4)
    else:
        name = _idna(host)
        if len(name) > 255:
            raise OSError("hostname too long for SOCKS5")
        body = b"\x03" + bytes([len(name)]) + name
    sock.sendall(b"\x05\x01\x00" + body + struct.pack("!H", port))

    head = _recv_exact(sock, 4)
    if head[0] != 5:
        raise OSError("bad SOCKS5 reply")
    if head[1] != 0:
        raise OSError(f"SOCKS5 connect failed: "
                      f"{SOCKS5_ERRORS.get(head[1], head[1])}")
    if head[3] == 1:                                       # IPv4 bind addr
        _recv_exact(sock, 6)
    elif head[3] == 4:                                     # IPv6
        _recv_exact(sock, 18)
    elif head[3] == 3:                                     # domain
        _recv_exact(sock, 1 + _recv_exact(sock, 1)[0] + 2)
    else:
        raise OSError(f"bad SOCKS5 address type {head[3]}")


def _socks_dial_once(proto: str, address: tuple[str, int], host: str,
                     port: int, timeout: float) -> socket.socket:
    """Open a raw TCP channel to (host, port) through a SOCKS upstream."""
    sock = socket.create_connection(address, timeout=timeout)
    sock.settimeout(timeout)
    try:
        (_socks4_handshake if proto == "socks4" else _socks5_handshake)(
            sock, host, port)
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
    return sock


def _socks_dial(proto: str, address: tuple[str, int], host: str, port: int,
                timeout: float) -> socket.socket:
    """Dial through a SOCKS upstream; hostnames use remote DNS first.

    SOCKS4a/SOCKS5 pass the hostname to the exit so DNS agrees with the
    exit's location. Ancient SOCKS4 servers that reject the 4a marker get
    one retry with a locally resolved address -- only ever on the failure
    path, so the common case still never touches local DNS.
    """
    try:
        return _socks_dial_once(proto, address, host, port, timeout)
    except OSError:
        if proto != "socks4" or _as_ipv4(host):
            raise
        ip = _resolve_ipv4(host)
        if not ip:
            raise
        return _socks_dial_once(proto, address, ip, port, timeout)


# ---------------------------------------------------------------------------
# one upstream proxy
# ---------------------------------------------------------------------------
# Browser-shaped probe headers: plenty of public proxies refuse requests
# that look like a script (no User-Agent), which used to mark healthy
# nodes as dead and shrink the pool for no reason.
_PROBE_UA = (b"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             b"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 "
             b"Safari/537.36\r\n"
             b"Accept: */*\r\nAccept-Language: en-US,en;q=0.9\r\n")


class Node:
    """A configured upstream plus its latest observed health."""

    __slots__ = ("host", "port", "proto", "cc", "country", "status", "latency",
                 "last_check", "last_error", "failures", "hits", "connect_ok",
                 "score", "samples", "blocks")

    def __init__(self, host: str, port: int, proto: str = "http",
                 cc: str = "", country: str = ""):
        self.host = host
        self.port = port
        self.proto = proto            # http | socks4 | socks5
        self.cc = cc                  # ISO 3166-1 alpha-2, "" when unknown
        self.country = country        # human name, "" when unknown
        self.status = "unknown"      # unknown | checking | alive | dead
        self.latency: float | None = None
        self.last_check: float = 0.0
        self.last_error = ""
        self.failures = 0            # consecutive failures
        self.hits = 0                # connections successfully served
        self.connect_ok: bool | None = None   # HTTPS tunnel support
        self.score: float | None = None       # EMA of recent success (0..1)
        self.samples = 0             # observations behind `score`
        self.blocks = 0              # times a target refused this exit's IP

    @property
    def label(self) -> str:
        """Round-trippable identity: scheme only shown for non-HTTP."""
        return format_entry(self.proto, self.host, self.port)

    @property
    def kind(self) -> str:
        return PROTO_NAMES.get(self.proto, self.proto.upper())

    @property
    def country_label(self) -> str:
        """What the Country column shows: the name, or the code, or "—"."""
        if self.country:
            return self.country
        return self.cc or "—"

    @property
    def region(self) -> str:
        """Coarse world area ("Europe", "Asia", …) derived from the code."""
        return region_of(self.cc)

    @property
    def strength(self) -> str:
        """Strong / Good / Weak / New, from the rolling success score."""
        return _tier(self.score, self.samples)

    @property
    def https(self) -> str:
        return "yes" if self.connect_ok else ("no" if self.connect_ok is False else "—")

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def sample(self, value: float) -> None:
        """Fold one observation (1.0 success, 0.0 failure) into the score.

        An exponential moving average reacts to the last few outcomes
        instead of lifetime totals, so one bad sweep can demote a node and
        a couple of clean ones can earn it back.
        """
        self.samples += 1
        self.score = (value if self.score is None
                      else (1 - SCORE_ALPHA) * self.score + SCORE_ALPHA * value)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "host": self.host,
            "port": self.port,
            "proto": self.proto,
            "kind": self.kind,
            "cc": self.cc,
            "country": self.country,
            "country_label": self.country_label,
            "region": self.region,
            "status": self.status,
            "latency": self.latency,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "failures": self.failures,
            "hits": self.hits,
            "connect_ok": self.connect_ok,
            "https": self.https,
            "score": self.score,
            "samples": self.samples,
            "strength": self.strength,
            "blocks": self.blocks,
        }


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class RotatingProxy:
    def __init__(self, *, logger=None, proxies=None, **settings):
        self.settings = dict(DEFAULTS)
        for key, value in settings.items():
            if key in self.settings:
                self.settings[key] = value
        # a hand-edited state file must never be able to scope the pool to
        # a nonsense code: that would 502 every request with no visible
        # cause, so anything that is not an ISO country code is dropped
        code = str(self.settings.get("country") or "").strip().upper()
        if code and (len(code) != 2 or not code.isalpha()):
            code = ""
        self.settings["country"] = code
        self.settings["https_only"] = bool(self.settings.get("https_only"))
        # a hand-edited state file must never break start-up: anything that
        # is not a status-code list falls back to the shipped default
        try:
            self.settings["rotate_on"] = _validate_rotate_on(
                self.settings.get("rotate_on"))
        except ValueError:
            self.settings["rotate_on"] = DEFAULTS["rotate_on"]

        self._log_cb = logger
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._checking = threading.Event()

        self._nodes: list[Node] = []
        self._cc_hints: dict[str, str] = {}   # label -> country from an import
        self._last_scope_warn = 0.0           # throttle for the empty-country log
        self._srv: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._open_socks: set[socket.socket] = set()

        self.stats = {
            "status": "stopped",
            "accepted": 0,
            "served": 0,
            "failed": 0,
            "active": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "started_at": 0.0,
            "last_check_at": 0.0,
            "check_progress": (0, 0),
        }

        if proxies is not None:
            self.set_proxies(proxies)

    # -- configuration ----------------------------------------------------
    @property
    def host(self) -> str:
        return self.settings["host"]

    @property
    def port(self) -> int:
        return int(self.settings["port"])

    @property
    def running(self) -> bool:
        return self.stats["status"] in ("starting", "running")

    @property
    def checking(self) -> bool:
        return self._checking.is_set()

    def configure(self, **updates) -> dict:
        """Update settings. Only allowed while stopped (bind values change)."""
        if self.running:
            raise RuntimeError("stop the proxy before changing its settings")
        unknown = set(updates) - set(self.settings)
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        if "rotate_on" in updates:
            # normalised here so bad input surfaces as a dialog, not as
            # silently disabled rotation later on
            updates = dict(updates)
            updates["rotate_on"] = _validate_rotate_on(updates["rotate_on"])
        self.settings.update(updates)
        return dict(self.settings)

    @property
    def rotate_codes(self) -> frozenset:
        """Status codes that make a plain-HTTP request try another exit."""
        return _rotate_codes(self.settings.get("rotate_on"))

    @property
    def country(self) -> str:
        """ISO code the pool is restricted to, or "" for anywhere."""
        return str(self.settings.get("country") or "").upper()

    def set_country(self, value) -> str:
        """Restrict upstream selection to one country ("" = anywhere).

        Unlike `configure()` this may be called while running: it only
        changes candidate selection, never the listener, so the picker in
        the panel applies straight away.  Accepts a code ("de") or a name
        ("Germany") and normalises to the ISO code.
        """
        want = str(value or "").strip().lower()
        if want in ("any", "all", "anywhere", "worldwide"):
            want = ""
        with self._lock:
            if not want:
                changed = bool(self.settings["country"])
                self.settings["country"] = ""
            else:
                code = want.upper() if len(want) == 2 else ""
                if not code:                       # accept a full name too
                    code = next((n.cc for n in self._nodes
                                 if n.cc and n.country.lower() == want), "")
                if not code:
                    raise ValueError(f"unknown country {value!r}")
                if not any(n.cc == code for n in self._nodes):
                    raise ValueError(f"no upstreams configured for {code}")
                changed = self.settings["country"] != code
                self.settings["country"] = code
        if not changed:
            return self.country
        if self.country:
            name = self._country_name(self.country)
            self.log("info", f"upstream selection restricted to "
                             f"{name or self.country} ({self.country})")
        else:
            self.log("info", "upstream selection: any country")
        return self.country

    def _country_name(self, code: str) -> str:
        with self._lock:
            for node in self._nodes:
                if node.cc == code and node.country:
                    return node.country
        return ""

    @property
    def https_only(self) -> bool:
        """True when rotation is restricted to HTTPS-tunnelling upstreams."""
        return bool(self.settings.get("https_only"))

    def set_https_only(self, value) -> bool:
        """Restrict upstream selection to proxies that can tunnel HTTPS.

        The twin of `set_country`: it may be called while running, because
        it only changes candidate selection, never the listener.  A node
        counts as usable unless a health check proved it cannot open a
        CONNECT tunnel -- unknown nodes stay in play until they are probed.
        """
        if isinstance(value, str):
            want = value.strip().lower()
            if want in ("1", "true", "yes", "on"):
                new = True
            elif want in ("0", "false", "no", "off", ""):
                new = False
            else:
                raise ValueError(f"expected a boolean, got {value!r}")
        elif isinstance(value, (int, float)):
            new = bool(value)          # bool is an int, both land here
        else:
            raise ValueError(f"expected a boolean, got {value!r}")
        with self._lock:
            changed = bool(self.settings.get("https_only")) != new
            self.settings["https_only"] = new
        if not changed:
            return self.https_only
        if new:
            known = sum(1 for n in self._nodes if n.connect_ok is True)
            self.log("info", f"upstream selection: HTTPS-capable only "
                             f"({known} known to tunnel)")
        else:
            self.log("info", "upstream selection: any upstream "
                             "(HTTP and HTTPS)")
        return new

    # -- proxy list -------------------------------------------------------
    def set_proxies(self, labels) -> int:
        """Replace the configured upstream set (order preserved, deduped)."""
        nodes, seen = [], set()
        bad = []
        for raw in labels:
            raw = str(raw).strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                proto, host, port = parse_entry(raw)
            except ValueError:
                bad.append(raw)
                continue
            label = format_entry(proto, host, port)
            if label in seen:
                continue
            seen.add(label)
            cc, name = resolve_country(host, self._cc_hints.get(label, ""))
            nodes.append(Node(host, port, proto, cc, name))
        with self._lock:
            # keep health for entries that survive the edit
            old = {n.label: n for n in self._nodes}
            for n in nodes:
                if n.label in old:
                    prev = old[n.label]
                    n.status, n.latency = prev.status, prev.latency
                    n.last_check, n.failures = prev.last_check, prev.failures
                    n.last_error, n.hits = prev.last_error, prev.hits
                    n.connect_ok = prev.connect_ok
                    n.score, n.samples = prev.score, prev.samples
                    n.blocks = prev.blocks
            self._nodes = nodes
        if bad:
            self.log("warn", f"ignored {len(bad)} malformed entr"
                             f"{'y' if len(bad) == 1 else 'ies'}: "
                             f"{', '.join(bad[:4])}{'…' if len(bad) > 4 else ''}")
        return len(nodes)

    def add_proxies(self, labels) -> int:
        """Append new upstreams; returns how many were actually added.

        Accepts anything readable by `iter_proxy_labels`: bare lists,
        comma-separated blobs, or TSV exports with extra columns.
        """
        fresh: list[str] = []
        with self._lock:
            current = [n.label for n in self._nodes]
            have = set(current)
            for label, cc in iter_proxy_entries(labels):
                if label in have:
                    continue
                fresh.append(label)
                have.add(label)
                if cc:
                    # remembered so the code survives when GeoIP is absent
                    self._cc_hints.setdefault(label, cc)
        if not fresh:
            return 0
        self.set_proxies(current + fresh)
        return len(fresh)

    def remove_proxies(self, labels) -> int:
        drop = {str(l).strip() for l in labels}
        with self._lock:
            before = len(self._nodes)
            self._nodes = [n for n in self._nodes if n.label not in drop]
            return before - len(self._nodes)

    def proxies(self, status: str | None = None, *,
                connect: bool | None = None) -> list[str]:
        """Labels of configured upstreams.

        `status` filters by health ("alive"/"dead"/"unknown"); `connect`
        filters by HTTPS tunnel capability (True/False, omit for either).
        """
        with self._lock:
            return [n.label for n in self._nodes
                    if (status is None or n.status == status)
                    and (connect is None or n.connect_ok is connect)]

    def nodes(self) -> list[dict]:
        with self._lock:
            return [n.as_dict() for n in self._nodes]

    # -- logging ----------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        if self._log_cb:
            try:
                self._log_cb(level, message)
            except Exception:
                pass

    # -- lifecycle --------------------------------------------------------
    def start(self, *, probe_first: bool = True) -> None:
        if self.running:
            raise RuntimeError("already running")
        if not self._nodes:
            raise RuntimeError("the proxy list is empty")

        self._stop.clear()
        self.stats.update(status="starting", accepted=0, served=0, failed=0,
                          active=0, bytes_in=0, bytes_out=0,
                          started_at=time.time(), last_check_at=0.0)

        # Bind first so the GUI flips to "running" without waiting on probes.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as exc:
            srv.close()
            self.stats["status"] = "stopped"
            raise OSError(f"cannot bind {self.host}:{self.port} -- {exc}") from exc
        srv.listen(200)
        self._srv = srv

        t = threading.Thread(target=self._accept_loop, args=(srv,),
                             name="accept", daemon=True)
        t.start()
        self._threads = [t]
        self.stats["status"] = "running"
        self.log("info", f"listening on {self.host}:{self.port} — "
                         f"{len(self._nodes)} upstreams configured")
        note = self._scope_note()
        if note:
            self.log("info", f"selection scope: {note}")
        if probe_first:
            self.check_now(reason="startup")
        self.start_health_loop()

    def stop(self) -> None:
        if not self.running and self.stats["status"] != "starting":
            return
        self.log("info", "shutting down…")
        self._stop.set()

        with self._lock:
            socks = list(self._open_socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

        # Let the accept thread close the listening socket itself: closing it
        # from here while that thread sits in select()/accept() leaves the
        # port bound until it wakes, so connections would still be accepted
        # after stop() returned.
        for t in self._threads:
            t.join(timeout=2.5)
        srv, self._srv = self._srv, None
        if srv is not None:                       # fallback if it never ran
            try:
                srv.close()
            except OSError:
                pass
        self._threads = []
        self.stats.update(status="stopped", active=0)
        self.log("info", "stopped")

    # -- connection tracking ---------------------------------------------
    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._open_socks.add(sock)
            self.stats["active"] = len(self._open_socks)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._open_socks.discard(sock)
            self.stats["active"] = len(self._open_socks)

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.stats[key] += amount

    def _bytes(self, up: int, down: int) -> None:
        with self._lock:
            self.stats["bytes_out"] += up
            self.stats["bytes_in"] += down

    # -- upstream selection ----------------------------------------------
    def _candidates(self, *, connect: bool = False) -> list[Node]:
        """Pick the upstreams to try for this request, best odds first.

        Ordering runs strongest-first: nodes whose recent success score
        puts them in the Strong tier are offered before Good, New and
        Weak ones (shuffled inside each tier so load still spreads over
        the pool). For CONNECT we split known HTTPS-capable nodes to the
        front first: only about a quarter of free proxies will tunnel, and
        without this a single HTTPS request could burn all its retries on
        proxies that answer 407 to CONNECT.
        """
        with self._lock:
            pool = [n for n in self._nodes if n.status == "alive"]
            if not pool:
                # nothing verified yet (or everything just died): last resort
                pool = [n for n in self._nodes if n.status != "checking"]
            country = str(self.settings.get("country") or "").upper()
            https = bool(self.settings.get("https_only"))
            scope_empty: list[str] = []
            if country:
                # hard scope: an exit in another country is exactly what the
                # user asked not to have, so an empty scope yields no
                # candidates rather than quietly routing elsewhere
                pool = [n for n in pool if n.cc == country]
                if not pool:
                    scope_empty.append(country)
            if https:
                # same rule for capability: a node only stays in play unless
                # a health check proved it cannot open a CONNECT tunnel
                pool = [n for n in pool if n.connect_ok is not False]
                if not pool:
                    scope_empty.append("https")
            if not pool:
                if scope_empty:
                    self._warn_scope(*scope_empty)
                return []
            if connect:
                parts = ([n for n in pool if n.connect_ok is True],
                         [n for n in pool if n.connect_ok is None],
                         [n for n in pool if n.connect_ok is False])
            else:
                parts = (pool,)
            ordered: list[Node] = []
            for part in parts:
                buckets: dict[int, list[Node]] = {}
                for n in part:
                    buckets.setdefault(TIER_ORDER.get(n.strength, 2),
                                       []).append(n)
                for tier in sorted(buckets):
                    group = buckets[tier]
                    ordered += random.sample(group, len(group))
            k = min(int(self.settings["max_retries"]), len(ordered))
            return ordered[:k]

    def _note_failure(self, node: Node, reason: str) -> None:
        with self._lock:
            node.failures += 1
            node.status = "dead"
            node.last_error = reason
            node.last_check = time.time()
            node.sample(0.0)
        self.log("warn", f"evicted {node.label} ({reason})")

    def _note_success(self, node: Node, latency_ms: float | None = None) -> None:
        with self._lock:
            node.failures = 0
            node.status = "alive"
            node.hits += 1
            if latency_ms is not None:
                node.latency = latency_ms
            node.last_error = ""
            node.sample(1.0)

    # -- accept / dispatch ------------------------------------------------
    def _accept_loop(self, srv: socket.socket) -> None:
        """Accept loop. Owns `srv` and closes it on the way out.

        `select` with a short timeout (rather than a blocking `accept`) lets
        the loop notice a stop request promptly -- otherwise stop() would
        return while the port was still bound and accepting.
        """
        try:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([srv], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    client, _addr = srv.accept()
                except OSError:
                    break
                self._bump("accepted")
                threading.Thread(target=self._handle_client, args=(client,),
                                 daemon=True).start()
        finally:
            try:
                srv.close()
            except OSError:
                pass
            with self._lock:
                if self._srv is srv:
                    self._srv = None

    def _handle_client(self, client: socket.socket) -> None:
        self._track(client)
        try:
            client.settimeout(30)
            head = _recv_headers(client)
            if not head:
                return
            line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = line.split(" ")
            if len(parts) < 2:
                self._reply(client, "400 Bad Request")
                return
            method, target = parts[0].upper(), parts[1]

            if method == "CONNECT":
                host, port = _split_hostport(target, 443)
                self._open_tunnel(client, host, port)
            else:
                self._relay_plain(client, head, method, target)
        except (socket.timeout, TimeoutError):
            pass
        except ValueError as exc:
            self.log("debug", f"bad request: {exc}")
            self._reply(client, "400 Bad Request")
        except OSError:
            pass
        except Exception as exc:                       # never kill the thread
            self.log("debug", f"handler error: {type(exc).__name__}: {exc}")
        finally:
            self._untrack(client)
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _reply(client: socket.socket, status: str) -> None:
        try:
            body = f"{status}\n".encode()
            client.sendall(b"HTTP/1.1 " + status.encode() +
                           b"\r\nContent-Type: text/plain\r\nContent-Length: "
                           + str(len(body)).encode() +
                           b"\r\nConnection: close\r\n\r\n" + body)
        except OSError:
            pass

    # -- opening a channel to the target through an upstream -------------
    def _open_channel(self, node: Node, host: str,
                      port: int) -> tuple[socket.socket, bytes]:
        """Dial (host, port) through `node`.

        HTTP upstreams are asked with CONNECT; SOCKS upstreams complete
        their own handshake. Returns (socket, any bytes already buffered).
        On failure the socket is closed and the error re-raised.
        """
        timeout = float(self.settings["connect_timeout"])
        if node.proto != "http":
            # a SOCKS handshake *is* the tunnel, so nothing can arrive early
            return _socks_dial(node.proto, node.address, host, port, timeout), b""

        up = socket.create_connection(node.address, timeout=timeout)
        up.settimeout(timeout)
        try:
            up.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Proxy-Connection: keep-alive\r\n\r\n".encode("latin-1"))
            head, early = _read_header_block(up)
            status = head.split(b"\r\n", 1)[0]
            if not _status_line_is_ok(status):
                # remember *why* it failed: a 407 means "won't tunnel for us",
                # which is a capability issue, not a dead proxy.
                if _is_upstream_rejection(status):
                    node.connect_ok = False
                raise OSError("upstream said "
                              + status.decode("latin-1", "replace")[:60])
            return up, early
        except Exception:
            try:
                up.close()
            except OSError:
                pass
            raise

    # -- CONNECT (HTTPS) --------------------------------------------------
    def _open_tunnel(self, client: socket.socket, host: str, port: int) -> None:
        for node in self._candidates(connect=True):
            try:
                t0 = time.monotonic()
                up, early = self._open_channel(node, host, port)

                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                # BUG FIX: drop the dial timeout so an idle tab/websocket
                # doesn't get killed after 10 seconds of silence.
                up.settimeout(None)
                client.settimeout(None)
                node.connect_ok = True
                self._note_success(node, (time.monotonic() - t0) * 1000)
                self._bump("served")
                self.log("debug", f"tunnel {host}:{port} via {node.label}")
                # `early` is any target data the upstream already buffered
                # alongside its 200 -- it must not be dropped.
                self._pipe(client, up, primed=early)
                return
            except Exception as exc:
                self._note_failure(node, str(exc)[:80] or "connect failed")
        self._bump("failed")
        self._reply(client, "502 Bad Gateway")
        self.log("error", f"no upstream could reach {host}:{port}"
                          f"{self._no_upstream_note()}")

    # -- plain HTTP -------------------------------------------------------
    def _relay_plain(self, client: socket.socket, head: bytes,
                     method: str, target: str) -> None:
        host, port, path = self._origin_of(head, method, target)
        if not host:
            self._reply(client, "400 Bad Request")
            return

        headers, _, body_prefix = _split_head(head)
        # An HTTP upstream expects an absolute-form request target; a SOCKS
        # upstream hands us a raw socket to the target, so it gets origin-form.
        absolute = (target if target.startswith(("http://", "https://"))
                    else f"http://{host}:{port}{path}")
        timeout = float(self.settings["connect_timeout"])

        # Expect: 100-continue -- answer the client ourselves so it starts
        # sending the body instead of stalling for its own timeout. The
        # upstream never sees the header (stripped when heads are built).
        if any(k.lower() == "expect" and "100-continue" in v.lower()
               for k, v in headers):
            try:
                client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            except OSError:
                return

        # Only bodiless requests can rotate after the answer arrived: a
        # POST body has already been consumed from the client by then and
        # cannot be replayed through a different exit.
        replayable = (not body_prefix
                      and not any(k.lower() in ("content-length",
                                                "transfer-encoding")
                                  for k, _ in headers))
        rotate = self.rotate_codes if replayable else frozenset()
        candidates = self._candidates()

        for i, node in enumerate(candidates):
            up = None
            try:
                if node.proto == "http":
                    up = socket.create_connection(node.address, timeout=timeout)
                    up.settimeout(timeout)
                    out_head = self._build_upstream_head(method, absolute,
                                                         host, port, headers)
                else:
                    up = _socks_dial(node.proto, node.address, host, port, timeout)
                    out_head = self._build_upstream_head(method, path,
                                                         host, port, headers)
                up.sendall(out_head)

                # forward the request body, whatever its framing
                sent = self._forward_body(client, up, headers, body_prefix)
                if sent is None:
                    raise OSError("client vanished mid-body")

                # Read the first response chunk *before* committing to this
                # upstream, so a proxy-level rejection (407/502/503/504) can
                # fail over instead of being handed to the client.
                first = up.recv(RELAY_CHUNK)
                if not first:
                    raise OSError("upstream closed without responding")
                status = first.split(b"\r\n", 1)[0]
                if _is_upstream_rejection(status):
                    raise OSError("upstream said "
                                  + status.decode("latin-1", "replace")[:60])

                # The target itself said no (403/429/…): the proxy worked,
                # but this exit's IP is likely on a blocklist -- so count it
                # and, while other candidates remain, retry from a different
                # IP. On the last candidate the answer is passed through.
                code = _status_code(status)
                if rotate and code in rotate:
                    with self._lock:
                        node.blocks += 1
                    if i < len(candidates) - 1:
                        with self._lock:
                            node.sample(0.0)
                        self.log("info",
                                 f"{method} {host}{path} refused via "
                                 f"{node.label} ({code}) — rotating to "
                                 f"another exit")
                        try:
                            up.close()
                        except OSError:
                            pass
                        continue

                up.settimeout(None)
                client.settimeout(None)
                self._note_success(node)
                self._bump("served")
                self.log("debug", f"{method} {host}:{port}{path} via {node.label}")
                # BUG FIX: relay the real response (and any follow-up
                # traffic) instead of assuming GET succeeded.
                self._pipe(client, up, primed=first)
                return
            except Exception as exc:
                if up is not None:
                    try:
                        up.close()
                    except OSError:
                        pass
                self._note_failure(node, str(exc)[:80] or "relay failed")
        self._bump("failed")
        self._reply(client, "502 Bad Gateway")
        self.log("error", f"no upstream could reach {host}:{port}{path}"
                          f"{self._no_upstream_note()}")

    @staticmethod
    def _origin_of(head: bytes, method: str, target: str) -> tuple[str, int, str]:
        """Return (host, port, path) for an absolute-form or origin-form target."""
        host_header = ""
        for raw in head.split(b"\r\n")[1:]:
            if raw.lower().startswith(b"host:"):
                host_header = raw.split(b":", 1)[1].strip().decode("latin-1")
                break

        if target.startswith("http://") or target.startswith("https://"):
            rest = target.split("://", 1)[1]
            authority, _, path = rest.partition("/")
            path = "/" + path
            host, port = _split_hostport(authority, 443 if target.startswith("https") else 80)
            return host, port, path

        if target.startswith("/"):
            path = target
        elif target in ("*", ""):
            path = "/"
        else:                                          # authority-form
            host, port = _split_hostport(target, 80)
            return host, port, "/"

        if not host_header:
            return "", 0, path
        host, port = _split_hostport(host_header, 80)
        return host, port, path

    @staticmethod
    def _build_upstream_head(method: str, request_target: str, host: str, port: int,
                             headers: list[tuple[str, str]]) -> bytes:
        """`request_target` is absolute-form for HTTP upstreams and
        origin-form (a plain path) for SOCKS upstreams.

        Hop-by-hop headers never cross the proxy: the client's own
        `Connection` tokens (RFC 7230 §6.1), the proxy-protocol headers and
        `Expect` are dropped, while everything end-to-end -- cookies, User-
        Agent, Accept, Authorization -- is forwarded byte for byte, so the
        target sees exactly the headers the client sent.
        """
        drop = {"proxy-connection", "proxy-authorization", "proxy-authenticate",
                "expect", "te", "keep-alive"}
        for name, value in headers:
            if name.lower() == "connection":
                drop.update(tok.strip().lower() for tok in value.split(",")
                            if tok.strip())
        lines = [f"{method} {request_target} HTTP/1.1"]
        seen_connection = False
        for name, value in headers:
            low = name.lower()
            if low in drop:
                continue
            if low == "connection":
                value, seen_connection = "close", True
            if low == "host":
                value = host if port == 80 else f"{host}:{port}"
            lines.append(f"{name}: {value}")
        if not seen_connection:
            lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    def _forward_body(client: socket.socket, up: socket.socket,
                      headers: list[tuple[str, str]], prefix: bytes) -> int | None:
        """Copy the client's request body upstream. Returns bytes sent, or
        None if the client disconnected. Handles Content-Length, chunked
        framing and no-body requests.

        The body does not have to arrive glued to the headers: with
        `Expect: 100-continue` (or simply a split TCP segment) the client
        sends it only after we have answered, so an empty prefix must
        still read whatever the framing declares.
        """
        low = {n.lower(): v for n, v in headers}
        if "content-length" in low:
            remaining = int(low["content-length"])
            chunks = [prefix] if prefix else []
            got = len(prefix)
            while got < remaining:
                piece = client.recv(min(RELAY_CHUNK, remaining - got))
                if not piece:
                    return None
                chunks.append(piece)
                got += len(piece)
            up.sendall(b"".join(chunks))
            return got

        if "transfer-encoding" in low and "chunked" in low["transfer-encoding"].lower():
            return _forward_chunked(client, up, prefix)

        # no declared body: anything already read is pipelining, send as-is
        if prefix:
            up.sendall(prefix)
            return len(prefix)
        return 0

    # -- relay ------------------------------------------------------------
    def _pipe(self, a: socket.socket, b: socket.socket,
              primed: bytes = b"") -> None:
        """Bidirectional relay until either side closes, idle timeout, or stop.

        `primed` is a chunk already read from `b` (the upstream); it is
        delivered to `a` (the client) first so nothing observed while we were
        still deciding whether to trust this upstream gets lost.
        """
        idle = float(self.settings["idle_timeout"])
        last = time.monotonic()
        try:
            if primed:
                a.sendall(primed)
                self._bytes(up=0, down=len(primed))
                last = time.monotonic()
            while not self._stop.is_set():
                ready, _, _ = select.select([a, b], [], [], 1.0)
                if not ready:
                    if time.monotonic() - last > idle:
                        self.log("debug", "relay idle timeout")
                        break
                    continue
                for s in ready:
                    other = b if s is a else a
                    data = s.recv(RELAY_CHUNK)
                    if not data:
                        return
                    last = time.monotonic()
                    other.sendall(data)
                    if s is a:
                        self._bytes(up=len(data), down=0)
                    else:
                        self._bytes(up=0, down=len(data))
        except (OSError, socket.timeout):
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    # -- health checks ----------------------------------------------------
    def probe(self, node: Node) -> tuple[bool, float | None, str, bool | None]:
        """One liveness probe.

        Returns (alive, latency_ms, error, connect_ok) where `connect_ok` is
        True when the upstream can open an HTTPS tunnel, False when it
        refused one (407 etc.), and None when it couldn't be determined.
        """
        timeout = float(self.settings["probe_timeout"])
        try:
            if node.proto == "http":
                return self._probe_http(node, timeout)
            return self._probe_socks(node, timeout)
        except Exception as exc:
            return False, None, str(exc)[:80] or type(exc).__name__, None

    def _probe_http(self, node: Node, timeout: float):
        t0 = time.monotonic()
        sock = None
        try:
            sock = socket.create_connection(node.address, timeout=timeout)
            sock.settimeout(timeout)
            sock.sendall(b"GET http://example.com/ HTTP/1.1\r\n"
                         b"Host: example.com\r\n"
                         + _PROBE_UA +
                         b"Connection: close\r\n\r\n")
            data = sock.recv(256)
            # any HTTP answer proves the hop forwards traffic (200, 30x, 403…)
            alive = data.startswith(b"HTTP/") or b"200" in data or b"30" in data
            err = "" if alive else f"unexpected reply {data[:40]!r}"
            latency = (time.monotonic() - t0) * 1000
            if not alive:
                return False, latency, err, None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.settings.get("probe_connect"):
            return True, latency, "", self._probe_connect(node, timeout)
        return True, latency, "", None

    def _probe_socks(self, node: Node, timeout: float):
        """SOCKS upstream: open a tunnel and speak HTTP over it."""
        t0 = time.monotonic()
        sock = _socks_dial(node.proto, node.address, "example.com", 80, timeout)
        try:
            sock.sendall(b"GET / HTTP/1.1\r\n"
                         b"Host: example.com\r\n"
                         + _PROBE_UA +
                         b"Connection: close\r\n\r\n")
            data = sock.recv(256)
            alive = data.startswith(b"HTTP/") or b"200" in data or b"30" in data
            err = "" if alive else f"unexpected reply {data[:40]!r}"
            latency = (time.monotonic() - t0) * 1000
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if not alive:
            return False, latency, err, None
        if self.settings.get("probe_connect"):
            return True, latency, "", self._probe_socks_tunnel(node, timeout)
        return True, latency, "", None

    def _probe_socks_tunnel(self, node: Node, timeout: float) -> bool:
        """SOCKS gives a raw tunnel, so the only question is :443 reachability."""
        try:
            sock = _socks_dial(node.proto, node.address, "example.com", 443, timeout)
        except Exception:
            return False
        try:
            sock.close()
        except OSError:
            pass
        return True

    def _probe_connect(self, node: Node, timeout: float) -> bool | None:
        """Second opinion: will this upstream open a CONNECT tunnel?

        Most free proxies serve plain HTTP but answer 407 to CONNECT, which
        is exactly why HTTPS requests used to fail at random.
        """
        sock = None
        try:
            sock = socket.create_connection((node.host, node.port), timeout=timeout)
            sock.settimeout(timeout)
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n"
                         b"Host: example.com:443\r\n"
                         b"Proxy-Connection: close\r\n\r\n")
            head, _ = _read_header_block(sock, limit=8192)
            parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
            if len(parts) >= 2 and parts[1][:1] == b"2":
                return True
            if len(parts) >= 2 and parts[1] in (UPSTREAM_REJECTIONS | {b"400", b"403"}):
                return False
            return None
        except Exception:
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def check_now(self, *, reason: str = "manual") -> bool:
        """Run a full sweep in the background. Returns False if one is running."""
        if self._checking.is_set():
            self.log("info", "a health check is already running")
            return False
        threading.Thread(target=self._run_sweep, args=(reason,),
                         name="health", daemon=True).start()
        return True

    def _run_sweep(self, reason: str) -> None:
        if self._checking.is_set():
            return
        self._checking.set()
        t0 = time.monotonic()
        try:
            with self._lock:
                targets = list(self._nodes)
            total = len(targets)
            if not total:
                self.log("warn", "health check skipped: no upstreams configured")
                return
            with self._lock:
                self.stats["check_progress"] = (0, total)
            self.log("info", f"checking {total} upstreams ({reason})…")

            done = 0
            revived = failed = tunnelled = 0
            # BUG FIX: parallel sweep -- 300 proxies at ~4 s each would take
            # 20+ minutes serially; this finishes in seconds.
            with ThreadPoolExecutor(max_workers=int(self.settings["health_workers"]),
                                    thread_name_prefix="probe") as pool:
                for node, (alive, latency, err, connect_ok) in zip(
                        targets, pool.map(self._probe_wrapped, targets)):
                    done += 1
                    with self._lock:
                        node.status = "alive" if alive else "dead"
                        node.latency = latency
                        node.last_check = time.time()
                        node.last_error = "" if alive else err
                        node.failures = 0 if alive else node.failures + 1
                        node.sample(1.0 if alive else 0.0)
                        if connect_ok is not None:
                            node.connect_ok = connect_ok
                        if node.connect_ok:
                            tunnelled += 1
                        self.stats["check_progress"] = (done, total)
                        self.stats["last_check_at"] = time.time()
                    if alive:
                        revived += 1
                    else:
                        failed += 1
                    if done % 50 == 0 or done == total:
                        self.log("debug", f"probe progress {done}/{total}")

            secs = time.monotonic() - t0
            with self._lock:
                alive = sum(1 for n in self._nodes if n.status == "alive")
                https = sum(1 for n in self._nodes
                            if n.status == "alive" and n.connect_ok)
                strong = sum(1 for n in self._nodes if n.strength == "Strong")
                code = str(self.settings.get("country") or "").upper()
                https_only = bool(self.settings.get("https_only"))
                scoped = [n for n in self._nodes if n.cc == code] if code else []
                scoped_alive = sum(1 for n in scoped if n.status == "alive")
                name = next((n.country for n in scoped if n.country), "")
            msg = (f"health check done in {secs:.1f}s — "
                   f"{alive} alive ({https} tunnel HTTPS) of {total}"
                   f" · {strong} strong")
            if code:
                msg += (f" · {name or code} ({code}): "
                        f"{scoped_alive} alive of {len(scoped)}"
                        if scoped else
                        f" · no upstreams configured for {name or code} ({code})")
            if https_only:
                usable = sum(1 for n in self._nodes if n.connect_ok is not False)
                msg += (f" · HTTPS-only: {usable} can tunnel of {total}"
                        if usable else
                        f" · HTTPS-only: none of these {total} can tunnel")
            self.log("info", msg)
        except Exception as exc:
            self.log("error", f"health check failed: {exc}")
        finally:
            with self._lock:
                self.stats["check_progress"] = (0, 0)
            self._checking.clear()

    def _probe_wrapped(self, node: Node):
        return self.probe(node)

    # -- periodic sweep ---------------------------------------------------
    def _health_loop(self) -> None:
        while not self._stop.wait(float(self.settings["check_interval"])):
            self._run_sweep(reason="interval")

    def start_health_loop(self) -> None:
        if any(t.name == "health-loop" for t in self._threads):
            return
        t = threading.Thread(target=self._health_loop, name="health-loop",
                             daemon=True)
        t.start()
        self._threads.append(t)

    def _no_upstream_note(self) -> str:
        """Suffix explaining a "nothing worked" line when a scope is set."""
        code = self.country
        if not code and not self.https_only:
            return ""
        if code:
            name = self._country_name(code)
            note = f"restricted to {name or code} ({code})"
            if self.https_only:
                note += " and HTTPS-capable upstreams"
            return f" [{note}]"
        return " [restricted to HTTPS-capable upstreams]"

    def _warn_scope(self, *what: str) -> None:
        """Say once in a while that the chosen scope has nothing usable."""
        now = time.time()
        if now - self._last_scope_warn < 15:
            return
        self._last_scope_warn = now
        parts = []
        for item in what:
            if item == "https":
                parts.append("no upstream here can tunnel HTTPS")
            else:
                name = self._country_name(item)
                parts.append(f"no usable upstream in {name or item} ({item})")
        self.log("warn", f"{' and '.join(parts)} — "
                         "requests will 502 until one comes back")

    def _scope_note(self) -> str:
        """Short "what selection is restricted to" phrase, "" when free."""
        bits = []
        code = self.country
        if code:
            name = self._country_name(code)
            bits.append(f"exit {name or code} ({code})")
        if self.https_only:
            bits.append("HTTPS-capable only")
        return " · ".join(bits)

    # -- reporting --------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            counts = {"alive": 0, "dead": 0, "unknown": 0, "checking": 0}
            for n in self._nodes:
                counts[n.status] = counts.get(n.status, 0) + 1
            snap = dict(self.stats)
            countries: dict[str, dict] = {}
            for n in self._nodes:
                if not n.cc:
                    continue
                entry = countries.setdefault(
                    n.cc, {"cc": n.cc, "name": n.country,
                           "total": 0, "alive": 0})
                entry["total"] += 1
                if n.status == "alive":
                    entry["alive"] += 1
                if not entry["name"] and n.country:
                    entry["name"] = n.country
            snap.update(
                total=len(self._nodes),
                country=str(self.settings.get("country") or "").upper(),
                https_only=bool(self.settings.get("https_only")),
                scope=self._scope_note(),
                pool_countries=countries,
                pool_alive=counts["alive"],
                pool_dead=counts["dead"],
                pool_unknown=counts["unknown"],
                pool_https=sum(1 for n in self._nodes if n.connect_ok),
                pool_socks=sum(1 for n in self._nodes if n.proto != "http"),
                pool_strong=sum(1 for n in self._nodes
                                if n.strength == "Strong"),
                host=self.host,
                port=self.port,
                checking=self._checking.is_set(),
                progress=self.stats["check_progress"],
                uptime=(time.time() - self.stats["started_at"]
                        if self.stats["started_at"] and self.running else 0),
                settings=dict(self.settings),
            )
        return snap


# ---------------------------------------------------------------------------
# small shared utilities
# ---------------------------------------------------------------------------
def _split_head(head: bytes) -> tuple[list[tuple[str, str]], bytes, bytes]:
    """Split a raw header block into [(name, value)…], the blank line and
    any bytes that followed it (start of the body)."""
    marker = head.find(b"\r\n\r\n")
    if marker < 0:
        return [], b"", b""
    block, rest = head[:marker], head[marker + 4:]
    lines = block.split(b"\r\n")[1:]
    headers = []
    for raw in lines:
        if b":" not in raw:
            continue
        name, _, value = raw.partition(b":")
        headers.append((name.decode("latin-1").strip(),
                        value.decode("latin-1").strip()))
    return headers, b"\r\n\r\n", rest


def _forward_chunked(client: socket.socket, up: socket.socket,
                     prefix: bytes) -> int | None:
    """Stream a chunked request body from client to upstream."""
    buf = prefix
    sent = 0
    while True:
        while b"\r\n" not in buf:
            piece = client.recv(4096)
            if not piece:
                return None
            buf += piece
        line, _, buf = buf.partition(b"\r\n")
        try:
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        except ValueError:
            raise ValueError(f"bad chunk size {line!r}")
        while len(buf) < size + 2:                     # body + trailing CRLF
            piece = client.recv(min(RELAY_CHUNK, size + 2 - len(buf)))
            if not piece:
                return None
            buf += piece
        frame = buf[:size + 2]
        buf = buf[size + 2:]
        up.sendall(line + b"\r\n" + frame)
        sent += size + len(line) + 2
        if size == 0:                                  # last chunk (+ trailers)
            if buf:
                up.sendall(buf)
                sent += len(buf)
            while not buf.endswith(b"\r\n\r\n"):
                piece = client.recv(4096)
                if not piece:
                    break
                up.sendall(piece)
                buf += piece
            return sent
