# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CatStack native desktop app.

Build:
    pyinstaller desktop/CatStack.spec --clean --noconfirm

Output: dist/CatStack/   (onedir bundle; ship the whole folder)
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

# Bundle the FastAPI static UI so Path(__file__).parent / "static" still works
# inside the frozen binary.
datas = [
    (str(ROOT / "mfarm" / "web" / "static"), "mfarm/web/static"),
]

# Some deps load submodules by string name — declare them so PyInstaller
# can't quietly drop them.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("uvicorn.protocols")
    + collect_submodules("uvicorn.lifespan")
    + collect_submodules("uvicorn.loops")
    + collect_submodules("starlette")
    + collect_submodules("anyio")
    + collect_submodules("rich")
    + collect_submodules("mfarm")
    + [
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "wsproto",
        "httptools",
        "watchfiles",
        "h11",
        "click",
        "rich",
        "paramiko",
        "cryptography",
        "bcrypt",
        "nacl",
        "cffi",
    ]
)

# uvloop is Linux/macOS only; pull its data/submodules opportunistically.
try:
    hiddenimports += collect_submodules("uvloop")
except Exception:
    pass

# Cryptography ships data files for OpenSSL backends.
datas += collect_data_files("cryptography")


a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CatStack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # stdout/stderr visible; same binary doubles as the CLI
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CatStack",
)
