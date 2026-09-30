from __future__ import annotations

from datetime import datetime

MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _plural(value: int, one: str, few: str, many: str) -> str:
    tail = value % 100
    if 11 <= tail <= 14:
        return many
    tail = value % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def format_relative_time(moment: datetime, now: datetime | None = None) -> str:
    """Return a compact Russian description of when *moment* happened."""
    now = now or datetime.now(tz=moment.tzinfo)
    seconds = max(0, int((now - moment).total_seconds()))

    if seconds < 60:
        return "Только что"
    if seconds < 3600:
        minutes = seconds // 60
        word = _plural(minutes, "минуту", "минуты", "минут")
        return f"{minutes} {word} назад"
    if seconds < 7200:
        return "час назад"
    if seconds < 86400:
        hours = seconds // 3600
        word = _plural(hours, "час", "часа", "часов")
        return f"{hours} {word} назад"

    days = (now.date() - moment.date()).days
    if days == 1:
        return "вчера"
    if days == 2:
        return "позавчера"
    return f"{moment.day} {MONTHS[moment.month]} {moment.year}"
