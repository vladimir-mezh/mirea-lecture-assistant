from __future__ import annotations

from pathlib import Path

from mirea_lecture_assistant import autostart


class FakeRegistry:
    """The part of winreg the autostart setting uses, in memory."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}

    class _Key:
        def __init__(self, registry):
            self.registry = registry

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def OpenKey(self, _root, _path):
        return self._Key(self)

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_start_with_windows_can_be_switched_on_and_off():
    registry = FakeRegistry()
    exe = Path(r"C:\Users\student\Desktop\MireaLectureAssistant.exe")

    autostart.set_enabled(True, exe, winreg=registry)
    assert autostart.registered(winreg=registry) == f'"{exe}" --autostart'

    autostart.set_enabled(False, exe, winreg=registry)
    assert autostart.registered(winreg=registry) is None
    autostart.set_enabled(False, exe, winreg=registry)  # already off: no error


def test_a_start_with_windows_is_recognised():
    assert autostart.launched_at_sign_in(["app.exe", "--autostart"])
    assert not autostart.launched_at_sign_in(["app.exe"])
