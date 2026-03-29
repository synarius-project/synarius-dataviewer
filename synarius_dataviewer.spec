# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller one-file Windows build bundling ``synarius_dataviewer`` and ``synarius_core``.

From repo root (after ``pip install . pyinstaller``)::

    pyinstaller --noconfirm --clean synarius_dataviewer.spec
"""
from __future__ import annotations

from pathlib import Path

from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all

_repo = Path(SPECPATH)
_src_main = _repo / "src" / "synarius_dataviewer" / "__main__.py"

datas: list[tuple[str, str]] = []
binaries: list = []
hiddenimports: list[str] = []

for _pkg in ("synarius_core", "sqlalchemy"):
    d, b, h = collect_all(_pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(_src_main)],
    pathex=[str(_repo / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="synarius-dataviewer",
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
)
