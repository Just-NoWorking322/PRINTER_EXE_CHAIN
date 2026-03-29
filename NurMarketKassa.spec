# -*- mode: python ; coding: utf-8 -*-
import os

_spec_dir = os.path.dirname(os.path.abspath(SPEC))
_cap = os.path.join(_spec_dir, "bundle_escpos", "capabilities.json")

a = Analysis(
    ["main.py"],
    pathex=[_spec_dir],
    binaries=[],
    datas=[(_cap, "escpos")] if os.path.isfile(_cap) else [],
    hiddenimports=[
        "escpos.printer",
        "escpos.capabilities",
        "serial",
        "serial.tools.list_ports",
        "usb.core",
        "usb.util",
    ],
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
    a.datas,
    [],
    name="NurMarketKassa",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
