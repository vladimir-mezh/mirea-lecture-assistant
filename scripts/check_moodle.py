r"""Check how the app can read «Вебинары по дисциплине» and what it finds there.

Run it with the URL of a webinars module or of a section listing several:

    .venv\Scripts\python.exe scripts/check_moodle.py <url> [--http] [--save-page]

By default the page is read through the app's own browser profile — the same way
the app will read it — so a Chrome window opens and stays signed in to the СДО.
With --http the page is requested over HTTP with the stored MIREA session, which
is only useful for checking whether the СДО accepts it. Nothing is printed that
could expose a cookie or a token.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

from mirea_lecture_assistant.logging_setup import configure_logging
from mirea_lecture_assistant.moodle import (
    find_sso_login_links,
    find_webinar_modules,
    looks_like_login_page,
    parse_webinars,
)
from mirea_lecture_assistant.paths import data_dir
from mirea_lecture_assistant.security import SessionStore


def fetch_over_http(url: str) -> tuple[str, str]:
    """Try the stored MIREA SSO session, walking the СДО's own SSO link if asked to log in."""
    import httpx

    session = SessionStore().load() or {}
    cookies = {
        key: value
        for key, value in session.items()
        if key not in {"access_token", "refresh_token", "token_type", "expires_in"}
        and not key.startswith("__")
    }
    print(f"cookies from stored session: {len(cookies)}")
    with httpx.Client(follow_redirects=True, timeout=30, cookies=cookies) as client:
        response = client.get(url)
        if not looks_like_login_page(response.text):
            return response.text, str(response.url)

        print("страница попросила вход — пробую пройти по ссылке SSO той же сессией")
        candidates = find_sso_login_links(response.text, str(response.url))
        if not candidates:
            login = client.get("https://online-edu.mirea.ru/login/index.php")
            candidates = find_sso_login_links(login.text, str(login.url))
        print(f"ссылок SSO найдено: {len(candidates)}")
        for candidate in candidates[:3]:
            hop = client.get(candidate)
            print(f"  {urlparse(candidate).path} -> {urlparse(str(hop.url)).hostname}")
            retry = client.get(url)
            if not looks_like_login_page(retry.text):
                print("вход по сохранённой сессии SSO удался")
                return retry.text, str(retry.url)
        return response.text, str(response.url)


def fetch_over_browser(url: str) -> tuple[str, str]:
    """Read through the browser the app controls, exactly as the app will."""
    from mirea_lecture_assistant.browser_service import BrowserService

    service = BrowserService(data_dir() / "browser-profile")
    started = service.ensure_running()
    if started:
        print(f"started: {started} (окно пустое; вход в СДО, если попросит)")
    else:
        print(f"attached to the running browser on port {service.port}")
    return service.read_html(url), url


def main() -> int:
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    url = args[0]
    configure_logging(data_dir() / "logs")

    use_http = "--http" in sys.argv
    try:
        html, final_url = fetch_over_http(url) if use_http else fetch_over_browser(url)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must explain itself, not traceback
        print(f"RESULT: прочитать страницу не удалось: {type(exc).__name__}: {exc}")
        if "недоступно" in str(exc):
            print("Профиль браузера занят: закройте окно лекции, открытое приложением.")
        return 1
    print(f"final host: {urlparse(final_url).hostname}")
    print(f"html bytes: {len(html)}")
    if looks_like_login_page(html):
        if use_http:
            print("RESULT: по HTTP сохранённая сессия «Пульса» на СДО не действует")
            print("Используйте браузерный способ (без --http).")
        else:
            print("RESULT: профиль браузера ещё не вошёл в СДО")
            print("Войдите в СДО в открытом окне приложения, затем повторите эту же команду.")
            print("Окно намеренно оставлено открытым; вход сохранится в профиле.")
        return 1

    if "--save-page" in sys.argv:
        # A course page contains the student's personal data, so it is written
        # only when explicitly asked for, and next to the log, not into it.
        saved = data_dir() / "logs" / "moodle_page.html"
        saved.write_text(html, encoding="utf-8")
        print(f"страница сохранена (удалите после разбора): {saved}")

    webinars = parse_webinars(html, final_url)
    modules = find_webinar_modules(html, final_url)
    print(f"RESULT: вебинаров в таблице: {len(webinars)}; вложенных модулей: {len(modules)}")
    now = datetime.now().astimezone()
    for webinar in webinars[:10]:
        when = webinar.start_at.strftime("%d.%m.%Y %H:%M")
        age = "предстоит" if webinar.start_at > now else "прошёл"
        if webinar.is_joinable:
            link = "есть ссылка на подключение"
        elif webinar.is_recording:
            link = f"только запись ({webinar.action})"
        else:
            link = f"кнопка без ссылки: {webinar.action or '—'}"
        print(f"  {when} [{age}] {', '.join(webinar.groups) or 'без групп'} | {link}")
        print(f"    {webinar.title[:90]}")
        if not webinar.is_joinable and webinar.actions_html:
            print(f"    разметка кнопки: {webinar.actions_html[:400]}")
    for title, module_url in modules[:10]:
        print(f"  модуль: {title[:60]} -> {module_url}")
    if webinars:
        soon = min(webinars, key=lambda w: abs(w.start_at - now))
        print(
            f"ближайший по времени: {soon.start_at:%d.%m %H:%M}, разница "
            f"{abs(soon.start_at - now) // timedelta(minutes=1)} мин"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
