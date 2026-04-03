# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('C:\\Users\\TechLine\\OneDrive\\Рабочий стол\\принтер_для_next\\PRINTER_EXE_CHAIN\\bundle_escpos\\capabilities.json', 'escpos'),
        ('C:\\Users\\TechLine\\OneDrive\\Рабочий стол\\принтер_для_next\\PRINTER_EXE_CHAIN\\assets\\header_logo.png', 'assets'),
    ],
    hiddenimports=['escpos.printer', 'escpos.capabilities', 'serial', 'serial.tools.list_ports', 'usb.core', 'usb.util'],
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
    version='C:\\Users\\TechLine\\AppData\\Local\\Temp\\4e8bbddc-37f0-42b2-9c9f-b7b5a8ec847e',
    icon='C:\\Users\\TechLine\\OneDrive\\Рабочий стол\\принтер_для_next\\PRINTER_EXE_CHAIN\\assets\\nurmarket_logo.ico',
)
