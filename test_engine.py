#!/usr/bin/env python3
"""
Tests for the rotating proxy engine.

Runs a local target HTTP server plus a mock upstream proxy on 127.0.0.1, so
the CONNECT tunnel, the plain-HTTP relay (GET *and* POST bodies) and the
health checker can all be verified without touching the real internet.

    python3 test_engine.py
"""

import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from engine import RotatingProxy, parse_proxy, _split_hostport

FREE_PORT = ("127.0.0.1", 0)

# The engine's probes always dial "example.com" -- that name is the
# subject of the handshake, not of the test.  Reaching the real one made
# the suite depend on the network: the mocks' DNS lookup goes through the
# resolver like any other process, and a resolver that stalls for a couple
# of seconds pushed a probe past its 4 s budget and failed the health
# checks at random.  The name therefore resolves to the local target
# server, which is what the header of this file promises anyway.
_TARGET_PORT = 0


def _dial_upstream(host: str, port: int, timeout: float = 5) -> socket.socket:
    """Connect where an upstream proxy would: locally for `example.com`."""
    if host == "example.com" and _TARGET_PORT:
        host, port = "127.0.0.1", _TARGET_PORT
    return socket.create_connection((host, port), timeout=timeout)


# ---------------------------------------------------------------------------
# local target: answers GET and POST
# ---------------------------------------------------------------------------
class Target(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"TARGET-GET " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        reply = f"TARGET-POST len={len(body)} body=".encode() + body
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def start_target() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# mock upstream proxy: supports CONNECT + absolute-form requests
# ---------------------------------------------------------------------------
def _read_head(sock) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _pump(a, b):
    import select
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 5)
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass


def mock_upstream(sock: socket.socket, alive: bool = True,
                  reject_status: str | None = None, early: bytes = b""):
    """One proxied connection.

    alive=False         -> refuse everything with a 502
    reject_status="407" -> answer with that proxy-level rejection
    early=b"MARKER"     -> bytes sent alongside a CONNECT's 200 (tests that
                           data buffered with the handshake isn't dropped)
    """
    try:
        head = _read_head(sock)
        if not head:
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, *_ = line.split(" ")

        if reject_status:
            sock.sendall((f"HTTP/1.1 {reject_status} Rejected\r\n"
                          "Content-Length: 0\r\n"
                          "Connection: close\r\n\r\n").encode())
            return

        if not alive:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return

        if method == "CONNECT":
            host, port = _split_hostport(target, 443)
            upstream = _dial_upstream(host, port)
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" + early)
            _pump(sock, upstream)
            upstream.close()
            return

        # absolute-form plain HTTP
        rest = target.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        host, port = _split_hostport(authority, 80)
        upstream = _dial_upstream(host, port)

        # forward headers, then any body already buffered with them
        marker = head.find(b"\r\n\r\n")
        headers, prefix = head[:marker + 4], head[marker + 4:]
        lines = headers.split(b"\r\n")
        lines[0] = f"{method} /{path} HTTP/1.1".encode("latin-1")
        rebuilt = b"\r\n".join(lines)
        upstream.sendall(rebuilt)

        clen = 0
        for raw in headers.split(b"\r\n")[1:]:
            if raw.lower().startswith(b"content-length:"):
                clen = int(raw.split(b":", 1)[1])
        got = len(prefix)
        upstream.sendall(prefix)
        while got < clen:
            piece = sock.recv(min(65536, clen - got))
            if not piece:
                break
            upstream.sendall(piece)
            got += len(piece)

        _pump(sock, upstream)
        upstream.close()
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def start_mock(alive: bool = True, *, reject_status: str | None = None,
               early: bytes = b"") -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    port = srv.getsockname()[1]

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=mock_upstream,
                             args=(c, alive, reject_status, early),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return port


# ---------------------------------------------------------------------------
# mock SOCKS4 / SOCKS5 upstreams
# ---------------------------------------------------------------------------
def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("short read")
        buf += chunk
    return buf


def _read_cstring(sock) -> bytes:
    out = b""
    while True:
        b = sock.recv(1)
        if not b or b == b"\x00":
            return out
        out += b


def socks_session(sock, version: int) -> None:
    """Speak SOCKS4/SOCKS4a or SOCKS5, then relay to the requested target."""
    up = None
    try:
        if version == 4:
            hdr = _read_exact(sock, 8)
            port = int.from_bytes(hdr[2:4], "big")
            ip = hdr[4:8]
            _read_cstring(sock)                                   # userid
            host = socket.inet_ntoa(ip)
            if ip[:3] == b"\x00\x00\x00" and ip[3] != 0:          # SOCKS4a
                host = _read_cstring(sock).decode()
            up = _dial_upstream(host, port)
            sock.sendall(b"\x00\x5a" + hdr[2:8])                  # granted
        else:
            ver, n = _read_exact(sock, 2)
            _read_exact(sock, n)                                  # methods
            sock.sendall(b"\x05\x00")                             # no-auth
            hdr = _read_exact(sock, 4)                            # ver cmd rsv atyp
            atyp = hdr[3]
            if atyp == 1:
                host = socket.inet_ntoa(_read_exact(sock, 4))
            elif atyp == 3:
                host = _read_exact(sock, _read_exact(sock, 1)[0]).decode()
            else:
                _read_exact(sock, 16)
                host = "::1"
            port = int.from_bytes(_read_exact(sock, 2), "big")
            up = _dial_upstream(host, port)
            sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)       # succeeded
        _pump(sock, up)
    except OSError:
        pass
    finally:
        if up is not None:
            try:
                up.close()
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass


def start_socks(version: int) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    port = srv.getsockname()[1]

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=socks_session, args=(c, version),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return port


# ---------------------------------------------------------------------------
# tiny HTTP client that speaks to our engine
# ---------------------------------------------------------------------------
def http_get(port: int, host: str, path: str, timeout=8) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(f"GET http://{host}{path} HTTP/1.1\r\n"
                  f"Host: {host}\r\nConnection: close\r\n\r\n".encode())
        return _drain(s)
    finally:
        s.close()


def http_post(port: int, host: str, path: str, payload: bytes, timeout=8) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(f"POST http://{host}{path} HTTP/1.1\r\n"
                  f"Host: {host}\r\nContent-Length: {len(payload)}\r\n"
                  f"Content-Type: text/plain\r\nConnection: close\r\n\r\n"
                  .encode() + payload)
        return _drain(s)
    finally:
        s.close()


def http_connect(port: int, hostport: str, inner: bytes, timeout=8) -> bytes:
    """CONNECT tunnel, then send `inner` through it."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(f"CONNECT {hostport} HTTP/1.1\r\n"
                  f"Host: {hostport}\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if b" 200 " not in buf.split(b"\r\n", 1)[0]:
            return buf
        s.sendall(inner)
        return buf + _drain(s)
    finally:
        s.close()


def _drain(sock) -> bytes:
    out = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        out += chunk
        if len(out) > 4_000_000:
            break
    return out


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


def quiet(*_a, **_k):
    pass


def test_parse():
    print("\nparse_proxy / _split_hostport")
    check("parses host:port", parse_proxy("1.2.3.4:8080") == ("1.2.3.4", 8080))
    check("trims whitespace", parse_proxy("  1.2.3.4:80 \n") == ("1.2.3.4", 80))
    for bad in ("1.2.3.4", "1.2.3.4:", ":80", "1.2.3.4:abc", "1.2.3.4:99999"):
        try:
            parse_proxy(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)
    check("split default port", _split_hostport("example.com", 443) == ("example.com", 443))
    check("split explicit port", _split_hostport("example.com:8443", 443) == ("example.com", 8443))
    check("split ipv6", _split_hostport("[::1]:9000", 443) == ("::1", 9000))


def test_plain_get(target_port, mock_port):
    print("\nplain HTTP GET through an upstream")
    logs = []
    eng = RotatingProxy(logger=lambda lvl, msg: logs.append((lvl, msg)),
                         proxies=[f"127.0.0.1:{mock_port}"])
    # bind an ephemeral port so tests never collide
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        raw = http_get(free, f"127.0.0.1:{target_port}", "/hello")
        status = raw.split(b"\r\n", 1)[0]
        check("returns 200", b" 200 " in status + b" ", status.decode(errors="replace"))
        check("body relayed", b"TARGET-GET /hello" in raw)
        check("served counted", eng.snapshot()["served"] == 1,
              str(eng.snapshot()["served"]))
        check("upstream marked alive",
              eng.proxies("alive") == [f"127.0.0.1:{mock_port}"],
              str(eng.proxies()))
    finally:
        eng.stop()


def test_post_body(target_port, mock_port):
    print("\nplain HTTP POST with a request body (was hardcoded GET)")
    eng = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{mock_port}"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        payload = b"x" * 50_000                       # larger than one read
        raw = http_post(free, f"127.0.0.1:{target_port}", "/upload", payload)
        check("POST returns 200", b" 200 " in raw.split(b"\r\n", 1)[0] + b" ",
              raw[:60].decode(errors="replace"))
        check("full body arrived",
              b"TARGET-POST len=50000" in raw,
              raw.split(b"\r\n\r\n")[-1][:80].decode(errors="replace"))
    finally:
        eng.stop()


def test_connect(target_port, mock_port):
    print("\nCONNECT tunnel through an upstream")
    eng = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{mock_port}"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        inner = (f"GET /tun HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n"
                 f"Connection: close\r\n\r\n").encode()
        raw = http_connect(free, f"127.0.0.1:{target_port}", inner)
        check("tunnel established",
              b"200" in raw.split(b"\r\n", 1)[0], raw[:60].decode(errors="replace"))
        check("traffic flows", b"TARGET-GET /tun" in raw)
    finally:
        eng.stop()


def test_connect_failover(target_port, dead_port, mock_port):
    print("\nfailover past a dead upstream")
    import engine as engine_mod
    original = engine_mod.random.sample
    # force configured order so the dead proxy is always the one tried first
    engine_mod.random.sample = lambda seq, k: list(seq)[:k]
    try:
        eng = RotatingProxy(logger=quiet,
                             proxies=[f"127.0.0.1:{dead_port}",
                                      f"127.0.0.1:{mock_port}"],
                             max_retries=5)
        free = _free_port()
        eng.configure(port=free)
        eng.start(probe_first=False)
        try:
            raw = http_get(free, f"127.0.0.1:{target_port}", "/fail")
            check("recovered via 2nd candidate", b"TARGET-GET /fail" in raw)
            dead = f"127.0.0.1:{dead_port}"
            check("dead one evicted", dead not in eng.proxies("alive"),
                  str(eng.proxies("alive")))
            check("survivor still configured", dead in eng.proxies("dead"),
                  str(eng.proxies("dead")))
            check("failed counter zero (request succeeded)",
                  eng.snapshot()["failed"] == 0)
        finally:
            eng.stop()
    finally:
        engine_mod.random.sample = original


def test_upstream_rejection_detection():
    print("\nupstream rejection detection")
    from engine import _is_upstream_rejection
    check("407 is a rejection",
          _is_upstream_rejection(b"HTTP/1.1 407 Proxy Authentication Required"))
    check("502 is a rejection", _is_upstream_rejection(b"HTTP/1.1 502 Bad Gateway"))
    check("503 is a rejection", _is_upstream_rejection(b"HTTP/1.1 503 Unavailable"))
    check("200 is not a rejection", not _is_upstream_rejection(b"HTTP/1.1 200 OK"))
    check("404 is not (that is the target's answer)",
          not _is_upstream_rejection(b"HTTP/1.1 404 Not Found"))
    check("garbage counts as a rejection", _is_upstream_rejection(b"nonsense"))


def test_reject_failover(target_port, reject_port, mock_port):
    print("\nfailover when an upstream answers 407")
    import engine as engine_mod
    original = engine_mod.random.sample
    # force configured order so the rejecting proxy is always tried first
    engine_mod.random.sample = lambda seq, k: list(seq)[:k]
    try:
        eng = RotatingProxy(logger=quiet,
                            proxies=[f"127.0.0.1:{reject_port}",
                                     f"127.0.0.1:{mock_port}"],
                            max_retries=5)
        free = _free_port()
        eng.configure(port=free)
        eng.start(probe_first=False)
        try:
            raw = http_get(free, f"127.0.0.1:{target_port}", "/reject")
            status = raw.split(b"\r\n", 1)[0]
            check("second upstream used", b"TARGET-GET /reject" in raw, raw[:90])
            check("no 407 leaked to the client", b"407" not in status,
                  status.decode(errors="replace"))
            check("rejector evicted",
                  f"127.0.0.1:{reject_port}" in eng.proxies("dead"),
                  str(eng.proxies("dead")))
            snap = eng.snapshot()
            check("still counted as served", snap["served"] == 1, str(snap))
            check("not counted as failed", snap["failed"] == 0, str(snap["failed"]))
        finally:
            eng.stop()
    finally:
        engine_mod.random.sample = original


def test_connect_early_data(target_port, early_port):
    print("\nCONNECT handshake that carries early data")
    eng = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{early_port}"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        inner = (f"GET /early HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n"
                 f"Connection: close\r\n\r\n").encode()
        raw = http_connect(free, f"127.0.0.1:{target_port}", inner)
        check("early bytes preserved", b"EARLY-DATA" in raw, raw[:140])
        check("tunnel still works", b"TARGET-GET /early" in raw, raw[:220])
    finally:
        eng.stop()


def test_no_upstream(target_port):
    print("\nno usable upstream -> 502, not a crash")
    # a port nothing listens on
    dead = _free_port()
    eng = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{dead}"],
                         connect_timeout=1.0)
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        raw = http_get(free, f"127.0.0.1:{target_port}", "/nope", timeout=6)
        check("answers 502", b" 502 " in raw.split(b"\r\n", 1)[0] + b" ",
              raw[:60].decode(errors="replace"))
        check("failure counted", eng.snapshot()["failed"] >= 1)
    finally:
        eng.stop()


def test_health_check(alive_port, dead_port):
    print("\nhealth check")
    eng = RotatingProxy(logger=quiet,
                         proxies=[f"127.0.0.1:{alive_port}", f"127.0.0.1:{dead_port}"],
                         probe_timeout=1.5)
    eng.check_now(reason="test")
    deadline = time.time() + 15
    while eng.checking and time.time() < deadline:
        time.sleep(0.05)
    nodes = {n["label"]: n for n in eng.nodes()}
    alive = f"127.0.0.1:{alive_port}"
    dead = f"127.0.0.1:{dead_port}"
    check("live upstream marked alive", nodes.get(alive, {}).get("status") == "alive",
          str(nodes.get(alive)))
    check("dead upstream marked dead", nodes.get(dead, {}).get("status") == "dead",
          str(nodes.get(dead)))
    check("latency recorded", isinstance(nodes.get(alive, {}).get("latency"), float),
          str(nodes.get(alive, {}).get("latency")))
    check("progress resets", eng.snapshot()["progress"] == (0, 0))


def test_entry_parsing():
    print("\nentry parsing / scheme labels / tabular import")
    from engine import parse_entry, iter_proxy_labels

    check("bare host:port is http",
          parse_entry("1.2.3.4:8080") == ("http", "1.2.3.4", 8080))
    check("explicit http scheme",
          parse_entry("http://1.2.3.4:8080") == ("http", "1.2.3.4", 8080))
    check("socks4 scheme",
          parse_entry("socks4://1.2.3.4:1080") == ("socks4", "1.2.3.4", 1080))
    check("socks4a scheme",
          parse_entry("socks4a://1.2.3.4:1080") == ("socks4", "1.2.3.4", 1080))
    check("socks5 scheme",
          parse_entry("socks5://1.2.3.4:1080") == ("socks5", "1.2.3.4", 1080))
    try:
        parse_entry("gopher://1.2.3.4:70")
        check("unknown scheme rejected", False)
    except ValueError:
        check("unknown scheme rejected", True)

    tsv = ("14.136.67.106\t1080\tHK\tHong Kong\tSocks4\tAnonymous\tYes\t"
           "1 min ago")
    check("tabular SOCKS4 row",
          iter_proxy_labels(tsv) == ["socks4://14.136.67.106:1080"],
          str(iter_proxy_labels(tsv)))
    check("tabular HTTP row",
          iter_proxy_labels("1.2.3.4 8080 US United States HTTP Anonymous")
          == ["1.2.3.4:8080"],
          str(iter_proxy_labels("1.2.3.4 8080 US United States HTTP Anonymous")))
    check("header row ignored",
          iter_proxy_labels("IP\tPort\tCode\tCountry\tType") == [],
          str(iter_proxy_labels("IP\tPort\tCode\tCountry\tType")))
    check("mixed list dedupes",
          iter_proxy_labels("1.1.1.1:80\nsocks4://2.2.2.2:1080\n1.1.1.1:80")
          == ["1.1.1.1:80", "socks4://2.2.2.2:1080"])
    check("set_proxies keeps the scheme",
          RotatingProxy(logger=quiet,
                        proxies=["1.1.1.1:80", "socks4://2.2.2.2:1080"]).proxies()
          == ["1.1.1.1:80", "socks4://2.2.2.2:1080"])


def _socks_engine(port: int, proto: str) -> tuple[RotatingProxy, int]:
    eng = RotatingProxy(logger=quiet, proxies=[f"{proto}://127.0.0.1:{port}"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    return eng, free


def test_socks4_relay(target_port, socks4_port):
    print("\nGET / POST through a SOCKS4 upstream")
    eng, free = _socks_engine(socks4_port, "socks4")
    label = f"socks4://127.0.0.1:{socks4_port}"
    try:
        raw = http_get(free, f"127.0.0.1:{target_port}", "/s4")
        check("GET returns 200", b" 200 " in raw.split(b"\r\n", 1)[0] + b" ",
              raw[:70].decode(errors="replace"))
        check("GET body relayed", b"TARGET-GET /s4" in raw)
        check("upstream marked alive", eng.proxies("alive") == [label],
              str(eng.proxies()))

        payload = b"y" * 30_000
        raw = http_post(free, f"127.0.0.1:{target_port}", "/up", payload)
        check("POST body relayed", b"TARGET-POST len=30000" in raw,
              raw[-90:].decode(errors="replace"))
    finally:
        eng.stop()


def test_socks5_relay(target_port, socks5_port):
    print("\nGET / POST through a SOCKS5 upstream")
    eng, free = _socks_engine(socks5_port, "socks5")
    label = f"socks5://127.0.0.1:{socks5_port}"
    try:
        raw = http_get(free, f"127.0.0.1:{target_port}", "/s5")
        check("GET returns 200", b" 200 " in raw.split(b"\r\n", 1)[0] + b" ",
              raw[:70].decode(errors="replace"))
        check("GET body relayed", b"TARGET-GET /s5" in raw)
        check("upstream marked alive", eng.proxies("alive") == [label],
              str(eng.proxies()))

        payload = b"z" * 30_000
        raw = http_post(free, f"127.0.0.1:{target_port}", "/up", payload)
        check("POST body relayed", b"TARGET-POST len=30000" in raw,
              raw[-90:].decode(errors="replace"))
    finally:
        eng.stop()


def test_socks_connect(target_port, socks4_port, socks5_port):
    print("\nCONNECT tunnel through SOCKS upstreams")
    inner = (f"GET /tun HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n"
             f"Connection: close\r\n\r\n").encode()
    for version, port in ((4, socks4_port), (5, socks5_port)):
        eng, free = _socks_engine(port, f"socks{version}")
        try:
            raw = http_connect(free, f"127.0.0.1:{target_port}", inner)
            check(f"socks{version} tunnel established",
                  b"TARGET-GET /tun" in raw, raw[:70].decode(errors="replace"))
        finally:
            eng.stop()


def test_socks_health(socks4_port, socks5_port, dead_port):
    print("\nhealth check for SOCKS upstreams")
    s4 = f"socks4://127.0.0.1:{socks4_port}"
    s5 = f"socks5://127.0.0.1:{socks5_port}"
    dead = f"socks4://127.0.0.1:{dead_port}"
    eng = RotatingProxy(logger=quiet, proxies=[s4, s5, dead],
                        probe_timeout=4.0)   # the engine's default; the
                                             # 2.0 this test used to use
                                             # flakes on a loaded desktop
    eng.check_now(reason="test")
    deadline = time.time() + 20
    while eng.checking and time.time() < deadline:
        time.sleep(0.05)
    nodes = {n["label"]: n for n in eng.nodes()}
    check("socks4 marked alive", nodes.get(s4, {}).get("status") == "alive",
          str(nodes.get(s4)))
    check("socks5 marked alive", nodes.get(s5, {}).get("status") == "alive",
          str(nodes.get(s5)))
    check("dead socks marked dead", nodes.get(dead, {}).get("status") == "dead",
          str(nodes.get(dead)))
    check("https reachability recorded",
          nodes.get(s4, {}).get("connect_ok") is True,
          str(nodes.get(s4, {}).get("connect_ok")))
    check("snapshot counts socks pool",
          eng.snapshot()["pool_socks"] == 3, str(eng.snapshot()["pool_socks"]))


def test_stats_and_list_ops():
    print("\nproxy list bookkeeping")
    eng = RotatingProxy(logger=quiet, proxies=["1.1.1.1:80", "1.1.1.1:80", "junk"])
    check("dedupes", eng.proxies() == ["1.1.1.1:80"], str(eng.proxies()))
    added = eng.add_proxies("2.2.2.2:8080\n3.3.3.3:9090, 2.2.2.2:8080")
    check("adds 2 unique", added == 2, str(eng.proxies()))
    check("rejects junk on add", eng.add_proxies("nope") == 0)
    removed = eng.remove_proxies(["1.1.1.1:80"])
    check("removes 1", removed == 1 and eng.proxies() == ["2.2.2.2:8080", "3.3.3.3:9090"],
          str(eng.proxies()))
    check("snapshot shape",
          {"total", "pool_alive", "served", "active", "uptime"} <= set(eng.snapshot()))


def test_country():
    print("\nexit country selection")
    import geodb
    from engine import (RotatingProxy, _guess_cc, iter_proxy_entries,
                        resolve_country)

    # -- reading the country column off a tabular export -------------------
    rows = iter_proxy_entries(
        "14.136.67.106\t1080\tHK\tHong Kong\tSocks4\tAnonymous\tYes\t"
        "1 min ago\n1.2.3.4:8080")
    check("tsv country column read",
          rows == [("socks4://14.136.67.106:1080", "HK"),
                   ("1.2.3.4:8080", "")], str(rows))
    check("no code without a column", _guess_cc(["1.2.3.4", "8080"]) == "")
    check("scheme is not a code", _guess_cc(["socks4://1.2.3.4:1080"]) == "")
    check("code normalised to upper", _guess_cc(["us"]) == "US")

    real = geodb.lookup
    try:
        # -- GeoIP is authoritative, the import row is the fallback --------
        geodb.lookup = lambda h: ("DE", "Germany") if h == "9.9.9.9" else ("", "")
        check("geoip wins over the import", resolve_country("9.9.9.9", "FR")
              == ("DE", "Germany"))
        check("import hint when geoip is blind", resolve_country("1.2.3.4", "fr")
              == ("FR", ""))
        check("nothing known", resolve_country("1.2.3.4") == ("", ""))

        # -- the pool gets tagged, and selection scopes to one country -----
        GEO = {"9.9.9.9": ("DE", "Germany"),
               "8.8.8.8": ("US", "United States"),
               "1.1.1.1": ("AU", "Australia"),
               "10.0.0.1": ("", "")}
        geodb.lookup = lambda h: GEO.get(h, ("", ""))
        eng = RotatingProxy(logger=quiet,
                            proxies=["9.9.9.9:8080", "8.8.8.8:8080",
                                     "1.1.1.1:8080", "10.0.0.1:8080"])
        snap = eng.snapshot()
        tally = {k: v["total"] for k, v in snap["pool_countries"].items()}
        check("pool tagged with countries", tally == {"DE": 1, "US": 1, "AU": 1},
              str(tally))
        check("scope starts empty", snap["country"] == "")
        for n in eng._nodes:
            n.status = "alive"
        check("unscoped candidates", len(eng._candidates()) == 4,
              str([n.label for n in eng._candidates()]))

        check("set_country by name", eng.set_country("germany") == "DE")
        check("scope stored", eng.settings["country"] == "DE"
              and eng.snapshot()["country"] == "DE")
        check("candidates scoped to Germany",
              [n.label for n in eng._candidates()] == ["9.9.9.9:8080"],
              str([n.label for n in eng._candidates()]))
        check("set_country is idempotent", eng.set_country("DE") == "DE")

        # an empty scope fails honestly instead of leaking to another exit
        eng.set_country("")
        eng.set_proxies(["8.8.8.8:8080"])
        for n in eng._nodes:
            n.status = "alive"
        check("scope can be re-adopted", eng.set_country("US") == "US")
        eng.set_proxies(["9.9.9.9:8080"])
        for n in eng._nodes:
            n.status = "alive"
        check("empty scope yields no candidate", eng._candidates() == [],
              str([n.label for n in eng._candidates()]))
        try:
            eng.set_country("Atlantis")
            check("unknown country rejected", False, "no error raised")
        except ValueError:
            check("unknown country rejected", True)

        # -- hints survive when there is no GeoIP database at all ----------
        geodb.lookup = lambda h: ("", "")
        eng2 = RotatingProxy(logger=quiet, proxies=[])
        eng2.add_proxies("14.136.67.106\t1080\tHK\tHong Kong\tSocks4\tX\tY\t"
                         "1 min ago")
        got = eng2.nodes()
        check("import hint used without geoip",
              len(got) == 1 and got[0]["cc"] == "HK", str(got))
        check("hint makes the country selectable", eng2.set_country("hk") == "HK")

        # -- the scope can be changed while the proxy is serving -----------
        geodb.lookup = lambda h: GEO.get(h, ("", ""))
        eng3 = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080"])
        eng3.configure(port=_free_port())
        eng3.start(probe_first=False)
        try:
            check("country change allowed while running",
                  eng3.set_country("DE") == "DE", eng3.settings["country"])
        finally:
            eng3.stop()
    finally:
        geodb.lookup = real


def test_region_state():
    print("\nexit region / state selection")
    import geodb
    from engine import RotatingProxy, normalize_region

    # -- region names normalise, junk is rejected -------------------------
    check("region by name", normalize_region("europe") == "Europe")
    check("region keeps its spelling", normalize_region("NORTH AMERICA")
          == "North America")
    check("region empty means anywhere",
          normalize_region("") == "" and normalize_region("any") == ""
          and normalize_region("Anywhere") == "")
    try:
        normalize_region("Middle Earth")
        check("unknown region rejected", False, "no error raised")
    except ValueError:
        check("unknown region rejected", True)

    real_lookup, real_locate = geodb.lookup, geodb.locate
    GEO = {"9.9.9.9": ("DE", "Germany"), "8.8.8.8": ("US", "United States"),
           "1.1.1.1": ("AU", "Australia"), "10.0.0.1": ("US", "United States")}
    STATE = {"9.9.9.9": ("Berlin", "", "Berlin"),
             "8.8.8.8": ("California", "", "Mountain View"),
             "1.1.1.1": ("New South Wales", "", "Sydney"),
             "10.0.0.1": ("Texas", "", "Dallas")}
    geodb.lookup = lambda h: GEO.get(h, ("", ""))
    geodb.locate = lambda h: STATE.get(h, ("", "", ""))
    try:
        eng = RotatingProxy(logger=quiet,
                            proxies=["9.9.9.9:8080", "8.8.8.8:8080",
                                     "1.1.1.1:8080", "10.0.0.1:8080"])
        got = {n["label"]: n["state"] for n in eng.nodes()}
        check("pool tagged with states",
              got == {"9.9.9.9:8080": "Berlin", "8.8.8.8:8080": "California",
                      "1.1.1.1:8080": "New South Wales",
                      "10.0.0.1:8080": "Texas"}, str(got))
        for n in eng._nodes:
            n.status = "alive"
        check("nothing scoped at the start",
              eng.region == "" and eng.state == "" and eng.country == "")
        check("free pool offers everyone", len(eng._candidates()) == 4,
              str([n.label for n in eng._candidates()]))

        # -- region ------------------------------------------------------
        eng.set_region("europe")
        check("set_region by name", eng.region == "Europe", eng.region)
        check("candidates inside the region",
              {n.label for n in eng._candidates()} == {"9.9.9.9:8080"},
              str([n.label for n in eng._candidates()]))
        check("scope note names the region",
              eng.snapshot()["scope"] == "exit Europe",
              str(eng.snapshot().get("scope")))

        # a country outside the current region is not offered
        eng.set_country("DE")
        check("country inside the region accepted", eng.country == "DE")
        try:
            eng.set_country("US")
            check("country outside the region rejected", False,
                  "no error raised")
        except ValueError:
            check("country outside the region rejected", True)

        # -- state -------------------------------------------------------
        eng.set_state("berlin")
        check("set_state by name", eng.state == "Berlin", eng.state)
        check("candidates inside the state",
              {n.label for n in eng._candidates()} == {"9.9.9.9:8080"},
              str([n.label for n in eng._candidates()]))
        check("scope note names the state",
              eng.snapshot()["scope"] == "exit Berlin, Germany (DE)",
              str(eng.snapshot().get("scope")))
        try:
            eng.set_state("Texas")
            check("state outside the country rejected", False,
                  "no error raised")
        except ValueError:
            check("state outside the country rejected", True)
        check("set_state is idempotent", eng.set_state("Berlin") == "Berlin")

        # -- switching region drops what no longer fits -------------------
        eng.set_region("North America")
        check("region switch clears country and state",
              eng.region == "North America" and eng.country == ""
              and eng.state == "", f"{eng.region} {eng.country} {eng.state}")
        check("candidates follow the new region",
              {n.label for n in eng._candidates()}
              == {"8.8.8.8:8080", "10.0.0.1:8080"},
              str([n.label for n in eng._candidates()]))
        eng.set_country("US")
        eng.set_state("Texas")
        check("state narrows the region",
              {n.label for n in eng._candidates()} == {"10.0.0.1:8080"},
              str([n.label for n in eng._candidates()]))
        eng.set_state("")
        eng.set_country("")
        eng.set_region("")

        # -- an empty geographic scope still fails honestly ---------------
        logs: list = []
        scoped = RotatingProxy(logger=lambda l, m: logs.append((l, m)),
                               proxies=["1.1.1.1:8080"])
        for n in scoped._nodes:
            n.status = "alive"
        scoped.set_region("Europe")
        check("empty region yields no candidate", scoped._candidates() == [],
              str(scoped._candidates()))
        check("empty region warned",
              any("no upstream in Europe" in m and "502" in m
                  for lvl, m in logs if lvl == "warn"), str(logs[-2:]))
        check("empty region note",
              scoped._no_upstream_note() == " [restricted to Europe]",
              scoped._no_upstream_note())
        eng.set_region("")
        eng.set_proxies(["9.9.9.9:8080", "8.8.8.8:8080", "1.1.1.1:8080",
                         "10.0.0.1:8080"])
        for n in eng._nodes:
            n.status = "alive"

        # -- clearing works from every level ------------------------------
        eng.set_region("Oceania")
        check("clear the region", eng.set_region("") == ""
              and eng.region == "")
        eng2 = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080"])
        eng2.set_state("Berlin")
        check("clear the state", eng2.set_state("") == "" and eng2.state == "")

        # -- a hand-edited state file must not be able to strand us --------
        eng3 = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080"],
                             region="Asia", country="DE")
        check("contradictory scope keeps the country",
              eng3.region == "" and eng3.country == "DE",
              f"{eng3.region} {eng3.country}")
        eng4 = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080"],
                             region="Middle Earth")
        check("junk region dropped at load", eng4.region == "",
              eng4.region)
        # configure() validates the same way
        eng6 = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080"])
        try:
            eng6.configure(region="Narnia")
            check("configure rejects a junk region", False, "no error raised")
        except ValueError:
            check("configure rejects a junk region", True)
        check("configure accepts a real region",
              eng6.configure(region="europe")["region"] == "Europe",
              eng6.settings.get("region"))
        check("configure trims the state",
              eng6.configure(state="  California ")["state"] == "California",
              eng6.settings.get("state"))

        # -- a narrower level may never contradict a wider one ------------
        eng.set_region("")
        eng.set_country("")
        eng.set_state("")
        eng.set_state("Texas")
        check("a state can stand on its own", eng.state == "Texas",
              eng.state)
        check("states offered follow the scope",
              eng.snapshot()["pool_states"]
              == {"Berlin": 1, "California": 1, "New South Wales": 1,
                  "Texas": 1}, str(eng.snapshot().get("pool_states")))
        eng.set_country("DE")
        check("changing country clears a state it does not have",
              eng.country == "DE" and eng.state == "",
              f"{eng.country} {eng.state}")
        check("the state offer narrows with the country",
              eng.snapshot()["pool_states"] == {"Berlin": 1},
              str(eng.snapshot().get("pool_states")))
        eng.set_state("Berlin")
        eng.set_region("Europe")                  # Germany is inside it
        check("region keeps a state it contains",
              eng.region == "Europe" and eng.state == "Berlin"
              and eng.country == "DE",
              f"{eng.region} {eng.country} {eng.state}")
        try:
            eng.set_state("Texas")
            check("state outside the region refused", False,
                  "no error raised")
        except ValueError:
            check("state outside the region refused", True)
        eng.set_state("")
        eng.set_country("")
        eng.set_region("")
    finally:
        geodb.lookup = real_lookup
        geodb.locate = real_locate


def test_https_only():
    print("\nHTTPS-capable routing")
    import geodb
    real = geodb.lookup
    try:
        GEO = {"9.9.9.9": ("DE", "Germany"), "8.8.8.8": ("US", "United States"),
               "1.1.1.1": ("AU", "Australia")}
        geodb.lookup = lambda h: GEO.get(h, ("", ""))
        logs: list = []
        eng = RotatingProxy(logger=lambda lvl, msg: logs.append((lvl, msg)),
                            proxies=["9.9.9.9:8080", "8.8.8.8:8080",
                                     "1.1.1.1:8080", "10.0.0.7:8080"])
        snap = eng.snapshot()
        check("off by default",
              eng.https_only is False and snap["https_only"] is False,
              str(snap.get("https_only")))
        check("no scope note while free", snap.get("scope") == "",
              str(snap.get("scope")))

        for n in eng._nodes:
            n.status = "alive"
        by_host = {n.host: n for n in eng._nodes}
        by_host["9.9.9.9"].connect_ok = True      # known tunnelers
        by_host["8.8.8.8"].connect_ok = True
        by_host["1.1.1.1"].connect_ok = False      # proved it cannot tunnel
        # 10.0.0.7 was never probed -> connect_ok stays None

        check("unscoped picks everyone", len(eng._candidates()) == 4,
              str([n.label for n in eng._candidates()]))

        check("turn it on", eng.set_https_only(True) is True)
        check("setting recorded",
              eng.settings["https_only"] is True
              and eng.snapshot()["https_only"] is True)
        check("turning on is logged",
              any("HTTPS-capable" in m for _, m in logs), str(logs[-3:]))
        pick = {n.label for n in eng._candidates()}
        check("known non-tunneller excluded", "1.1.1.1:8080" not in pick,
              str(pick))
        check("never-probed stays in play", "10.0.0.7:8080" in pick, str(pick))
        check("known tunnelers kept",
              {"9.9.9.9:8080", "8.8.8.8:8080"} <= pick, str(pick))

        before = len(logs)
        eng.set_https_only(True)
        check("set_https_only is idempotent", len(logs) == before,
              str(logs[before:]))
        check("string accepted", eng.set_https_only("off") is False
              and eng.set_https_only("yes") is True)
        try:
            eng.set_https_only("maybe")
            check("bad boolean rejected", False, "no error raised")
        except ValueError:
            check("bad boolean rejected", True)

        # -- together with a country scope ---------------------------------
        eng.set_https_only(True)
        eng.set_country("DE")
        for n in eng._nodes:
            n.status = "alive"
        check("country + https together",
              [n.label for n in eng._candidates()] == ["9.9.9.9:8080"],
              str([n.label for n in eng._candidates()]))
        check("scope note names both",
              eng.snapshot()["scope"] == "exit Germany (DE) · HTTPS-capable only",
              str(eng.snapshot().get("scope")))

        # an empty scope fails honestly instead of routing somewhere else
        eng.set_country("AU")
        for n in eng._nodes:
            n.status = "alive"
        check("empty scope yields no candidate", eng._candidates() == [],
              str([n.label for n in eng._candidates()]))
        check("scope warning emitted",
              any("tunnel HTTPS" in m for lvl, m in logs if lvl == "warn"),
              str(logs[-4:]))
        check("502 note mentions the https scope",
              eng._no_upstream_note()
              == " [restricted to Australia (AU) and HTTPS-capable upstreams]",
              eng._no_upstream_note())

        eng.set_country("")
        check("https note alone", eng._no_upstream_note()
              == " [restricted to HTTPS-capable upstreams]",
              eng._no_upstream_note())

        # -- while running, and via configure() when stopped ---------------
        check("configure sets it while stopped",
              eng.configure(https_only=False)["https_only"] is False)
        eng_logs: list = []
        eng2 = RotatingProxy(logger=lambda lvl, msg: eng_logs.append((lvl, msg)),
                             proxies=["9.9.9.9:8080"])
        eng2.set_https_only(True)
        eng2.configure(port=_free_port())
        eng2.start(probe_first=False)
        try:
            check("allowed while running",
                  eng2.set_https_only(True) is True
                  and eng2.snapshot()["https_only"] is True)
            check("start announces the scope",
                  any("selection scope" in m and "HTTPS-capable" in m
                      for _, m in eng_logs), str(eng_logs[:4]))
        finally:
            eng2.stop()
        check("stopped again", not eng2.running)
    finally:
        geodb.lookup = real


def test_mmdb_reader():
    """The bundled state/city reader: stdlib only, offline, and faithful.

    State scope ships `mmdb.py` instead of `python3-maxminddb` so the
    feature exists on any machine.  When the reference binding happens to
    be installed as well, both readers are compared record for record on
    the real database; without a database the file-error paths are still
    exercised so the suite stays hermetic.
    """
    print("\nbundled MMDB reader")
    import tempfile
    from pathlib import Path

    import mmdb

    with tempfile.TemporaryDirectory(prefix="mmdb-") as tmp:
        junk = Path(tmp) / "junk.mmdb"
        junk.write_bytes(b"this is not a database")
        for name, path in (("missing database rejected",
                            str(Path(tmp) / "nowhere.mmdb")),
                           ("garbage database rejected", str(junk))):
            try:
                mmdb.open_database(path)
                check(name, False, "no error raised")
            except mmdb.InvalidDatabase:
                check(name, True)

    import geodb
    db_path = Path(geodb.city_path())
    if not db_path.exists():
        check("city database read", True, "skipped: none installed")
        return

    with mmdb.open_database(str(db_path)) as db:
        check("metadata read",
              "city" in str(db.metadata.get("database_type", "")).lower()
              and db.metadata.get("node_count", 0) > 0,
              str({k: db.metadata.get(k)
                   for k in ("database_type", "ip_version", "node_count")}))
        record = db.get("8.8.8.8")
        check("record decoded",
              isinstance(record, dict)
              and "subdivisions" in record
              and record["subdivisions"][0]["names"]["en"],
              str(record)[:160])
        check("unrouted address reads as no data",
              db.get("10.0.0.1") is None, str(db.get("10.0.0.1")))
        try:
            db.get("not-an-ip")
            check("non-IP argument rejected", False, "no error raised")
        except ValueError:
            check("non-IP argument rejected", True)

        # geodb's state scope sits on top of this reader
        state, _code, city = geodb.locate("8.8.8.8")
        check("state scope resolves through it",
              state == "California" and bool(city),
              f"{state=} {city=}")

        # -- cross-check against the reference binding when it exists ----
        try:
            import maxminddb
        except ImportError:
            check("matches the reference reader", True,
                  "skipped: maxminddb not installed")
            return
        probes = ["8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222",
                  "193.0.6.139", "2a00:1450:4001:80f::200e", "10.0.0.1"]
        with maxminddb.open_database(str(db_path)) as ref:
            for ip in probes:
                ours, theirs = db.get(ip), ref.get(ip)
                check(f"reference agrees for {ip}", ours == theirs,
                      f"ours={str(ours)[:120]} theirs={str(theirs)[:120]}")


def test_lifecycle():
    print("\nstart / stop lifecycle")
    eng = RotatingProxy(logger=quiet, proxies=["1.1.1.1:80"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    check("running", eng.snapshot()["status"] == "running")
    try:
        eng.configure(port=1)
        check("settings locked while running", False)
    except RuntimeError:
        check("settings locked while running", True)
    # port is genuinely bound
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", free))
        check("port accepting connections", True)
    except OSError as e:
        check("port accepting connections", False, str(e))
    finally:
        s.close()
    eng.stop()
    check("stopped", eng.snapshot()["status"] == "stopped")
    check("active cleared", eng.snapshot()["active"] == 0)
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", free))
        check("port released", False, "still accepting after stop")
    except OSError:
        check("port released", True)
    finally:
        s.close()


def test_rotate_on_block(target_port, mock_port):
    print("\nrotate the exit when the target blocks it")
    import engine as engine_mod
    block_port = start_mock(reject_status="403")
    logs = []
    original = engine_mod.random.sample
    # force configured order so the blocked exit is always tried first
    engine_mod.random.sample = lambda seq, k: list(seq)[:k]
    try:
        eng = RotatingProxy(logger=lambda l, m: logs.append((l, m)),
                            proxies=[f"127.0.0.1:{block_port}",
                                     f"127.0.0.1:{mock_port}"],
                            max_retries=5)
        free = _free_port()
        eng.configure(port=free)
        eng.start(probe_first=False)
        try:
            raw = http_get(free, f"127.0.0.1:{target_port}", "/rot")
            check("recovered from a different exit",
                  b"TARGET-GET /rot" in raw, raw[:90])
            check("rotation logged",
                  any("rotating to another exit" in m for _, m in logs),
                  str(logs[-3:]))
            nodes = {n["label"]: n for n in eng.nodes()}
            blocked = f"127.0.0.1:{block_port}"
            check("block counted on the node",
                  nodes[blocked]["blocks"] == 1, str(nodes[blocked]))
            check("blocked exit not evicted",
                  blocked not in eng.proxies("dead"),
                  str(eng.proxies("dead")))
            snap = eng.snapshot()
            check("counted as served",
                  snap["served"] == 1 and snap["failed"] == 0,
                  f"{snap['served']} / {snap['failed']}")
        finally:
            eng.stop()

        # a single upstream means nowhere to rotate: the 403 is the
        # target's answer and must reach the client untouched
        eng2 = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{block_port}"])
        free2 = _free_port()
        eng2.configure(port=free2)
        eng2.start(probe_first=False)
        try:
            raw = http_get(free2, f"127.0.0.1:{target_port}", "/only")
            status = raw.split(b"\r\n", 1)[0]
            check("last candidate passes the block through",
                  b" 403 " in status, status.decode(errors="replace")[:60])
        finally:
            eng2.stop()

        # a request with a body cannot be replayed through another exit
        eng3 = RotatingProxy(logger=quiet,
                             proxies=[f"127.0.0.1:{block_port}",
                                      f"127.0.0.1:{mock_port}"])
        free3 = _free_port()
        eng3.configure(port=free3)
        eng3.start(probe_first=False)
        try:
            raw = http_post(free3, f"127.0.0.1:{target_port}", "/post", b"data")
            status = raw.split(b"\r\n", 1)[0]
            check("POST is not replayed",
                  b" 403 " in status, status.decode(errors="replace")[:60])
        finally:
            eng3.stop()
    finally:
        engine_mod.random.sample = original


def test_strength():
    print("\nstrength tiers / regions / candidate preference")
    from engine import Node, _tier, _rotate_codes, _validate_rotate_on, region_of

    check("fresh node is New", _tier(None, 0) == "New")
    check("one clean probe -> Good", _tier(1.0, 1) == "Good")
    check("two clean probes -> Strong", _tier(1.0, 2) == "Strong")
    check("steady failures -> Weak", _tier(0.0, 5) == "Weak")
    check("0.7 stays Good", _tier(0.7, 5) == "Good")
    check("0.5 falls to Weak", _tier(0.5, 5) == "Weak")
    n = Node("1.2.3.4", 8080)
    check("node strength before any data", n.strength == "New"
          and n.as_dict()["samples"] == 0 and n.as_dict()["score"] is None)
    n.sample(1.0)
    n.sample(1.0)
    check("node scores to Strong", n.strength == "Strong"
          and n.as_dict()["score"] == 1.0)
    n.sample(0.0)
    check("EMA reacts to a failure", n.as_dict()["score"] < 1.0)

    check("region Europe", region_of("DE") == "Europe")
    check("region Asia", region_of("HK") == "Asia")
    check("region Africa", region_of("ZW") == "Africa")
    check("region lower-case ok", region_of("us") == "North America")
    check("region unknown", region_of("") == "" and region_of("ZZ") == "")
    check("region travels with the node",
          Node("1.2.3.4", 80, cc="FR").as_dict()["region"] == "Europe")

    eng = RotatingProxy(logger=quiet, proxies=["9.9.9.9:8080", "8.8.8.8:8080"])
    eng._nodes[0].status = "alive"
    eng._nodes[0].sample(1.0)
    eng._nodes[0].sample(1.0)
    eng._nodes[0].blocks = 3
    eng.set_proxies(["9.9.9.9:8080"])          # edit the list, keep the health
    again = eng.nodes()[0]
    check("score survives a list edit",
          again["samples"] == 2 and again["strength"] == "Strong"
          and again["blocks"] == 3, str(again))

    # the strong exit must be offered before the weak one, always
    eng.set_proxies(["9.9.9.9:8080", "8.8.8.8:8080"])
    for node in eng._nodes:
        node.status = "alive"
    strong, weak = eng._nodes[0], eng._nodes[1]
    strong.sample(1.0)
    strong.sample(1.0)
    weak.sample(0.0)
    weak.sample(0.0)
    weak.sample(0.0)
    check("tiers differ", strong.strength == "Strong"
          and weak.strength == "Weak",
          f"{strong.strength} / {weak.strength}")
    firsts = [eng._candidates()[0].label for _ in range(6)]
    check("strong exit offered first",
          all(label == "9.9.9.9:8080" for label in firsts), str(firsts))
    check("weak stays in rotation", len(eng._candidates()) == 2)
    check("snapshot counts the strong",
          eng.snapshot()["pool_strong"] == 1, str(eng.snapshot()["pool_strong"]))

    check("codes parsed", _rotate_codes("403, 429;999") == {403, 429, 999})
    check("junk skipped", _rotate_codes("banana,,403") == {403})
    check("empty disables", _rotate_codes("") == set())
    check("validated + normalised", _validate_rotate_on("429; 503") == "429,503")
    try:
        _validate_rotate_on("blocked")
        check("bad list rejected", False, "no error raised")
    except ValueError:
        check("bad list rejected", True)
    check("default on", RotatingProxy(logger=quiet).settings["rotate_on"]
          == "403,429,999")
    check("hand-edited garbage falls back",
          RotatingProxy(logger=quiet, rotate_on="nonsense").settings["rotate_on"]
          == "403,429,999")
    eng2 = RotatingProxy(logger=quiet)
    try:
        eng2.configure(rotate_on="banana")
        check("configure rejects junk", False, "no error raised")
    except ValueError:
        check("configure rejects junk", True)
    check("configure normalises",
          eng2.configure(rotate_on="403, 429")["rotate_on"] == "403,429")
    check("rotation can be switched off",
          eng2.configure(rotate_on="")["rotate_on"] == "")


def test_socks4a_remote_dns(target_port, socks4_port):
    print("\nSOCKS4a remote DNS")
    import engine as engine_mod
    eng, free = _socks_engine(socks4_port, "socks4")
    real = engine_mod._resolve_ipv4

    def boom(_host):
        raise AssertionError("local DNS was used for a hostname")

    engine_mod._resolve_ipv4 = boom
    try:
        raw = http_get(free, f"localhost:{target_port}", "/4a")
        check("hostname dialed through the exit",
              b"TARGET-GET /4a" in raw, raw[:90])
        check("upstream still marked alive",
              eng.proxies("alive") == [f"socks4://127.0.0.1:{socks4_port}"],
              str(eng.proxies()))
    finally:
        engine_mod._resolve_ipv4 = real
        eng.stop()


def test_header_hygiene():
    print("\nhop-by-hop header hygiene")
    build = RotatingProxy._build_upstream_head
    head = build("GET", "http://example.com/x", "example.com", 80,
                 [("Host", "example.com"), ("Connection", "close, X-Secret"),
                  ("X-Secret", "hunter2"), ("X-Keep", "yes"),
                  ("Proxy-Connection", "keep-alive"),
                  ("Proxy-Authorization", "Basic dXNlcg=="),
                  ("Expect", "100-continue"), ("User-Agent", "UA/1.0"),
                  ("Cookie", "sid=1")])
    check("Connection token stripped", b"X-Secret" not in head)
    check("end-to-end header kept", b"X-Keep: yes" in head)
    check("User-Agent forwarded verbatim", b"User-Agent: UA/1.0" in head)
    check("Cookie forwarded verbatim", b"Cookie: sid=1" in head)
    check("Proxy-Connection dropped", b"Proxy-Connection" not in head)
    check("Proxy-Authorization dropped", b"Proxy-Authorization" not in head)
    check("Expect dropped", b"Expect" not in head)
    check("Connection rewritten to close", b"Connection: close" in head)

    head2 = build("GET", "/p", "example.com", 8080,
                  [("Host", "example.com"), ("Accept", "*/*")])
    check("origin-form target for SOCKS", head2.startswith(b"GET /p HTTP/1.1"))
    check("Host rebuilt with the port", b"Host: example.com:8080" in head2)
    check("Connection appended when absent",
          head2.rstrip().endswith(b"Connection: close"))


def test_expect_continue(target_port, mock_port):
    print("\nExpect: 100-continue")
    eng = RotatingProxy(logger=quiet, proxies=[f"127.0.0.1:{mock_port}"])
    free = _free_port()
    eng.configure(port=free)
    eng.start(probe_first=False)
    try:
        payload = b"e" * 100
        s = socket.create_connection(("127.0.0.1", free), timeout=8)
        try:
            s.settimeout(4)
            s.sendall(
                f"POST http://127.0.0.1:{target_port}/exp HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Content-Type: text/plain\r\n"
                f"Expect: 100-continue\r\nConnection: close\r\n\r\n".encode())
            early = b""
            try:
                early = s.recv(256)
            except socket.timeout:
                pass
            check("100 Continue answered promptly",
                  b"100 Continue" in early, early[:70])
            s.sendall(payload)
            rest = _drain(s)
            check("body accepted after the 100",
                  b"TARGET-POST len=100" in rest, rest[:90])
        finally:
            s.close()
    finally:
        eng.stop()


def test_list_refresh():
    """Auto-reload of plain-text proxy lists (the "keep the pool stocked"
    feature): fetch, parse, merge, tolerate dead sources, validate input."""
    print("\nauto-refresh from proxy list URLs")
    import http.server

    http_list = (
        "# comment line\n"
        "192.0.2.10:8080\n"
        "192.0.2.11:3128\n"
        "192.0.2.10:8080\n"                      # duplicate
        "garbage without a port\n"               # junk, skipped silently
        "198.51.100.7:8080,203.0.113.9:9999\n"   # comma blob
    )
    socks_list = "203.0.113.50:1080\n203.0.113.51:1080\n"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = socks_list if "socks5" in self.path else http_list
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a):               # keep the test output clean
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    logs: list = []
    eng = RotatingProxy(logger=lambda l, m: logs.append((l, m)),
                        proxies=["192.0.2.99:8080"])
    try:
        eng.configure(refresh_url=f"{base}/http.txt, {base}/socks5.txt",
                      refresh_interval=3600)
        added, total = eng.refresh_list(reason="test")
        check("fresh upstreams added", added == 6, f"added={added}")
        labels = eng.proxies()
        check("pool total after merge", total == 7 and len(labels) == 7,
              f"total={total} labels={len(labels)}")
        check("bare lines become HTTP",
              "192.0.2.10:8080" in labels and "192.0.2.11:3128" in labels,
              str(labels))
        check("comma blob split into two",
              "198.51.100.7:8080" in labels and "203.0.113.9:9999" in labels,
              str(labels))
        check("socks list inherits its scheme",
              "socks5://203.0.113.50:1080" in labels
              and "socks5://203.0.113.51:1080" in labels, str(labels))
        check("comments, junk and dupes skipped",
              len(labels) == 7 and all("garbage" not in l and "#" not in l
                                       for l in labels), str(labels))
        check("existing upstream kept", "192.0.2.99:8080" in labels)
        check("refresh logged",
              any("proxy list refreshed" in m for _, m in logs),
              str(logs[-2:]))
        check("snapshot records the refresh",
              eng.snapshot()["last_refresh_at"] > 0)
        added2, _ = eng.refresh_list()
        check("second refresh is deduplicated", added2 == 0, f"{added2}")
        check("no sweep while stopped", not eng.checking)

        # long-dead entries are pruned by the next refresh pass
        for label in ("192.0.2.10:8080", "192.0.2.11:3128"):
            node = next(n for n in eng._nodes if n.label == label)
            node.status, node.failures = "dead", 12
        added_stale, total_stale = eng.refresh_list()
        check("stale dead entries pruned",
              added_stale == 0 and total_stale == 5,
              f"{added_stale}/{total_stale}")
        check("prune logged",
              any("stale dropped" in m for _, m in logs[-2:]), str(logs[-1:]))

        # an unreachable source is logged and never raised
        eng.configure(refresh_url="http://127.0.0.1:1/list.txt")
        added3, total3 = eng.refresh_list()
        check("dead source tolerated",
              added3 == 0 and total3 == 5, f"{added3}/{total3}")
        check("dead source logged",
              any("unreachable" in m for _, m in logs[-3:]), str(logs[-2:]))

        # validation surfaces as ValueError, not a broken setting
        rejected = False
        try:
            eng.configure(refresh_url="ftp://host/list.txt")
        except ValueError:
            rejected = True
        check("non-http URL rejected", rejected)
        rejected = False
        try:
            eng.configure(refresh_interval=30)
        except ValueError:
            rejected = True
        check("interval under 60s rejected", rejected)

        # the off switch, and the loop's guards
        eng.configure(refresh_url="")
        check("empty URL disables the feature",
              eng.refresh_list() == (0, 5) and eng.refresh_url == [],
              str(eng.refresh_list()))
        eng.start_refresh_loop()
        check("loop refuses to start without a URL",
              not any(t.name == "refresh-loop" for t in eng._threads))
        eng.configure(refresh_url=f"{base}/http.txt")
        eng.start_refresh_loop()
        check("loop starts with a URL",
              any(t.name == "refresh-loop" for t in eng._threads))
        eng._stop.set()                          # releases the parked loop
    finally:
        eng._stop.set()
        httpd.shutdown()


def test_use_scope():
    """The routing scope (`use_only`): which proxies may be used at all."""
    print("\nrouting scope (use flags)")
    logs: list = []
    eng = RotatingProxy(logger=lambda l, m: logs.append((l, m)),
                        proxies=["192.0.2.1:8080", "socks4://192.0.2.2:1080",
                                 "socks5://192.0.2.3:1080",
                                 "socks5://192.0.2.4:1080"])
    nodes = eng._nodes
    latencies = (120.0, 800.0, 150.0, None)
    for i, n in enumerate(nodes):
        n.status = "alive"
        n.latency = latencies[i]
    nodes[2].score, nodes[2].samples = 0.95, 3   # the one Strong entry
    try:
        check("scope starts empty",
              eng.use_flags == frozenset()
              and eng.settings["use_only"] == "")
        check("unrestricted pool offered",
              len(eng._candidates()) == 4,
              str([n.label for n in eng._candidates()]))

        eng.set_use("socks5")
        pick = {n.label for n in eng._candidates()}
        check("socks5 scope",
              pick == {"socks5://192.0.2.3:1080", "socks5://192.0.2.4:1080"},
              str(sorted(pick)))
        check("scope change logged",
              any("only socks5" in m for _, m in logs), str(logs[-1:]))

        eng.set_use("http,socks4")
        pick = {n.label for n in eng._candidates()}
        check("http+socks4 scope",
              pick == {"192.0.2.1:8080", "socks4://192.0.2.2:1080"},
              str(sorted(pick)))

        eng.set_use("strong")
        pick = {n.label for n in eng._candidates()}
        check("strong scope", pick == {"socks5://192.0.2.3:1080"},
              str(sorted(pick)))

        eng.set_use("fast")
        pick = {n.label for n in eng._candidates()}
        check("fast scope drops slow and unmeasured",
              pick == {"192.0.2.1:8080", "socks5://192.0.2.3:1080"},
              str(sorted(pick)))

        check("flags normalised",
              eng.set_use(" SOCKS5 , socks5 ,strong ") == "socks5,strong")

        # `strong` is a *preference*: when nothing in scope has proven
        # itself yet the request still goes out, through the best exit
        # there is, instead of refusing every connection outright
        eng.set_use("http,strong")
        check("strong falls back instead of refusing",
              {n.label for n in eng._candidates()} == {"192.0.2.1:8080"},
              str([n.label for n in eng._candidates()]))
        check("fallback announced in the log",
              any("falling back" in m for _, m in logs[-4:]),
              str(logs[-3:]))
        check("snapshot reports the fallback",
              "strong" in eng.snapshot().get("scope_fallback", []),
              str(eng.snapshot().get("scope_fallback")))
        # …and `fast` behaves the same way
        for n in eng._nodes:
            n.latency = 900.0                     # nothing is fast any more
        eng.set_use("fast")
        check("fast falls back too",
              len(eng._candidates()) > 0, str(eng._candidates()))
        check("fast fallback reported",
              "fast" in eng.snapshot().get("scope_fallback", []),
              str(eng.snapshot().get("scope_fallback")))
        for n, latency in zip(eng._nodes, latencies):
            n.latency = latency
        eng.set_use("")

        rejected = False
        try:
            eng.configure(use_only="tor")
        except ValueError:
            rejected = True
        check("unknown flag rejected", rejected)
        hint = False
        try:
            eng.configure(use_only="https")
        except ValueError as exc:
            hint = "https-only" in str(exc)
        check("https rejected with a hint", hint)

        eng.set_use("")
        check("clearing restores the whole pool",
              eng.use_flags == frozenset() and len(eng._candidates()) == 4)

        # https stays its own switch, independent of the routing flags
        eng.set_https_only(True)
        eng.set_use("socks5")
        check("https-only is independent",
              eng.https_only is True
              and eng.use_flags == frozenset({"socks5"})
              and "https" not in eng.use_flags, str(eng.use_flags))
        snap = eng.snapshot()
        check("snapshot carries the scope",
              snap["use_only"] == "socks5" and "use socks5" in snap["scope"],
              f"{snap['use_only']!r} {snap['scope']!r}")

        # the protocol chips are promises, not preferences: with no SOCKS
        # upstream at all the request fails honestly rather than quietly
        # leaving through HTTP
        eng.set_proxies(["192.0.2.1:8080"])
        for n in eng._nodes:
            n.status = "alive"
        eng.set_use("socks5")
        check("protocol scope stays hard",
              eng._candidates() == [], str(eng._candidates()))
        check("hard empty scope warned",
              any("routing filter" in m and "502" in m for _, m in logs[-6:]),
              str(logs[-6:]))
    finally:
        eng.configure(use_only="", https_only=False)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    global _TARGET_PORT
    target = start_target()
    target_port = target.server_address[1]
    _TARGET_PORT = target_port        # example.com answers from here
    mock_port = start_mock(alive=True)
    reject_port = start_mock(reject_status="407")
    early_port = start_mock(early=b"EARLY-DATA")
    socks4_port = start_socks(4)
    socks5_port = start_socks(5)
    dead_port = _free_port()                 # nothing listening

    tests = [
        test_parse,
        test_entry_parsing,
        lambda: test_plain_get(target_port, mock_port),
        lambda: test_post_body(target_port, mock_port),
        lambda: test_connect(target_port, mock_port),
        lambda: test_connect_failover(target_port, dead_port, mock_port),
        test_upstream_rejection_detection,
        lambda: test_reject_failover(target_port, reject_port, mock_port),
        lambda: test_connect_early_data(target_port, early_port),
        lambda: test_no_upstream(target_port),
        lambda: test_health_check(mock_port, dead_port),
        lambda: test_socks4_relay(target_port, socks4_port),
        lambda: test_socks5_relay(target_port, socks5_port),
        lambda: test_socks_connect(target_port, socks4_port, socks5_port),
        lambda: test_socks_health(socks4_port, socks5_port, dead_port),
        test_stats_and_list_ops,
        test_country,
        test_region_state,
        test_mmdb_reader,
        test_https_only,
        test_use_scope,
        lambda: test_rotate_on_block(target_port, mock_port),
        test_strength,
        lambda: test_socks4a_remote_dns(target_port, socks4_port),
        test_header_hygiene,
        lambda: test_expect_continue(target_port, mock_port),
        test_list_refresh,
        test_lifecycle,
    ]

    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            RESULTS.append((getattr(t, "__name__", "test"), False, "crashed"))

    target.shutdown()
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
