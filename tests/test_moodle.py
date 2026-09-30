from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mirea_lecture_assistant.moodle import (
    COURSES_URL,
    discover_course_urls,
    find_courses,
    find_sso_login_links,
    find_webinar_modules,
    looks_like_login_page,
    match_webinar,
    module_group_rank,
    parse_webinars,
    resolve_lecture_url,
)

MODULE_URL = "https://online-edu.mirea.ru/mod/webinars/view.php?id=964187"
SUBJECT = "Зарубежные среды разработки приложений дополненной и виртуальной реальностей"
# 1788415200 == 03.09.2026 09:00 local, exactly as the page renders it.
LESSON_START = datetime.fromtimestamp(1788415200).astimezone()
LESSON_END = LESSON_START + timedelta(minutes=90)
# Markup copied from the live page: the control is a button carrying data-href.
JOIN_BUTTON = (
    '<button class="btn wb2-join-btn" data-id="188788" '
    'data-href="https://my.mts-link.ru/j/10000001/20000000002">Подключиться</button>'
)
RECORD_BUTTON = (
    '<button class="btn secondary wb2-record-btn" data-id="188787" '
    'data-href="https://my.mts-link.ru/j/10000001/20000000001/record-new/30000000001">'
    "Смотреть запись</button>"
)


def row(
    *,
    epoch: int = 1788415200,
    end_epoch: str = "1788426702",
    groups: tuple[str, ...] = ("ИКБО-13-99", "ИКБО-12-99", "ИКБО-11-99"),
    title: str = f"03.09.2026 09:00 ЛК. {SUBJECT}",
    action: str = JOIN_BUTTON,
) -> str:
    tags = "".join(f'<span class="group-tag">{name}</span>' for name in groups)
    return (
        f'<tr><td data-val="{title.casefold()}">{title}</td>'
        f'<td data-val="{epoch}">03.09.2026 09:00</td>'
        f'<td data-val="{end_epoch}">3.09.2026 12:11</td>'
        f"<td>{tags}</td>"
        f'<td class="actions">{action}</td></tr>'
    )


def page(*rows: str) -> str:
    body = "".join(rows)
    return (
        '<div id="wb2-table"><table class="data"><thead><tr><th>НАЗВАНИЕ</th>'
        "<th>НАЧАЛО</th><th>ОКОНЧАНИЕ</th><th>ГРУППЫ</th><th>ДЕЙСТВИЯ</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def test_row_values_come_from_the_machine_readable_attributes():
    (webinar,) = parse_webinars(page(row()), MODULE_URL)

    assert webinar.start_at == LESSON_START
    assert webinar.end_at == datetime.fromtimestamp(1788426702).astimezone()
    assert webinar.groups == ("ИКБО-13-99", "ИКБО-12-99", "ИКБО-11-99")
    assert webinar.join_url == "https://my.mts-link.ru/j/10000001/20000000002"
    assert webinar.is_joinable


def test_a_webinar_without_a_button_yet_is_parsed_but_not_joinable():
    (webinar,) = parse_webinars(page(row(action="Ожидается")), MODULE_URL)

    assert webinar.join_url is None
    assert not webinar.is_joinable


def test_an_open_ended_webinar_is_accepted():
    (webinar,) = parse_webinars(page(row(end_epoch="")), MODULE_URL)

    assert webinar.end_at is None


def _match(html: str, *, group: str = "ИКБО-11-99", subject: str = SUBJECT):
    return match_webinar(
        parse_webinars(html, MODULE_URL),
        subject=subject,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group=group,
        tolerance=timedelta(minutes=20),
    )


def test_matches_own_group_slot_and_subject():
    assert _match(page(row())) is not None


def test_another_group_is_rejected():
    assert _match(page(row(groups=("ИКБО-01-24",)))) is None


def test_another_subject_in_the_same_slot_is_rejected():
    assert _match(page(row(title="03.09.2026 09:00 ЛК. Технологии анализа больших данных"))) is None


def test_a_webinar_from_another_pair_is_rejected():
    later = int(LESSON_START.timestamp()) + 3 * 3600
    assert _match(page(row(epoch=later))) is None


def test_a_webinar_starting_late_within_the_pair_is_accepted():
    """Teachers often create the room after the pair has already started."""
    late = int(LESSON_START.timestamp()) + 40 * 60
    assert _match(page(row(epoch=late))) is not None


def test_the_closest_joinable_webinar_wins():
    late = int(LESSON_START.timestamp()) + 10 * 60
    picked = _match(page(row(epoch=late, action="Ожидается"), row()))

    assert picked is not None
    assert picked.start_at == LESSON_START


def test_joinable_webinar_for_own_group_beats_closer_unlabeled_one():
    own_group_later = int(LESSON_START.timestamp()) + 10 * 60
    picked = _match(
        page(
            row(groups=(), epoch=int(LESSON_START.timestamp())),
            row(groups=("ИКБО-11-99",), epoch=own_group_later),
        )
    )

    assert picked is not None
    assert picked.groups == ("ИКБО-11-99",)


def test_joinable_unlabeled_webinar_beats_own_group_without_join_link():
    picked = _match(
        page(
            row(groups=("ИКБО-11-99",), action="Ожидается"),
            row(groups=(), epoch=int(LESSON_START.timestamp()) + 10 * 60),
        )
    )

    assert picked is not None and picked.is_joinable
    assert picked.groups == ()


def test_group_written_with_a_dash_variant_still_matches():
    assert _match(page(row()), group="икбо–11–99") is not None


SECTION_HTML = (
    '<div class="course-content">'
    '<a href="/mod/webinars/view.php?id=964187">03.09.2026 Зарубежные среды разработки</a>'
    '<a href="/mod/webinars/view.php?id=964188">10.09.2026 Зарубежные среды разработки</a>'
    '<a href="/mod/assign/view.php?id=1">Задание</a>'
    "</div>"
)


def test_only_webinar_modules_are_followed():
    modules = find_webinar_modules(SECTION_HTML, "https://online-edu.mirea.ru/course/section.php")

    assert [url for _title, url in modules] == [
        "https://online-edu.mirea.ru/mod/webinars/view.php?id=964187",
        "https://online-edu.mirea.ru/mod/webinars/view.php?id=964188",
    ]


def test_a_module_page_source_is_read_directly():
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return page(row())

    assert (
        resolve_lecture_url(
            fetch,
            [MODULE_URL],
            subject=SUBJECT,
            start_at=LESSON_START,
            end_at=LESSON_END,
            group="ИКБО-11-99",
        )
        is not None
    )
    assert fetched == [MODULE_URL]


def test_a_section_source_follows_the_module_named_after_this_date_first():
    pages = {
        "https://online-edu.mirea.ru/course/section.php?id=113677": SECTION_HTML,
        "https://online-edu.mirea.ru/mod/webinars/view.php?id=964187": page(row()),
        "https://online-edu.mirea.ru/mod/webinars/view.php?id=964188": page(),
    }
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return pages[url]

    found = resolve_lecture_url(
        fetch,
        ["https://online-edu.mirea.ru/course/section.php?id=113677"],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group="ИКБО-11-99",
    )

    assert found is not None and found.is_joinable
    modules = [url for url in fetched if "/mod/webinars/" in url]
    assert modules[0] == "https://online-edu.mirea.ru/mod/webinars/view.php?id=964187"


def test_a_broken_source_does_not_stop_the_others():
    def fetch(url: str) -> str:
        if "964000" in url:
            raise TimeoutError("СДО не ответила")
        return page(row())

    assert (
        resolve_lecture_url(
            fetch,
            ["https://online-edu.mirea.ru/mod/webinars/view.php?id=964000", MODULE_URL],
            subject=SUBJECT,
            start_at=LESSON_START,
            end_at=LESSON_END,
            group="ИКБО-11-99",
        )
        is not None
    )


def test_nothing_is_returned_when_the_webinar_has_not_appeared_yet():
    assert (
        resolve_lecture_url(
            lambda _url: page(),
            [MODULE_URL],
            subject=SUBJECT,
            start_at=LESSON_START,
            end_at=LESSON_END,
            group="ИКБО-11-99",
        )
        is None
    )


LOGIN_HTML = (
    '<div class="loginform">'
    '<a href="/auth/oidc/?login">Войти через учётную запись МИРЭА</a>'
    '<a href="/login/forgot_password.php">Забыли пароль?</a>'
    "</div>"
)


def test_a_login_page_is_recognised():
    assert looks_like_login_page(LOGIN_HTML)
    assert not looks_like_login_page(page(row()))


def test_optional_max_page_is_recognised_as_unfinished_login():
    assert looks_like_login_page(
        '<form action="/realms/mirea/login-actions/required-action?execution=max-account-config">'
        "<button>Пропустить</button></form>"
    )


def test_the_sso_link_is_found_and_password_recovery_is_not():
    links = find_sso_login_links(LOGIN_HTML, "https://online-edu.mirea.ru/login/index.php")

    assert links == ["https://online-edu.mirea.ru/auth/oidc/?login"]


def test_a_finished_webinar_offering_a_recording_is_not_joinable():
    """Opening the recording instead of the room would be worse than opening nothing."""
    (webinar,) = parse_webinars(page(row(action=RECORD_BUTTON)), MODULE_URL)

    assert webinar.is_recording
    assert not webinar.is_joinable


def test_a_recording_is_never_chosen_for_a_lesson():
    assert _match(page(row(action=RECORD_BUTTON))) is None


# Anchors copied from the student's own course list.
COURSES_HTML = """
<div class="course-listitems">
  <a href="https://online-edu.mirea.ru/course/view.php?id=16738">
     Разработка многопользовательских приложений виртуальной реальности (часть 1/1) [I.26-27]</a>
  <a href="https://online-edu.mirea.ru/course/view.php?id=16334">
     Зарубежные среды разработки приложений дополненной и виртуальной реальностей (часть 1/1)</a>
  <a href="https://online-edu.mirea.ru/course/view.php?id=15978">
     Зарубежные инструментальные средства компьютерной графики (КР/КП)</a>
  <a href="https://online-edu.mirea.ru/course/view.php?id=122">Как учиться в электронной среде</a>
  <a href="https://online-edu.mirea.ru/my/">Личный кабинет</a>
</div>
"""


def test_courses_are_read_from_the_list():
    courses = find_courses(COURSES_HTML, COURSES_URL)

    assert len(courses) == 4
    assert (
        "Как учиться в электронной среде",
        "https://online-edu.mirea.ru/course/view.php?id=122",
    ) in courses


def test_the_course_of_a_subject_is_found_despite_the_title_suffix():
    urls = discover_course_urls(lambda _url: COURSES_HTML, SUBJECT)

    assert urls == ["https://online-edu.mirea.ru/course/view.php?id=16334"]


def test_every_course_of_a_subject_is_returned():
    urls = discover_course_urls(
        lambda _url: COURSES_HTML, "Зарубежные инструментальные средства компьютерной графики"
    )

    assert urls == ["https://online-edu.mirea.ru/course/view.php?id=15978"]


def test_an_unknown_subject_finds_no_course():
    assert discover_course_urls(lambda _url: COURSES_HTML, "Военная подготовка") == []


def test_a_shortened_course_title_is_found():
    html = (
        '<a href="https://online-edu.mirea.ru/course/view.php?id=9">'
        "Зарубежные среды разработки приложений виртуальной реальности</a>"
    )
    assert discover_course_urls(lambda _url: html, SUBJECT) == [
        "https://online-edu.mirea.ru/course/view.php?id=9"
    ]


def test_total_sdo_outage_is_not_reported_as_an_absent_webinar():
    def unavailable(_url: str) -> str:
        raise TimeoutError("СДО не ответила")

    with pytest.raises(RuntimeError, match="Ни одна страница"):
        resolve_lecture_url(
            unavailable,
            [MODULE_URL],
            subject=SUBJECT,
            start_at=LESSON_START,
            end_at=LESSON_END,
            group="ИКБО-11-99",
        )


def test_one_generic_row_can_be_identified_by_group_and_time():
    generic = page(row(title="Лекция"))

    found = resolve_lecture_url(
        lambda _url: generic,
        [MODULE_URL],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group="ИКБО-11-99",
    )

    assert found is not None and found.is_joinable


def test_two_generic_rows_are_not_guessed():
    second = int(LESSON_START.timestamp()) + 10 * 60
    generic = page(row(title="Лекция"), row(epoch=second, title="Вебинар"))

    assert (
        resolve_lecture_url(
            lambda _url: generic,
            [MODULE_URL],
            subject=SUBJECT,
            start_at=LESSON_START,
            end_at=LESSON_END,
            group="ИКБО-11-99",
        )
        is None
    )


def test_generic_row_with_own_group_beats_unlabeled_row():
    later = int(LESSON_START.timestamp()) + 10 * 60
    generic = page(
        row(title="Лекция", groups=()),
        row(title="Вебинар", groups=("ИКБО-11-99",), epoch=later),
    )

    found = resolve_lecture_url(
        lambda _url: generic,
        [MODULE_URL],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group="ИКБО-11-99",
    )

    assert found is not None and found.groups == ("ИКБО-11-99",)


def test_all_modules_are_read_before_choosing():
    """A teacher may post two webinars: the dead one on time, the real one later."""
    course = "https://online-edu.mirea.ru/course/view.php?id=16335"
    on_time = "https://online-edu.mirea.ru/mod/webinars/view.php?id=964332"
    late = "https://online-edu.mirea.ru/mod/webinars/view.php?id=964390"
    late_start = int(LESSON_START.timestamp()) + 18 * 60
    pages = {
        course: f'<a href="{on_time}">Лектор</a><a href="{late}">Лектор (2)</a>',
        on_time: page(row(action="Ожидается")),
        late: page(row(epoch=late_start)),
    }
    read: list[str] = []

    def fetch(url: str) -> str:
        read.append(url)
        return pages[url]

    found = resolve_lecture_url(
        fetch,
        [course],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group="ИКБО-11-99",
    )

    assert found is not None and found.is_joinable
    assert found.start_at == datetime.fromtimestamp(late_start).astimezone()
    assert set(read) == {course, on_time, late}


def test_the_exact_slot_wins_when_both_can_be_joined():
    course = "https://online-edu.mirea.ru/course/view.php?id=16335"
    first = "https://online-edu.mirea.ru/mod/webinars/view.php?id=1"
    second = "https://online-edu.mirea.ru/mod/webinars/view.php?id=2"
    pages = {
        course: f'<a href="{first}">Лектор</a><a href="{second}">Лектор (2)</a>',
        first: page(row(epoch=int(LESSON_START.timestamp()) + 18 * 60)),
        second: page(row()),
    }

    found = resolve_lecture_url(
        lambda url: pages[url],
        [course],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group="ИКБО-11-99",
    )

    assert found is not None and found.start_at == LESSON_START


MY_GROUP = "ИКБО-11-99"
MY_TEACHER = "Петров Пётр Петрович"


def test_a_module_naming_my_group_is_read_first():
    course = "https://online-edu.mirea.ru/course/view.php?id=16335"
    theirs = "https://online-edu.mirea.ru/mod/webinars/view.php?id=1"
    mine = "https://online-edu.mirea.ru/mod/webinars/view.php?id=2"
    pages = {
        course: (
            f'<a href="{theirs}">Лекция для ИКБО-01-24</a>'
            f'<a href="{mine}">Лекция для ИКБО-11-99</a>'
        ),
        theirs: page(),
        mine: page(row()),
    }
    read: list[str] = []

    found = resolve_lecture_url(
        lambda url: (read.append(url), pages[url])[1],
        [course],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group=MY_GROUP,
    )

    assert found is not None and found.is_joinable
    assert next(url for url in read if "/mod/webinars/" in url) == mine


def test_other_groups_are_not_read_once_a_room_is_found():
    """Reading a module addressed to other groups only wastes seconds."""
    course = "https://online-edu.mirea.ru/course/view.php?id=16335"
    mine = "https://online-edu.mirea.ru/mod/webinars/view.php?id=2"
    theirs = "https://online-edu.mirea.ru/mod/webinars/view.php?id=1"
    pages = {
        course: f'<a href="{theirs}">ИКБО-01-24</a><a href="{mine}">ИКБО-11-99</a>',
        mine: page(row()),
        theirs: page(),
    }
    read: list[str] = []

    resolve_lecture_url(
        lambda url: (read.append(url), pages[url])[1],
        [course],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group=MY_GROUP,
    )

    assert theirs not in read


def test_my_teacher_breaks_the_tie_when_no_group_is_named():
    course = "https://online-edu.mirea.ru/course/view.php?id=16335"
    other = "https://online-edu.mirea.ru/mod/webinars/view.php?id=1"
    mine = "https://online-edu.mirea.ru/mod/webinars/view.php?id=2"
    pages = {
        course: (
            f'<a href="{other}">Лекции Лектор - Сидоров Сидор Сидорович</a>'
            f'<a href="{mine}">Лекции Лектор - Петров Пётр Петрович</a>'
        ),
        other: page(),
        mine: page(row()),
    }
    read: list[str] = []

    found = resolve_lecture_url(
        lambda url: (read.append(url), pages[url])[1],
        [course],
        subject=SUBJECT,
        start_at=LESSON_START,
        end_at=LESSON_END,
        group=MY_GROUP,
        teacher=MY_TEACHER,
    )

    assert found is not None
    assert next(url for url in read if "/mod/webinars/" in url) == mine


def test_a_module_without_group_or_teacher_still_beats_another_group():
    assert module_group_rank("Лекция для ИКБО-11-99", MY_GROUP) == 0
    assert module_group_rank("Вебинары по дисциплине", MY_GROUP) == 1
    assert module_group_rank("Лекция для ИКБО-01-24", MY_GROUP) == 2


def test_group_spelling_in_a_title_is_matched_loosely():
    assert module_group_rank("лекция икбо-11-99 поток", "ИКБО-11-99") == 0
