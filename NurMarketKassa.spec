# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\TechLine\\OneDrive\\Рабочий стол\\принтер\\bundle_escpos\\capabilities.json', 'escpos')],
    hiddenimports=['escpos.printer', 'escpos.capabilities', 'serial', 'serial.tools.list_ports', 'usb.core', 'usb.util', 'win32print', 'win32file', 'win32con', 'pywintypes', 'win32api', 'win32ui'],
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
    name='NurMarketKassa',
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
    version='C:\\Users\\TechLine\\AppData\\Local\\Temp\\8fb935ee-f60d-4ec3-985c-1360d443aacf',
)
