# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('static', 'static'), ('assets', 'assets'), ('allow_firewall.bat', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Onedir build: DLLs live beside the exe instead of being unpacked into %TEMP%\_MEI*
# on every launch, which broke when the installer force-closed a running copy or
# temp cleaners/antivirus removed the folder ("Failed to load Python DLL").
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NearBeam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='NearBeam',
)
