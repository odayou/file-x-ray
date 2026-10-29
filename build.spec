# -*- mode: python ; coding: utf-8 -*-
# 用于把工具打包成 Windows 免安装 exe（双击即用）。
# 在 Windows 上执行：
#   pip install pyinstaller
#   pyinstaller build.spec
# 产物位于 dist\文件索引工具.exe
block_cipher = None

a = Analysis(
    ['backend.py'],
    pathex=[],
    binaries=[],
    datas=[('index.html', '.')],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.h11',
        'uvicorn.protocols.websockets', 'uvicorn.lifespan', 'uvicorn.server',
        'click', 'h11', 'starlette', 'sqlite3',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='文件索引工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
)
