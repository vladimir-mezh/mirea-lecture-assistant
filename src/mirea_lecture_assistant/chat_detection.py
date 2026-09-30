from __future__ import annotations

import re

# Names are written with a capital letter; lowercase words ("кто из ИКБО-01-24")
# are ordinary chat, not a roll call.
NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
MIREA_GROUP = r"(?i:[А-ЯЁA-Z]{2,8}-\d{2}-\d{2})"
LINE = re.compile(rf"^(?:{NAME}\s+){{1,2}}{NAME}\s+{MIREA_GROUP}[.!]?$")
# A roll call is several classmates writing their names, not one or two.
MINIMUM_DISTINCT = 3


def attendance_like_messages(page_text: str, group: str, own_message: str) -> tuple[str, ...]:
    """Find distinct chat lines shaped like 'Фамилия Имя ГРУППА'."""
    own = " ".join(own_message.casefold().split())
    matches: list[str] = []
    seen: set[str] = set()
    for source_line in page_text.splitlines():
        line = " ".join(source_line.strip().split())
        line = re.sub(r"^\d{1,2}:\d{2}\s+", "", line)
        folded = line.casefold()
        if not LINE.fullmatch(line) or folded == own or folded in seen:
            continue
        seen.add(folded)
        matches.append(line)
    return tuple(matches)


def classmates_report_attendance_issue(
    page_text: str,
    group: str,
    own_message: str,
    minimum_distinct: int = MINIMUM_DISTINCT,
    baseline: frozenset[str] = frozenset(),
) -> bool:
    """Whether classmates are answering a roll call in the chat right now.

    ``baseline`` holds the lines that were already on screen when the lecture
    was joined (display names in the chat history, say): only lines that
    appeared afterwards count.
    """
    fresh = [
        line
        for line in attendance_like_messages(page_text, group, own_message)
        if line.casefold() not in baseline
    ]
    return len(fresh) >= minimum_distinct


def chat_baseline(page_text: str, group: str, own_message: str) -> frozenset[str]:
    return frozenset(
        line.casefold() for line in attendance_like_messages(page_text, group, own_message)
    )
