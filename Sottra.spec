# -*- mode: python ; coding: utf-8 -*-
import glob
import os
from PyInstaller.utils.hooks import collect_all

SP = os.path.join(os.getcwd(), ".venv", "Lib", "site-packages")

datas, binaries, hiddenimports = [], [], []

# Gom trọn các gói native (binaries + datas + submodules)
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "sounddevice"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# CUDA DLL (cublas/cudnn/nvrtc) -> bỏ vào ROOT bundle để ctranslate2 tìm qua PATH
for dll in glob.glob(os.path.join(SP, "nvidia", "*", "bin", "*.dll")):
    binaries.append((dll, "."))

hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32"]

a = Analysis(
    ["app_qt.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "noisereduce", "tkinter",
              "pywebview", "pystray", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sottra",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # app cửa sổ, không console
    icon="icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Sottra",
)
