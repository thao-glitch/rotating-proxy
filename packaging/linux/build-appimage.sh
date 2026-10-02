#!/usr/bin/env bash
#
# Build a self-contained AppImage from the PyInstaller bundle.
#
#     packaging/linux/build-appimage.sh <version>
#
# Expects dist/rotating-proxy/ to exist.  Downloads appimagetool (once,
# into build/) and produces dist/RotatingProxy-<version>-x86_64.AppImage,
# a single file that runs by double-clicking it after
#   chmod +x  (file managers and browsers do not keep the bit).
#
set -euo pipefail

VERSION="${1:?usage: build-appimage.sh <version>}"
VERSION="${VERSION#v}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUNDLE="$ROOT/dist/rotating-proxy"
APPDIR="$ROOT/build/RotatingProxy.AppDir"
TOOL="$ROOT/build/appimagetool"
OUT="$ROOT/dist/RotatingProxy-${VERSION}-x86_64.AppImage"

if [ ! -x "$BUNDLE/rotating-proxy" ]; then
    echo "error: $BUNDLE/rotating-proxy missing -- run pyinstaller first" >&2
    exit 1
fi

echo "==> staging $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr"
cp -a "$BUNDLE" "$APPDIR/usr/rotating-proxy"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# AppImage entry point: run the bundled desktop panel.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/rotating-proxy/rotating-proxy" "$@"
EOF
chmod 755 "$APPDIR/AppRun"

install -m 644 "$HERE/rotating-proxy.desktop" "$APPDIR/rotating-proxy.desktop"
install -m 644 "$ROOT/packaging/icons/icon-512.png" "$APPDIR/rotating-proxy.png"
ln -sf rotating-proxy.png "$APPDIR/.DirIcon"
desktop-file-validate "$APPDIR/rotating-proxy.desktop"

if [ ! -x "$TOOL" ]; then
    echo "==> downloading appimagetool"
    mkdir -p "$(dirname "$TOOL")"
    curl -fsSL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod 755 "$TOOL"
fi

echo "==> building $OUT"
export ARCH=x86_64
# runners often have no /dev/fuse -- extract and run instead of mounting
export APPIMAGE_EXTRACT_AND_RUN=1
"$TOOL" "$APPDIR" "$OUT" >/dev/null

echo "==> $OUT ($(du -h "$OUT" | cut -f1))"
