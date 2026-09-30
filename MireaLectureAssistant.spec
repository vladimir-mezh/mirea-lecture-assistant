# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


# Qt parts the app never touches. PySide6's hooks bring them in through plugins
# (the virtual keyboard input context pulls in Qt Quick and QML, the PDF image
# format pulls in Qt PDF), and together with the software OpenGL fallback and
# 40 translations they made up a large share of the executable.
UNUSED_QT = (
    "qt6quick",
    "qt6qml",
    "qt6pdf",
    "qt6virtualkeyboard",
    "qt6network",
    "qt6opengl",
    "qt6svg",
    "qt6waylandclient",
    "qt6wlshell",
    "opengl32sw",
    "/plugins/platforminputcontexts/",
    "/plugins/iconengines/",
    "/plugins/networkinformation/",
    "/plugins/tls/",
    "/plugins/generic/",
    "/plugins/egldeviceintegrations/",
    "/plugins/wayland",
)
KEPT_IMAGE_FORMATS = ("qico",)  # PNG is built into QtGui; the window icon is an .ico
KEPT_TRANSLATIONS = ("qtbase_ru",)


def needed(entry) -> bool:
    name = "/" + entry[0].replace("\\", "/").lower()
    if any(part in name for part in UNUSED_QT):
        return False
    if "/plugins/imageformats/" in name:
        return any(kept in name for kept in KEPT_IMAGE_FORMATS)
    if "/translations/" in name and "pyside6" in name:
        return any(kept in name for kept in KEPT_TRANSLATIONS)
    return True


a = Analysis(
    ["src/mirea_lecture_assistant_launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=[("assets/app_icon.png", "assets"), ("assets/app_icon.ico", "assets")],
    hiddenimports=["keyring.backends.Windows", "zxingcpp", "mss"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy",
        "tkinter",
        "PySide6.QtNetwork",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtPdf",
        "PySide6.QtOpenGL",
        "PySide6.QtSvg",
    ],
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if needed(entry)]
a.datas = [entry for entry in a.datas if needed(entry)]

if sys.platform == "win32":
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
    # Python 3.12 ships an older VC runtime than current PySide6. If that older
    # copy is placed beside the executable, Windows loads it first and QtCore
    # cannot find procedures introduced in the newer runtime bundled by Qt.
    a.binaries = [
        entry
        for entry in a.binaries
        if entry[0].lower() not in qt_runtime_names
        and not entry[0].lower().startswith("api-ms-win-")
        and "codex-runtimes" not in entry[1].lower()
    ]
    a.binaries += [
        (name, str(qt_runtime_dir / name), "BINARY") for name in sorted(qt_runtime_names)
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
