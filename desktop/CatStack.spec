# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: CatStack.exe (GUI, no console) + CatStackCLI.exe (console).

Both exes share a single onedir bundle — Python runtime and all dependencies
are only included once.

Build:
    pyinstaller desktop/CatStack.spec --clean --noconfirm

Output: dist/CatStack/
    CatStack.exe      double-click to open the dashboard in a chromeless window
    CatStackCLI.exe   console entry for CLI commands: rig list, status, ...
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "mfarm" / "web" / "static"), "mfarm/web/static"),
    # Ship VERSION alongside mfarm/__init__.py so __version__ resolves at runtime.
    (str(ROOT / "VERSION"), "mfarm"),
]

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
        "paramiko",
        "cryptography",
        "bcrypt",
        "nacl",
        "cffi",
    ]
)

try:
    hiddenimports += collect_submodules("uvloop")
except Exception:
    pass

datas += collect_data_files("cryptography")

excludes = [
    "tkinter",
    "matplotlib",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "notebook",
]


gui_analysis = Analysis(
    [str(ROOT / "desktop" / "launcher_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

cli_analysis = Analysis(
    [str(ROOT / "desktop" / "launcher_cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# MERGE dedupes shared modules/data between the two analyses so the onedir
# bundle only contains one copy of Python, uvicorn, paramiko, etc.
MERGE(
    (gui_analysis, "launcher_gui", "CatStack"),
    (cli_analysis, "launcher_cli", "CatStackCLI"),
)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="CatStack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no black console window on double-click
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="CatStackCLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # CLI needs a console to print
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CatStack",
)
