#!/usr/bin/env bash
#
# Build the macOS installer package from the PyInstaller bundle.
#
#     packaging/macos/build-pkg.sh <version> <path-to-.app> [output.pkg]
#
# Stages the .app under /Applications (plus an optional CLI shim in
# /usr/local/bin when the bundle contains proxyctl) and runs pkgbuild.
# Double-clicking the result opens the standard Installer, which drops
# the panel into /Applications.
#
# Unsigned by default: set MACOS_INSTALLER_IDENTITY (e.g. "Developer ID
# Installer: You (TEAMID)") to have productbuild sign the result.
#
set -euo pipefail

VERSION="${1:?usage: build-pkg.sh <version> <Rotating Proxy.app> [out.pkg]}"
VERSION="${VERSION#v}"
APP="${2:?usage: build-pkg.sh <version> <Rotating Proxy.app> [out.pkg]}"
OUT="${3:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if [ -z "$OUT" ]; then
    OUT="$ROOT/dist/RotatingProxy-${VERSION}-macos.pkg"
fi
if [ ! -d "$APP" ]; then
    echo "error: $APP not found -- run pyinstaller first" >&2
    exit 1
fi

STAGE="$ROOT/build/pkgstage"
echo "==> staging $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/Applications" "$STAGE/usr/local/bin"

APP_NAME="$(basename "$APP")"
cp -a "$APP" "$STAGE/Applications/"

# CLI shim -- only when this build actually bundles proxyctl
if [ -f "$APP/Contents/MacOS/proxyctl" ]; then
    cat > "$STAGE/usr/local/bin/rotating-proxy-ctl" <<EOF
#!/bin/sh
exec "/Applications/${APP_NAME}/Contents/MacOS/proxyctl" "\$@"
EOF
    chmod 755 "$STAGE/usr/local/bin/rotating-proxy-ctl"
else
    echo "note: proxyctl missing from the bundle, skipping the CLI shim"
fi

echo "==> building $OUT"
mkdir -p "$(dirname "$OUT")"
pkgbuild \
    --root "$STAGE" \
    --identifier io.rotatingproxy.pkg \
    --version "$VERSION" \
    --install-location / \
    "$OUT" >/dev/null

if [ -n "${MACOS_INSTALLER_IDENTITY:-}" ]; then
    echo "==> signing with $MACOS_INSTALLER_IDENTITY"
    productbuild --sign "$MACOS_INSTALLER_IDENTITY" "$OUT" "$OUT.signed"
    mv "$OUT.signed" "$OUT"
fi

echo "==> $OUT ($(du -h "$OUT" | cut -f1))"
