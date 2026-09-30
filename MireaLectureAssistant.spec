# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


qt_runtime_dir = Path(SPECPATH) / ".venv" / "Lib" / "site-packages" / "PySide6"
qt_runtime_names = {
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll",
    "vcamp140.dll",
    "vccorlib140.dll",
    "vcomp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}

a = Analysis(
    ["src/mirea_lecture_assistant_launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=[("assets/app_icon.png", "assets"), ("assets/app_icon.ico", "assets")],
    hiddenimports=["keyring.backends.Windows", "zxingcpp", "mss", "numpy"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# Python 3.12 ships an older VC runtime than current PySide6. If that older copy
# is placed beside the executable, Windows loads it first and QtCore cannot find
# procedures introduced in the newer runtime bundled by Qt.
a.binaries = [
    entry
    for entry in a.binaries
    if entry[0].lower() not in qt_runtime_names
    and not entry[0].lower().startswith("api-ms-win-")
    and "codex-runtimes" not in entry[1].lower()
]
a.binaries += [
    (name, str(qt_runtime_dir / name), "BINARY")
    for name in sorted(qt_runtime_names)
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MireaLectureAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/app_icon.ico",
)
