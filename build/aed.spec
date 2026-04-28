# PyInstaller spec - single-file binary, no extra installs.
# Run:  pyinstaller build/aed.spec --clean

import os, sys

block_cipher = None

repo = os.path.abspath(os.path.dirname(os.path.dirname(__file__))) \
    if "__file__" in globals() else os.path.abspath(".")

a = Analysis(
    [os.path.join(repo, "aed.py")],
    pathex=[repo],
    binaries=[],
    datas=[],
    hiddenimports=["aed", "aed.scanner", "aed.health", "aed.imager",
                   "aed.analyzer", "aed.recover", "aed.carver", "aed.wizard"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

manifest = os.path.join(repo, "build", "aed.manifest") if sys.platform == "win32" else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="aed",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=manifest,
    uac_admin=True,            # request elevation at launch on Windows
    icon=None,
)
