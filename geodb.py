#!/usr/bin/env python3
"""
Offline IP -> country lookup for the upstream pool.

Uses the GeoLite Country database that Debian/Kali ships in the
`geoip-database` package together with the `python3-geoip` bindings:

    /usr/share/GeoIP/GeoIP.dat

Everything is local and synchronous -- no network calls, no rate limits --
and answers are cached, because re-importing the list resolves the whole
pool again.

If the database or the bindings are missing, `lookup()` simply returns
("","") for every address and `available()` says so, so the rest of the
program degrades to "no country data" instead of failing.

    import geodb
    geodb.available()              -> True
    geodb.lookup("14.136.67.106")  -> ("HK", "Hong Kong")
    geodb.lookup("example.com")    -> ("", "")   # not a literal address
"""

from __future__ import annotations

import os
import threading

# first existing file wins
DB_PATHS = (
    "/usr/share/GeoIP/GeoIP.dat",
    "/usr/share/GeoIP/GeoLiteCountry/GeoIP.dat",
    "/usr/local/share/GeoIP/GeoIP.dat",
    "/var/lib/GeoIP/GeoIP.dat",
)

_INSTALL_HINT = "install `geoip-database python3-geoip` for country data"

# GeoIP returns these for addresses it cannot place
_UNKNOWN = {"", "--", "n/a", "na", "none", "null"}

_RLock = threading.RLock()
_DB = None
_TRIED = False
_CACHE: dict[str, tuple[str, str]] = {}


def _looks_like_ip(host: str) -> bool:
    """True for a literal IPv4 or IPv6 address (never a hostname)."""
    if host.count(".") == 3:
        return all(p.isdigit() and len(p) <= 3 and int(p) < 256
                   for p in host.split("."))
    return host.count(":") >= 2            # IPv6


def _clean(value, *, upper: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.lower() in _UNKNOWN:
        return ""
    return value.upper() if upper else value


def _open():
    """Open the database once, lazily and thread-safely."""
    global _DB, _TRIED
    with _RLock:
        if _TRIED:
            return _DB
        _TRIED = True
        try:
            import GeoIP                                     # python3-geoip
        except Exception:
            return None
        for path in DB_PATHS:
            if not os.path.isfile(path):
                continue
            try:
                _DB = GeoIP.open(path, GeoIP.GEOIP_STANDARD)
            except Exception:
                continue
            break
        return _DB


def available() -> bool:
    """True when country data can actually be resolved."""
    return _open() is not None


def source() -> str:
    """A short human description of where country data comes from."""
    if _open() is None:
        return _INSTALL_HINT
    for path in DB_PATHS:
        if os.path.isfile(path):
            return path
    return "GeoIP"


def lookup(host: str) -> tuple[str, str]:
    """Return ``(code, name)`` for a literal address, or ``("","")``.

    Hostnames are never resolved -- the pool is literal IPs, and a DNS
    lookup here would block the caller for no benefit.
    """
    if not host:
        return ("", "")
    hit = _CACHE.get(host)
    if hit is not None:
        return hit
    if not _looks_like_ip(host):
        _CACHE[host] = ("", "")
        return ("", "")

    db = _open()
    code = name = ""
    if db is not None:
        ipv6 = ":" in host
        try:
            with _RLock:                    # the C object is shared state
                if ipv6:
                    code = _clean(db.country_code_by_addr_v6(host), upper=True)
                    name = _clean(db.country_name_by_addr_v6(host))
                else:
                    code = _clean(db.country_code_by_addr(host), upper=True)
                    name = _clean(db.country_name_by_addr(host))
        except Exception:
            code = name = ""
    result = (code, name)
    _CACHE[host] = result
    return result


def country_of(host: str) -> str:
    """Just the ISO code -- the key everything else filters and sorts on."""
    return lookup(host)[0]


def reset() -> None:
    """Forget the database handle and every cached answer."""
    global _DB, _TRIED
    with _RLock:
        _CACHE.clear()
        _DB = None
        _TRIED = False
