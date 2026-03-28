# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\TechLine\\OneDrive\\Рабочий стол\\принтер\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('bundle_escpos\\capabilities.json', 'escpos')],
    hiddenimports=['escpos.printer', 'escpos.capabilities', 'serial', 'usb.core', 'usb.util'],
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
    name='NurMarketKassa_new',
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
    version='C:\\Users\\TechLine\\AppData\\Local\\Temp\\2f07c439-dca2-433f-8e79-e7573db05443',
)
