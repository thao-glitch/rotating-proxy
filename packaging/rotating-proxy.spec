# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Rotating Proxy — one onedir bundle, two executables.

    APP_VERSION=1.0.0 pyinstaller packaging/rotating-proxy.spec

Produces dist/rotating-proxy/ containing:

    rotating-proxy   the desktop panel (windowed on Windows)
    proxyctl         the "point my apps at it" CLI (console on Windows)
    _internal/       shared runtime, Tcl/Tk and the pure-python modules

MERGE() collapses the modules both entry points need into one copy, so
the bundle is barely bigger than a single-binary build.  The spec runs
identically on Windows, macOS and Linux; per-OS differences (icon,
version resource, .app bundle) are branched on sys.platform.
"""

import os
import re
import sys

APP_VERSION = os.environ.get("APP_VERSION", "0.0.0").lstrip("v")
SPEC_DIR = os.path.abspath(SPECPATH)          # .../packaging
ROOT = os.path.dirname(SPEC_DIR)              # repository root
ICONS = os.path.join(SPEC_DIR, "icons")
BUILD_DIR = os.path.join(ROOT, "build")       # gitignored scratch space


def _version_tuple(text):
    """'1.2.3-rc1' -> (1, 2, 3, 0), always four numeric parts."""
    numbers = [int(n) for n in re.findall(r"\d+", text)][:4]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def _version_resource(exe_name, description):
    """Write a Windows VSVersionInfo file; returns its path (or None)."""
    if sys.platform != "win32":
        return None
    ver = _version_tuple(APP_VERSION)
    path = os.path.join(BUILD_DIR, f"file_version_info_{exe_name}.txt")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver!r},
    prodvers={ver!r},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Rotating Proxy contributors'),
        StringStruct('FileDescription', {description!r}),
        StringStruct('FileVersion', {APP_VERSION!r}),
        StringStruct('InternalName', {exe_name!r}),
        StringStruct('OriginalFilename', {exe_name + '.exe'!r}),
        StringStruct('ProductName', 'Rotating Proxy'),
        StringStruct('ProductVersion', {APP_VERSION!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
    return path


WINDOWS_ICON = os.path.join(ICONS, "icon.ico")
MACOS_ICON = os.path.join(ICONS, "icon.icns")

# ---------------------------------------------------------------- analyses
a_gui = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

a_ctl = Analysis(
    [os.path.join(ROOT, "proxyctl.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# keep the modules both entry points share in the first analysis only
MERGE(
    (a_gui, "rotating-proxy", "rotating-proxy"),
    (a_ctl, "proxyctl", "proxyctl"),
)

pyz_gui = PYZ(a_gui.pure)
pyz_ctl = PYZ(a_ctl.pure)

# ------------------------------------------------------------ executables
gui_kwargs = dict(
    exclude_binaries=True,
    name="rotating-proxy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
ctl_kwargs = dict(gui_kwargs, name="proxyctl", console=True)

if sys.platform == "win32":
    gui_kwargs["icon"] = WINDOWS_ICON
    gui_kwargs["version"] = _version_resource(
        "rotating-proxy", "Rotating Proxy desktop panel")
    ctl_kwargs["icon"] = WINDOWS_ICON
    ctl_kwargs["version"] = _version_resource(
        "proxyctl", "Rotating Proxy command line control tool")

exe_gui = EXE(pyz_gui, a_gui.scripts, [], **gui_kwargs)
exe_ctl = EXE(pyz_ctl, a_ctl.scripts, [], **ctl_kwargs)

coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_ctl,
    a_ctl.binaries,
    a_ctl.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rotating-proxy",
)

# ------------------------------------------------------------- macOS .app
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Rotating Proxy.app",
        icon=MACOS_ICON,
        version=APP_VERSION,
        bundle_identifier="io.rotatingproxy.app",
        info_plist={
            "CFBundleName": "Rotating Proxy",
            "CFBundleDisplayName": "Rotating Proxy",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "LSMinimumSystemVersion": "10.13",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
