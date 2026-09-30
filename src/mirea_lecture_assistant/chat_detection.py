from __future__ import annotations

import re

NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
MIREA_GROUP = r"[А-ЯЁA-Z]{2,8}-\d{2}-\d{2}"


def attendance_like_messages(page_text: str, group: str, own_message: str) -> tuple[str, ...]:
    """Find distinct visible messages shaped like 'Фамилия Имя ГРУППА'."""
    pattern = re.compile(
        rf"^(?:{NAME}\s+){{2,3}}{MIREA_GROUP}[.!]?$",
        re.IGNORECASE,
    )
    own = " ".join(own_message.casefold().split())
    matches: list[str] = []
    for source_line in page_text.splitlines():
        line = " ".join(source_line.strip().split())
        line = re.sub(r"^\d{1,2}:\d{2}\s+", "", line)
        if not pattern.fullmatch(line) or line.casefold() == own:
            continue
        folded = line.casefold()
        if folded not in {item.casefold() for item in matches}:
            matches.append(line)
    return tuple(matches)


def classmates_report_attendance_issue(
    page_text: str, group: str, own_message: str, minimum_distinct: int = 2
) -> bool:
    return len(attendance_like_messages(page_text, group, own_message)) >= minimum_distinct
