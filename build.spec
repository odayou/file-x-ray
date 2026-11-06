# -*- mode: python ; coding: utf-8 -*-
# 跨平台打包脚本：onedir 作为**构建中间产物**，再由各平台打包器加工成安装包/镜像。
#   Windows: dist\文件索引工具\  → installer\file-xray.iss（Inno Setup）→ dist\installer\*.exe
#   macOS:   dist\文件索引工具.app（BUNDLE）→ hdiutil 生成 .dmg
#   Linux:   dist\文件索引工具\ → tar.gz（保留可执行位）
# 本地/CI 均在项目根目录执行：pyinstaller build.spec --noconfirm
#
# 版本号：CI 从 tag 推导后通过环境变量 FIT_VERSION 注入；本地不设置时用
# assets/version_info.txt 里的默认版本，保证三处（版本资源 / .iss / CI）不漂移。
# 控制台：默认窗口模式（FIT_CONSOLE=0）；排查问题可设 FIT_CONSOLE=1 重新打包。
import os
import re
import sys

CONSOLE = os.environ.get("FIT_CONSOLE", "0") != "0"
VERSION = os.environ.get("FIT_VERSION", "").strip().lstrip("v")
block_cipher = None


def _win_version_file():
    """Windows 版本资源：注入 FIT_VERSION 时生成带版本号的副本，否则用仓库内默认文件。"""
    src = os.path.join("assets", "version_info.txt")
    if not os.path.isfile(src):
        return None
    if not VERSION:
        return src
    nums = [p for p in re.split(r"[.\-+]", VERSION) if p.isdigit()]
    nums = (nums + ["0", "0", "0", "0"])[:4]
    with open(src, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"filevers=\(\d+, \d+, \d+, \d+\)",
                  "filevers=(%s)" % ", ".join(nums), text, count=1)
    text = re.sub(r"prodvers=\(\d+, \d+, \d+, \d+\)",
                  "prodvers=(%s)" % ", ".join(nums), text, count=1)
    text = re.sub(r"StringStruct\('FileVersion', '[^']*'\)",
                  "StringStruct('FileVersion', '%s')" % VERSION, text, count=1)
    text = re.sub(r"StringStruct\('ProductVersion', '[^']*'\)",
                  "StringStruct('ProductVersion', '%s')" % VERSION, text, count=1)
    os.makedirs("build", exist_ok=True)
    out = os.path.join("build", "version_info.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    return out


# 第三方包常带 DLL/资源：显式收集，避免打包后转写/PDF 功能缺件。
extra_datas, extra_binaries, extra_hiddenimports = [], [], []
try:
    from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs
    # ctranslate2 / faster_whisper 整包收集是必要的：前者带 ctranslate2.dll
    # 与 libiomp5md.dll，后者带 assets/silero_vad_v6.onnx（VAD 必需）。
    for _pkg in ("ctranslate2", "faster_whisper"):
        try:
            _d, _b, _h = collect_all(_pkg)
            extra_datas += _d
            extra_binaries += _b
            extra_hiddenimports += _h
        except Exception:
            pass
    # pymupdf 只收集核心动态库（_mupdf.pyd / mupdfcpp64.dll 等），**不要 collect_all**：
    # 2.x 的 collect_all 会把 layout（近 50MB ONNX 模型）和 mupdf-devel（头文件/.lib）
    # 一起打包，并顺带把 onnxruntime.transformers → pandas/matplotlib 拉进来。
    # 本程序只用 open/文本抽取/页面渲染；1.28.x 没有 layout，收集方式完全兼容。
    try:
        extra_binaries += collect_dynamic_libs("pymupdf")
    except Exception:
        pass
except Exception:
    pass

a = Analysis(
    ['backend.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=[('index.html', '.'), ('web', 'web')] + extra_datas,
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.h11',
        'uvicorn.protocols.websockets', 'uvicorn.lifespan', 'uvicorn.server',
        'click', 'h11', 'starlette', 'sqlite3',
    ] + extra_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # PyMuPDF 2.x 的可选版面分析链路（本程序只用基础文本抽取/渲染）。
        # 1.28.x 没有这些模块，对新装环境无影响；本机若仍是 2.0，pymupdf 导入处的
        # try/except ImportError 会静默跳过。
        'pymupdf.layout', 'pymupdf4llm', 'pymupdf_layout',
        # 上述链路的传递依赖：PyMuPDF 的 Table.to_pandas()/layout 只是在函数内惰性
        # import，打包时删掉不影响核心 PDF 抽取与渲染。
        'pandas', 'matplotlib', 'lxml',
        'IPython', 'jedi', 'zmq', 'tkinter', '_tkinter',
        # onnxruntime 本体必须保留（faster-whisper 开启 VAD 时用 Silero VAD），
        # 只裁掉 transformers/quantization/tools/training 这些开发/训练子包；
        # 正是它们把 pandas/matplotlib 拉进产物。
        'onnxruntime.transformers', 'onnxruntime.quantization',
        'onnxruntime.tools', 'onnxruntime.training',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Windows 才支持图标与版本资源；macOS 的图标与元数据由下面的 BUNDLE 负责。
exe_kw = {}
if sys.platform == "win32":
    exe_kw["icon"] = os.path.join("assets", "app.ico")
    _ver = _win_version_file()
    if _ver:
        exe_kw["version"] = _ver

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='文件索引工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # 原生 DLL（ctranslate2/pymupdf）经 UPX 压缩易损坏，保持关闭
    console=CONSOLE,
    **exe_kw,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='文件索引工具',
)

# macOS：把 onedir 目录包成 .app（仅 darwin 上可用；其他平台该变量不会定义）
if sys.platform == "darwin":
    _icns = os.path.join("assets", "app.icns")
    app = BUNDLE(
        coll,
        name='文件索引工具.app',
        icon=_icns if os.path.isfile(_icns) else None,
        bundle_identifier='com.filexray.desktop',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.13.0',
        },
    )
