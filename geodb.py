#!/usr/bin/env python3
"""
Offline IP -> location lookup for the upstream pool.

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

State-level selection ("United States -> California") needs one more
layer: a city database, which no distribution ships by default.  It is
downloaded once -- from the panel's Settings dialog or
`python3 run.py --geo-fetch` -- into the user's config directory, and read
from there by the dependency-free `mmdb` reader afterwards, so the lookup
itself stays offline:

    geodb.city_available()            -> False        # nothing fetched yet
    geodb.fetch_city_db()             -> PosixPath(..)  (one ~60 MB download)
    geodb.state("8.8.8.8")            -> ("California", "US-CA" or "")
    geodb.city_of("8.8.8.8")          -> "Mountain View"

Who *runs* an address is the third layer, behind the exit risk flags: an
ASN database, fetched the same way as the city one (`python3 run.py
--asn-fetch` or the Settings dialog) and read offline afterwards:

    geodb.asn_available()            -> True
    geodb.fetch_asn_db()             -> PosixPath(..)  (one ~5 MB download)
    geodb.asn_info("8.8.8.8")        -> (15169, "Google LLC")
"""

from __future__ import annotations

import gzip
import os
import threading
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from appstate import data_dir

# first existing file wins
DB_PATHS = (
    "/usr/share/GeoIP/GeoIP.dat",
    "/usr/share/GeoIP/GeoLiteCountry/GeoIP.dat",
    "/usr/local/share/GeoIP/GeoIP.dat",
    "/var/lib/GeoIP/GeoIP.dat",
)

_INSTALL_HINT = "install `geoip-database python3-geoip` for country data"

# where the city (state-level) database lives and where else one might
# already be installed -- our own download comes first
CITY_FILENAME = "dbip-city-lite.mmdb"
# DB-IP publishes a fresh, freely licensed (CC BY 4.0) City database every
# month; the URL carries the month, so a stale one 404s and the next
# candidate below is tried
CITY_URL = "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
CITY_SIZE_CAP = 512 * 1024 * 1024          # generous: the file is ~120 MB
SYSTEM_CITY_PATHS = (
    "/usr/share/GeoIP/GeoLite2-City.mmdb",
    "/usr/local/share/GeoIP/GeoLite2-City.mmdb",
    "/var/lib/GeoIP/GeoLite2-City.mmdb",
    "/usr/share/GeoIP/dbip-city-lite.mmdb",
)
# the old (legacy) City/Region edition, for machines that still have it
LEGACY_CITY_PATHS = (
    "/usr/share/GeoIP/GeoIPCity.dat",
    "/usr/local/share/GeoIP/GeoIPCity.dat",
    "/var/lib/GeoIP/GeoIPCity.dat",
)
_INSTALL_CITY_HINT = "download it once with the panel's Settings dialog " \
                     "or `python3 run.py --geo-fetch`"

# where the ASN database lives: it says *what kind of network* an exit is
# on (a hosting provider, a VPN service, a phone company), which is the
# "is this address one services will trust?" half of exit verification
ASN_FILENAME = "dbip-asn-lite.mmdb"
ASN_URL = "https://download.db-ip.com/free/dbip-asn-lite-{ym}.mmdb.gz"
ASN_SIZE_CAP = 256 * 1024 * 1024
SYSTEM_ASN_PATHS = (
    "/usr/share/GeoIP/dbip-asn-lite.mmdb",
    "/usr/local/share/GeoIP/dbip-asn-lite.mmdb",
    "/usr/share/GeoIP/GeoLite2-ASN.mmdb",
    "/usr/local/share/GeoIP/GeoLite2-ASN.mmdb",
)
_INSTALL_ASN_HINT = "download it once with `python3 run.py --asn-fetch`"

# GeoIP returns these for addresses it cannot place
_UNKNOWN = {"", "--", "n/a", "na", "none", "null"}

_RLock = threading.RLock()
_DB = None
_TRIED = False
_CACHE: dict[str, tuple[str, str]] = {}
_CITY = None
_CITY_TRIED = False
_CITY_CACHE: dict[str, tuple[str, str, str]] = {}   # host -> (state, code, city)
_ASN = None
_ASN_TRIED = False
_ASN_CACHE: dict[str, tuple[int, str]] = {}         # host -> (asn, org)


def _looks_like_ip(host: str) -> bool:
    """True for a literal IPv4 or IPv6 address (never a hostname)."""
    if host.count(".") == 3:
        return all(p.isdigit() and len(p) <= 3 and int(p) < 256
                   for p in host.split("."))
    return host.count(":") >= 2            # IPv6


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


# ---------------------------------------------------------------------------
# city layer: state/province + city, from a MaxMind-format database
# ---------------------------------------------------------------------------
def geo_dir() -> Path:
    """Directory the downloaded city database lives in."""
    return data_dir() / "geo"


def city_path() -> Path:
    """Where `fetch_city_db()` puts the database."""
    return geo_dir() / CITY_FILENAME


def city_paths() -> tuple:
    """Every place a city database could already be (ours first)."""
    return (city_path(), geo_dir() / "GeoLite2-City.mmdb",
            *(Path(p) for p in SYSTEM_CITY_PATHS))


class _LegacyCity:
    """Adapter giving the old GeoIP City edition the `.mmdb` record shape."""

    def __init__(self, path: str) -> None:
        import GeoIP
        self._db = GeoIP.open(path, GeoIP.GEOIP_CITY_EDITION)
        self.path = path

    def get(self, host: str):
        try:
            region = ""
            if hasattr(self._db, "region_name_by_addr"):
                region = _clean(self._db.region_name_by_addr(host))
            record = self._db.record_by_addr(host) or {}
        except Exception:
            return None
        state = region or _clean(record.get("region"))
        city = _clean(record.get("city"))
        if not state and not city:
            return None
        out: dict = {}
        if state:
            out["subdivisions"] = [{"names": {"en": state},
                                    "iso_code": state.upper()}]
        if city:
            out["city"] = {"names": {"en": city}}
        return out

    def close(self) -> None:      # the binding owns its own handle
        pass


def _open_city():
    """Open the city database once, lazily and thread-safely."""
    global _CITY, _CITY_TRIED
    with _RLock:
        if _CITY_TRIED:
            return _CITY
        _CITY_TRIED = True
        import mmdb
        for path in city_paths():
            if not os.path.isfile(path):
                continue
            if str(path).lower().endswith(".mmdb"):
                try:
                    _CITY = mmdb.open_database(str(path))
                except Exception:
                    continue
            else:
                try:
                    _CITY = _LegacyCity(str(path))
                except Exception:
                    continue
            break
        return _CITY


def city_available() -> bool:
    """True when state/city data can actually be resolved."""
    return _open_city() is not None


def city_source() -> str:
    """A short human description of where state data comes from."""
    db = _open_city()
    if db is None:
        return _INSTALL_CITY_HINT
    return str(getattr(db, "path", "") or city_path())


def city_status() -> dict:
    """Everything the Settings dialog needs to describe the state database."""
    db = _open_city()
    path = next((p for p in city_paths() if p.is_file()), None)
    info = {"available": db is not None,
            "path": str(path) if path else str(city_path()),
            "size": path.stat().st_size if path else 0,
            "updated": 0.0}
    if db is not None and isinstance(getattr(db, "metadata", None), dict):
        info["updated"] = float(db.metadata.get("build_epoch") or 0)
    elif path is not None:
        try:
            info["updated"] = path.stat().st_mtime
        except OSError:
            pass
    return info


def _english(entry) -> str:
    """The English name of a GeoLite/DB-IP name map (any language fallback)."""
    if not isinstance(entry, dict):
        return ""
    names = entry.get("names")
    if not isinstance(names, dict):
        return ""
    value = _clean(str(names.get("en") or next(iter(names.values()), "")))
    return value


def locate(host: str) -> tuple[str, str, str]:
    """``(state, state code, city)`` for a literal address.

    Returns ``("","","")`` when the address is not a literal, nothing is
    installed, or the database has no entry -- the rest of the program
    then treats the state as unknown rather than failing.
    """
    if not host:
        return ("", "", "")
    hit = _CITY_CACHE.get(host)
    if hit is not None:
        return hit
    if not _looks_like_ip(host):
        _CITY_CACHE[host] = ("", "", "")
        return _CITY_CACHE[host]

    db = _open_city()
    state = code = city = ""
    if db is not None:
        try:
            with _RLock:                     # the legacy binding is not
                record = db.get(host)
        except Exception:
            record = None
        if isinstance(record, dict):
            divisions = record.get("subdivisions") or []
            first = divisions[0] if divisions else {}
            state = _english(first)
            iso = first.get("iso_code")
            code = str(iso).strip().upper() if isinstance(iso, str) else ""
            if state and not code and len(state) == 2 and state.isalpha():
                code = state.upper()         # legacy DBs store the code only
            city = _english(record.get("city"))
    result = (state, code, city)
    _CITY_CACHE[host] = result
    return result


def state(host: str) -> tuple[str, str]:
    """``(name, code)`` of the state/province an address sits in."""
    name, code, _ = locate(host)
    return (name, code)


def state_of(host: str) -> str:
    """Just the state name -- "" when no city database is installed."""
    return locate(host)[0]


def city_of(host: str) -> str:
    """Just the city name -- "" when no city database is installed."""
    return locate(host)[2]


def city_db_urls(when: date | None = None) -> list[str]:
    """Candidate download URLs: this month's DB-IP release, then last's."""
    today = when or date.today()
    first = today.replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1)
    return [CITY_URL.format(ym=today.strftime("%Y-%m")),
            CITY_URL.format(ym=previous.strftime("%Y-%m"))]


def fetch_city_db(url: str | None = None, *, progress=None,
                  timeout: float = 300.0) -> Path:
    """Download the city/state database once and install it.

    `url` overrides the default DB-IP monthly URL(s).  `progress(done, total)`
    is called while downloading so a panel can show a bar.  Returns the
    installed path; the file is validated by opening it before it replaces
    anything, and lookups stay offline afterwards.  Raises `OSError` when
    every source failed.
    """
    dest = city_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [url] if url else city_db_urls()
    errors = []
    for candidate in urls:
        gz = dest.with_suffix(".gz.part")
        part = dest.with_suffix(".part")
        try:
            _download(candidate, gz, progress=progress, timeout=timeout)
            _decompress(gz, part)
            _validate(part)
            os.replace(part, dest)
        except Exception as exc:                 # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
            gz.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            continue
        finally:
            gz.unlink(missing_ok=True)
        reset()                                  # drop stale handles/caches
        return dest
    raise OSError("no city database could be fetched -- "
                  + "; ".join(errors))


def _download(url: str, target: Path, *, progress=None,
              timeout: float = 300.0, cap: int = CITY_SIZE_CAP) -> None:
    """Stream `url` into `target`, capped at `cap` bytes."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "RotatingProxy/1.0 (geo database)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                done += len(chunk)
                if done > cap:
                    raise OSError("downloaded file is larger than expected")
                fh.write(chunk)
                if progress:
                    progress(done, total)


def _decompress(gz_path: Path, target: Path,
                *, cap: int = CITY_SIZE_CAP) -> None:
    """Gunzip `gz_path` into `target`, again with a size cap."""
    done = 0
    with gzip.open(gz_path, "rb") as src, target.open("wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            done += len(chunk)
            if done > cap:
                raise OSError("downloaded file is larger than expected")
            out.write(chunk)


def _validate(path: Path) -> None:
    """Open the file and read one record, so junk never replaces a good DB."""
    import mmdb
    with mmdb.open_database(str(path)) as db:
        if not int(db.metadata.get("node_count") or 0):
            raise OSError("downloaded file has no search tree")
        record = db.get("8.8.8.8")
        if not isinstance(record, dict):
            raise OSError("downloaded file does not answer lookups")


def remove_city_db() -> bool:
    """Delete the downloaded database (the state picker goes empty again)."""
    removed = False
    for path in (city_path(), city_path().with_suffix(".part"),
                 city_path().with_suffix(".gz.part")):
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError:
            pass
    if removed:
        reset()
    return removed


# ---------------------------------------------------------------------------
# ASN database: what *kind* of network an address sits on
# ---------------------------------------------------------------------------
# Exit verification asks two different questions about an address: where is
# it (city database, above) and who runs it (here).  The answer to the
# second is what tells a hosting/VPN exit -- the kind of address Google and
# friends already distrust -- apart from an ordinary one.
def asn_path() -> Path:
    """Where `fetch_asn_db()` puts the database."""
    return geo_dir() / ASN_FILENAME


def asn_paths() -> tuple:
    """Every place an ASN database could already be (ours first)."""
    return (asn_path(), *(Path(p) for p in SYSTEM_ASN_PATHS))


def asn_db_urls(when: date | None = None) -> list[str]:
    """Candidate download URLs: this month's DB-IP release, then last's."""
    today = when or date.today()
    first = today.replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1)
    return [ASN_URL.format(ym=today.strftime("%Y-%m")),
            ASN_URL.format(ym=previous.strftime("%Y-%m"))]


def _open_asn():
    """Open the ASN database once, lazily and thread-safely."""
    global _ASN, _ASN_TRIED
    with _RLock:
        if _ASN_TRIED:
            return _ASN
        _ASN_TRIED = True
        import mmdb
        for path in asn_paths():
            if not os.path.isfile(path):
                continue
            try:
                _ASN = mmdb.open_database(str(path))
            except Exception:
                continue
            break
        return _ASN


def asn_available() -> bool:
    """True when network/organisation data can actually be resolved."""
    return _open_asn() is not None


def asn_source() -> str:
    """A short human description of where ASN data comes from."""
    db = _open_asn()
    if db is None:
        return _INSTALL_ASN_HINT
    return str(getattr(db, "path", "") or asn_path())


def asn_status() -> dict:
    """Everything the Settings dialog needs to describe the ASN database."""
    db = _open_asn()
    path = next((p for p in asn_paths() if p.is_file()), None)
    info = {"available": db is not None,
            "path": str(path) if path else str(asn_path()),
            "size": path.stat().st_size if path else 0,
            "updated": 0.0}
    if db is not None and isinstance(getattr(db, "metadata", None), dict):
        info["updated"] = float(db.metadata.get("build_epoch") or 0)
    elif path is not None:
        try:
            info["updated"] = path.stat().st_mtime
        except OSError:
            pass
    return info


def asn_info(host: str) -> tuple[int, str]:
    """`(number, organisation)` for an address; `(0, "")` when unknown.

    Needs the ASN database (`run.py --asn-fetch`): like the city database
    it is downloaded once and read offline afterwards.  Unknown addresses
    answer `(0, "")` rather than raising, so a missing or broken file just
    means "no network data" for that row.
    """
    if not _looks_like_ip(host):
        return 0, ""
    with _RLock:
        hit = _ASN_CACHE.get(host)
    if hit is not None:
        return hit
    record = None
    db = _open_asn()
    if db is not None:
        try:
            record = db.get(host)
        except Exception:
            record = None
    number, org = 0, ""
    if isinstance(record, dict):
        try:
            number = int(record.get("autonomous_system_number") or 0)
        except (TypeError, ValueError):
            number = 0
        org = _clean(record.get("autonomous_system_organization"))
    answer = (number, org)
    with _RLock:
        _ASN_CACHE[host] = answer
    return answer


def fetch_asn_db(url: str | None = None, *, progress=None,
                 timeout: float = 300.0) -> Path:
    """Download the ASN database once and install it.

    Same contract as `fetch_city_db`: `progress(done, total)` for a panel
    bar, the file is opened before it replaces anything, lookups stay
    offline afterwards, and `OSError` is raised when every source failed.
    """
    dest = asn_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [url] if url else asn_db_urls()
    errors = []
    for candidate in urls:
        gz = dest.with_suffix(".gz.part")
        part = dest.with_suffix(".part")
        try:
            _download(candidate, gz, progress=progress, timeout=timeout,
                      cap=ASN_SIZE_CAP)
            _decompress(gz, part, cap=ASN_SIZE_CAP)
            _validate(part)                      # same shape, same checks
            os.replace(part, dest)
        except Exception as exc:                 # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
            gz.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            continue
        finally:
            gz.unlink(missing_ok=True)
        reset()                                  # drop stale handles/caches
        return dest
    raise OSError("no ASN database could be fetched -- " + "; ".join(errors))


def remove_asn_db() -> bool:
    """Delete the downloaded ASN database (risk flags go empty again)."""
    removed = False
    for path in (asn_path(), asn_path().with_suffix(".part"),
                 asn_path().with_suffix(".gz.part")):
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError:
            pass
    if removed:
        reset()
    return removed


def reset() -> None:
    """Forget every database handle and every cached answer."""
    global _DB, _TRIED, _CITY, _CITY_TRIED, _ASN, _ASN_TRIED
    with _RLock:
        _CACHE.clear()
        _CITY_CACHE.clear()
        _ASN_CACHE.clear()
        _DB = None
        _TRIED = False
        db, _CITY = _CITY, None
        _CITY_TRIED = False
        asn, _ASN = _ASN, None
        _ASN_TRIED = False
    for handle in (db, asn):
        if handle is not None:
            try:
                handle.close()
            except Exception:                   # noqa: BLE001
                pass
