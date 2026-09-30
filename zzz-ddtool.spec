# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir 打包：薄壳 exe + _internal/（依赖）+ data/（build.py 复制到 exe 同级）

import os

import rapidocr

_rapidocr_models = os.path.join(os.path.dirname(rapidocr.__file__), "models")

a = Analysis(
    ["src/zzz_ddtool/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        # rapidocr 模型是 .onnx，PyInstaller 收集不到，必须手动带进 _internal
        (_rapidocr_models, "rapidocr/models"),
        # 认页模板，运行时 assets_dir() 从包内读
        ("src/zzz_ddtool/assets/templates", "zzz_ddtool/assets/templates"),
    ],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name="zzz-ddtool",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="zzz-ddtool",
)
