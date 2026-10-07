from __future__ import annotations

from pathlib import Path

from mirea_lecture_assistant import browsers


def test_program_is_read_from_the_registered_command():
    assert browsers.program_from_command(
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --single-argument %1'
    ) == Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
    assert browsers.program_from_command("C:\\Yandex\\browser.exe %1") == Path(
        "C:\\Yandex\\browser.exe"
    )
    assert browsers.program_from_command("") is None


def test_only_chromium_browsers_default_first(tmp_path, monkeypatch):
    programs = {}
    for name in ("chrome.exe", "msedge.exe", "firefox.exe", "browser.exe"):
        programs[name] = tmp_path / name
        programs[name].write_bytes(b"MZ")
    opera = tmp_path / "Opera" / "launcher.exe"
    opera.parent.mkdir()
    opera.write_bytes(b"MZ")
    commands = [(name, f'"{path}" %1') for name, path in programs.items()]
    commands += [("OperaStable", f'"{opera}"'), ("Chrome-again", f'"{programs["chrome.exe"]}"')]
    monkeypatch.setattr(browsers.sys, "platform", "win32")
    monkeypatch.setattr(browsers, "_registry_commands", lambda: commands)
    monkeypatch.setattr(browsers, "_default_program", lambda: programs["browser.exe"])

    found = browsers.installed()

    assert [b.name for b in found] == ["Яндекс Браузер", "Google Chrome", "Microsoft Edge", "Opera"]
    assert found[0].default and not any(b.default for b in found[1:])
    assert found[0].extensions_page == "browser://extensions/"
