from __future__ import annotations

from types import SimpleNamespace

from mirea_lecture_assistant import browser_warning


def test_notice_helper_is_hidden_bounded_and_never_takes_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_warning.sys, "platform", "win32")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"1")

    monkeypatch.setattr(browser_warning.subprocess, "run", run)
    assert browser_warning.dismiss_password_notice()
    args, kwargs = calls[0]
    assert "Hidden" in args and kwargs["timeout"] == 6
    assert "SendKeys" not in browser_warning.SCRIPT
    assert "Invoke()" in browser_warning.SCRIPT
    assert "Chrome_WidgetWin_1" in browser_warning.SCRIPT
    assert "утечки данных" in browser_warning.SCRIPT


def test_notice_not_available_is_not_reported_as_closed(monkeypatch):
    monkeypatch.setattr(
        browser_warning.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"0"),
    )
    assert not browser_warning.dismiss_password_notice()
