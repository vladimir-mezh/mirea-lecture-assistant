from datetime import UTC, datetime, timedelta

import pytest

from mirea_lecture_assistant.relative_time import format_relative_time

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=2), "Только что"),
        (timedelta(minutes=1), "1 минуту назад"),
        (timedelta(minutes=5), "5 минут назад"),
        (timedelta(minutes=21), "21 минуту назад"),
        (timedelta(minutes=59), "59 минут назад"),
        (timedelta(hours=1), "час назад"),
        (timedelta(hours=2), "2 часа назад"),
        (timedelta(hours=5), "5 часов назад"),
    ],
)
def test_recent_relative_time(delta, expected):
    assert format_relative_time(NOW - delta, NOW) == expected


def test_yesterday_and_older_dates():
    assert format_relative_time(datetime(2026, 9, 2, 14, 0, tzinfo=UTC), NOW) == "вчера"
    assert format_relative_time(datetime(2026, 9, 1, 14, 0, tzinfo=UTC), NOW) == "позавчера"
    assert format_relative_time(datetime(2026, 8, 31, 14, 0, tzinfo=UTC), NOW) == "31 августа 2026"
