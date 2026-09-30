from datetime import UTC, datetime, timedelta

from mirea_lecture_assistant.database import MISSING_TOLERANCE, Database
from mirea_lecture_assistant.domain import Lesson, RuleMode


def test_lessons_rules_and_links_round_trip(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    start = datetime(2026, 9, 3, 10, 40, tzinfo=UTC)
    lesson = Lesson("lesson-1", "Физика", "ЛЕК", start, start + timedelta(minutes=90))
    db.sync_lessons([lesson], start - timedelta(days=1))
    db.set_rule("Физика", RuleMode.AUTO)
    db.set_link("Физика", "https://example.test/room")

    loaded = db.list_lessons()
    assert loaded == [lesson]
    assert db.get_rule("Физика") is RuleMode.AUTO
    assert db.get_link("Физика") == "https://example.test/room"


def _lesson(number: int, day: int = 3) -> Lesson:
    start = datetime(2026, 9, day, 9 + number, 0, tzinfo=UTC)
    return Lesson(
        f"lesson-{day}-{number}", f"Предмет {number}", "ЛЕК", start, start + timedelta(minutes=90)
    )


def test_a_day_missing_from_one_refresh_keeps_its_lessons(tmp_path):
    """Pulse is polled per day and a failed day returns empty, not an error."""
    db = Database(tmp_path / "test.sqlite3")
    keep_from = datetime(2026, 9, 3, tzinfo=UTC)
    full = [_lesson(1), _lesson(2), _lesson(3)]
    db.sync_lessons(full, keep_from)

    missing, dropped = db.sync_lessons([full[0], full[2]], keep_from)

    assert missing == 1
    assert dropped == 0
    assert len(db.list_lessons()) == 3


def test_a_lesson_absent_from_many_refreshes_is_dropped(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    keep_from = datetime(2026, 9, 3, tzinfo=UTC)
    full = [_lesson(1), _lesson(2)]
    db.sync_lessons(full, keep_from)

    for _ in range(MISSING_TOLERANCE - 1):
        db.sync_lessons([full[0]], keep_from)
    assert len(db.list_lessons()) == 2

    _missing, dropped = db.sync_lessons([full[0]], keep_from)
    assert dropped == 1
    assert [x.external_id for x in db.list_lessons()] == [full[0].external_id]


def test_a_lesson_that_reappears_stops_counting_as_missing(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    keep_from = datetime(2026, 9, 3, tzinfo=UTC)
    full = [_lesson(1), _lesson(2)]
    db.sync_lessons(full, keep_from)

    db.sync_lessons([full[0]], keep_from)
    db.sync_lessons([full[0]], keep_from)
    db.sync_lessons(full, keep_from)
    db.sync_lessons([full[0]], keep_from)
    db.sync_lessons([full[0]], keep_from)

    assert len(db.list_lessons()) == 2


def test_updated_lesson_details_overwrite_the_cached_row(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    keep_from = datetime(2026, 9, 3, tzinfo=UTC)
    lesson = _lesson(1)
    db.sync_lessons([lesson], keep_from)

    moved = Lesson(
        lesson.external_id,
        lesson.subject_name,
        lesson.lesson_type,
        lesson.start_at,
        lesson.end_at,
        teacher="Иванов И.И.",
        room="Онлайн",
    )
    db.sync_lessons([moved], keep_from)

    assert db.list_lessons() == [moved]


def test_finished_days_are_pruned(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    yesterday = _lesson(1, day=2)
    today = _lesson(1, day=3)
    db.sync_lessons([yesterday, today], datetime(2026, 9, 2, tzinfo=UTC))

    db.sync_lessons([yesterday, today], datetime(2026, 9, 3, tzinfo=UTC))

    assert [x.external_id for x in db.list_lessons()] == [today.external_id]


def test_sources_are_kept_per_subject(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.set_sources("Физика", ["https://a.test/1", " https://a.test/2 ", "  "])

    assert db.get_sources("Физика") == ["https://a.test/1", "https://a.test/2"]
    assert db.get_sources("Химия") == []


def test_sources_are_replaced_not_appended(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.set_sources("Физика", ["https://a.test/1"])
    db.set_sources("Физика", ["https://a.test/2"])

    assert db.get_sources("Физика") == ["https://a.test/2"]


def test_each_lesson_keeps_its_own_room(tmp_path):
    """Webinars differ per lesson, so a link cannot be stored against the subject."""
    db = Database(tmp_path / "test.sqlite3")
    db.set_resolved_link("lesson-1", "https://my.mts-link.ru/j/1/11")
    db.set_resolved_link("lesson-2", "https://my.mts-link.ru/j/1/22")

    assert db.get_resolved_link("lesson-1") == "https://my.mts-link.ru/j/1/11"
    assert db.get_resolved_link("lesson-2") == "https://my.mts-link.ru/j/1/22"
    assert db.get_resolved_link("lesson-3") == ""


def test_a_room_found_again_replaces_the_previous_one(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.set_resolved_link("lesson-1", "https://my.mts-link.ru/j/1/11")
    db.set_resolved_link("lesson-1", "https://my.mts-link.ru/j/1/12")

    assert db.get_resolved_link("lesson-1") == "https://my.mts-link.ru/j/1/12"


def test_a_running_lesson_survives_a_long_schedule_outage(tmp_path):
    """A Pulse outage used to delete the running pair and close its lecture."""
    db = Database(tmp_path / "test.sqlite3")
    now = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
    running = Lesson(
        "running", "Физика", "ЛЕК", now - timedelta(minutes=30), now + timedelta(hours=1)
    )
    db.sync_lessons([running], now.replace(hour=0), now=now)

    for _ in range(MISSING_TOLERANCE * 2):
        db.sync_lessons([], now.replace(hour=0), now=now)

    assert [x.external_id for x in db.list_lessons()] == ["running"]


def test_rows_with_different_offsets_are_compared_as_moments(tmp_path):
    from datetime import timezone

    db = Database(tmp_path / "test.sqlite3")
    msk = timezone(timedelta(hours=3))
    lesson = Lesson(
        "late",
        "Физика",
        "ЛЕК",
        datetime(2026, 9, 3, 1, 0, tzinfo=msk),
        datetime(2026, 9, 3, 2, 30, tzinfo=msk),
    )
    db.sync_lessons([lesson], datetime(2026, 9, 2, tzinfo=UTC))

    # 02:30 MSK is 23:30 UTC on the 2nd: already over when the 3rd begins in UTC.
    _missing, dropped = db.sync_lessons([lesson], datetime(2026, 9, 3, tzinfo=UTC))

    assert dropped == 1


def test_batch_lookups_match_single_ones(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.set_rule("Физика", RuleMode.AUTO)
    db.set_link("Физика", "https://example.test/room")
    db.set_resolved_link("lesson-1", "https://mts-link.ru/event/1")

    assert db.all_rules() == {"Физика": RuleMode.AUTO}
    assert db.all_links() == {"Физика": "https://example.test/room"}
    assert db.all_resolved_links() == {"lesson-1": "https://mts-link.ru/event/1"}


def test_the_next_pair_survives_an_hour_long_schedule_outage(tmp_path):
    from mirea_lecture_assistant.database import NEAR_FUTURE_TOLERANCE

    db = Database(tmp_path / "test.sqlite3")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    upcoming = Lesson(
        "next", "Физика", "ЛЕК", now + timedelta(hours=1), now + timedelta(hours=2, minutes=30)
    )
    db.sync_lessons([upcoming], now.replace(hour=0), now=now)

    for _ in range(NEAR_FUTURE_TOLERANCE - 1):
        db.sync_lessons([], now.replace(hour=0), now=now)
    assert [x.external_id for x in db.list_lessons()] == ["next"]

    db.sync_lessons([], now.replace(hour=0), now=now)
    assert db.list_lessons() == []  # a real cancellation still goes away
