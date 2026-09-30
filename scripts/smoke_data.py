"""Fill the data directory with what a real student has, for the exe smoke test.

The build smoke test used to start on an empty database, so nothing that draws
lessons, history or the saved dark theme ran before release.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mirea_lecture_assistant.database import Database
from mirea_lecture_assistant.domain import Lesson
from mirea_lecture_assistant.paths import data_dir


def main() -> None:
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    db = Database(data_dir() / "assistant.sqlite3")
    db.set_setting("group", "ИКБО-01-24")
    db.set_setting("student_name", "Иванов Иван")
    db.set_setting("theme", "dark")
    db.sync_lessons(
        [
            Lesson(
                "smoke-past",
                "Физика",
                "Лекция",
                now - timedelta(hours=4),
                now - timedelta(hours=2, minutes=30),
                "Иванов И. И.",
                "ИКБО-01-24",
                "Дистанционно",
                None,
                True,
            ),
            Lesson(
                "smoke-running",
                "Математический анализ",
                "Лекция",
                now - timedelta(minutes=10),
                now + timedelta(minutes=80),
                "Петров П. П.",
                "ИКБО-01-24",
                "Дистанционно",
                None,
                True,
            ),
            Lesson(
                "smoke-tomorrow",
                "Программирование",
                "Практика",
                now + timedelta(days=1),
                now + timedelta(days=1, minutes=90),
                None,
                "",
                "А-101",
                None,
                False,
            ),
        ],
        now - timedelta(days=1),
    )
    db.add_qr_event("smoke", "submitted", lesson_id="smoke-past")
    print("smoke data:", data_dir())


if __name__ == "__main__":
    main()
