#!/usr/bin/env bash
#
# Build the Debian package from the PyInstaller bundle.
#
#     packaging/linux/build-deb.sh <version>
#
# Expects dist/rotating-proxy/ (the pyinstaller output) to exist.  The
# result is a self-contained .deb that installs the app to
# /opt/rotating-proxy with launchers on the PATH and a .desktop entry,
# so double-clicking it in a file manager opens the distro's software
# centre and one click on "Install" is all it takes.
#
set -euo pipefail

VERSION="${1:?usage: build-deb.sh <version>}"
VERSION="${VERSION#v}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUNDLE="$ROOT/dist/rotating-proxy"
PKG="rotating-proxy"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
STAGE="$ROOT/build/deb"
OUT="$ROOT/dist/${PKG}_${VERSION}_${ARCH}.deb"

if [ ! -x "$BUNDLE/rotating-proxy" ]; then
    echo "error: $BUNDLE/rotating-proxy missing -- run pyinstaller first" >&2
    exit 1
fi

echo "==> staging $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/opt/$PKG" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/$PKG" \
         "$STAGE/DEBIAN"

cp -a "$BUNDLE/." "$STAGE/opt/$PKG/"

# ---- launchers ------------------------------------------------------------
cat > "$STAGE/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec /opt/rotating-proxy/rotating-proxy "$@"
EOF
cat > "$STAGE/usr/bin/${PKG}-ctl" <<'EOF'
#!/bin/sh
exec /opt/rotating-proxy/proxyctl "$@"
EOF
chmod 755 "$STAGE/usr/bin/$PKG" "$STAGE/usr/bin/${PKG}-ctl"

# ---- desktop entry and icons ---------------------------------------------
install -m 644 "$HERE/rotating-proxy.desktop" \
    "$STAGE/usr/share/applications/rotating-proxy.desktop"
desktop-file-validate "$STAGE/usr/share/applications/rotating-proxy.desktop"

for size in 1024 512 256 128 64 48 32 16; do
    mkdir -p "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps"
    install -m 644 "$ROOT/packaging/icons/icon-${size}.png" \
        "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/rotating-proxy.png"
done

# ---- documentation --------------------------------------------------------
install -m 644 "$ROOT/README.md" "$STAGE/usr/share/doc/$PKG/README.md"
cat > "$STAGE/usr/share/doc/$PKG/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Rotating Proxy
Source: https://github.com/thao-glitch/rotating-proxy

Files: *
Copyright: Rotating Proxy contributors
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a
 copy of this software and associated documentation files (the "Software"),
 to deal in the Software without restriction, including without limitation
 the rights to use, copy, modify, merge, publish, distribute, sublicense,
 and/or sell copies of the Software, and to permit persons to whom the
 Software is furnished to do so, subject to the following conditions:
 The above copyright notice and this permission notice shall be included
 in all copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
 OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
EOF

# ---- control --------------------------------------------------------------
SIZE_KB="$(du -sk "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: ${PKG}
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Maintainer: Rotating Proxy contributors <rotating-proxy@users.noreply.github.com>
Installed-Size: ${SIZE_KB}
Description: local rotating HTTP/SOCKS proxy with a desktop panel
 A forwarding proxy that rotates across a pool of free upstream proxies
 (HTTP, SOCKS4 and SOCKS5), with a dark Tkinter dashboard in front of it.
 Point any application at http://127.0.0.1:8888, or run rotating-proxy-ctl
 on to point the whole machine at it.
 .
 The bundled runtime is self-contained: no system Python is required.
 Country labels appear only when a GeoIP database is present.
EOF

echo "==> building $OUT"
# --root-owner-group: no fakeroot needed, files land owned by root:root
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT" >/dev/null
dpkg-deb -I "$OUT" | sed -n '1,12p'
echo "==> $OUT ($(du -h "$OUT" | cut -f1))"
