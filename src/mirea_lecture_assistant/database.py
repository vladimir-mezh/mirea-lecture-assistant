from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .domain import Lesson, QrEvent, RuleMode

# How many refreshes in a row may omit a lesson before it is treated as cancelled.
# Refreshes run once a minute, so this is also roughly the tolerated outage in
# minutes: three used to drop the running pair after a three-minute Pulse hiccup.
MISSING_TOLERANCE = 15


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection per thread: opening a new one per query cost a file open,
        # a PRAGMA and a commit on every scan frame and table refresh.
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self.migrate()

    def _thread_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    @contextmanager
    def connection(self):
        conn = self._thread_connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def close(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def migrate(self) -> None:
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                conn.executescript(
                    """
                    CREATE TABLE settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE lessons (
                        external_id TEXT PRIMARY KEY,
                        subject_name TEXT NOT NULL,
                        lesson_type TEXT NOT NULL,
                        teacher TEXT,
                        group_name TEXT NOT NULL DEFAULT '',
                        start_at TEXT NOT NULL,
                        end_at TEXT NOT NULL,
                        room TEXT,
                        source_url TEXT,
                        is_online INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE subject_rules (
                        subject_name TEXT PRIMARY KEY,
                        mode TEXT NOT NULL CHECK(mode IN ('AUTO', 'ASK', 'IGNORE'))
                    );
                    CREATE TABLE lecture_links (
                        subject_name TEXT PRIMARY KEY,
                        url TEXT NOT NULL
                    );
                    CREATE TABLE qr_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lesson_id TEXT,
                        token_hash TEXT NOT NULL,
                        detected_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT
                    );
                    CREATE INDEX ix_qr_events_detected_at ON qr_events(detected_at DESC);
                    CREATE INDEX ix_qr_events_hash ON qr_events(token_hash);
                    PRAGMA user_version = 1;
                    """
                )
            if version < 2:
                conn.executescript(
                    """
                    ALTER TABLE lessons
                        ADD COLUMN missing_count INTEGER NOT NULL DEFAULT 0;
                    PRAGMA user_version = 2;
                    """
                )

            if version < 3:
                conn.executescript(
                    """
                    CREATE TABLE lecture_sources (
                        subject_name TEXT NOT NULL,
                        url TEXT NOT NULL,
                        PRIMARY KEY (subject_name, url)
                    );
                    CREATE TABLE resolved_links (
                        lesson_id TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        resolved_at TEXT NOT NULL
                    );
                    PRAGMA user_version = 3;
                    """
                )

    def get_setting(self, key: str, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def reserve_auth_attempt(
        self, flow: str, max_attempts: int = 5, window_seconds: int = 1800
    ) -> bool:
        """Atomically cap all background SSO attempts in a rolling time window."""
        key = "auth_attempts"
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row is None:
                # Honor cooldowns recorded by the previous version after upgrading.
                legacy = conn.execute(
                    "SELECT value FROM settings WHERE key IN (?, ?)",
                    ("auth_attempt_last_mirea", "auth_attempt_last_sdo"),
                ).fetchall()
                values = [item["value"] for item in legacy]
            else:
                values = [row["value"]]
            try:
                attempts = []
                for value in values:
                    parsed = json.loads(value)
                    attempts.extend(parsed if isinstance(parsed, list) else [parsed])
                recent = [
                    float(attempt)
                    for attempt in attempts
                    if 0 <= now - float(attempt) < window_seconds
                ]
            except (TypeError, ValueError):
                recent = [now] * max_attempts  # Fail closed on damaged state.
            if len(recent) >= max_attempts:
                return False
            recent.append(now)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(recent)),
            )
        return True

    def sync_lessons(
        self,
        lessons: list[Lesson],
        keep_from: datetime,
        *,
        missing_tolerance: int = MISSING_TOLERANCE,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """Merge a refresh into the cache instead of overwriting it.

        The Pulse API is fetched one day at a time and a failed day comes back as
        an empty list, indistinguishable from a free day. Overwriting the cache
        with such a result makes real lessons disappear from the table. A lesson
        is therefore dropped only after it is absent from several refreshes in a
        row, and never while it is running. Returns (still missing, dropped).
        """
        now = now or datetime.now().astimezone()
        rows = [
            (
                x.external_id,
                x.subject_name,
                x.lesson_type,
                x.teacher,
                x.group_name,
                x.start_at.isoformat(),
                x.end_at.isoformat(),
                x.room,
                x.source_url,
                int(x.is_online),
            )
            for x in lessons
        ]
        present_ids = [x.external_id for x in lessons]
        placeholders = ",".join("?" * len(present_ids))
        with self.connection() as conn:
            conn.executemany(
                """INSERT INTO lessons(external_id, subject_name, lesson_type, teacher,
                   group_name, start_at, end_at, room, source_url, is_online, missing_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(external_id) DO UPDATE SET
                       subject_name = excluded.subject_name,
                       lesson_type = excluded.lesson_type,
                       teacher = excluded.teacher,
                       group_name = excluded.group_name,
                       start_at = excluded.start_at,
                       end_at = excluded.end_at,
                       room = excluded.room,
                       source_url = excluded.source_url,
                       is_online = excluded.is_online,
                       missing_count = 0""",
                rows,
            )
            conn.execute(
                "UPDATE lessons SET missing_count = missing_count + 1 "
                f"WHERE external_id NOT IN ({placeholders})",
                present_ids,
            )
            missing = conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE missing_count > 0"
            ).fetchone()[0]
            stale = [
                row["external_id"]
                for row in conn.execute(
                    "SELECT external_id, start_at, end_at, missing_count FROM lessons"
                ).fetchall()
                # Offsets may differ between rows, so compare datetimes, not strings.
                if datetime.fromisoformat(row["end_at"]) < keep_from
                or (
                    row["missing_count"] >= missing_tolerance
                    and not (
                        datetime.fromisoformat(row["start_at"])
                        <= now
                        <= datetime.fromisoformat(row["end_at"])
                    )
                )
            ]
            conn.executemany(
                "DELETE FROM lessons WHERE external_id = ?", [(item,) for item in stale]
            )
        return missing, len(stale)

    def get_lesson(self, external_id: str) -> Lesson | None:
        return next((x for x in self.list_lessons() if x.external_id == external_id), None)

    def list_lessons(self) -> list[Lesson]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM lessons ORDER BY start_at").fetchall()
        return [
            Lesson(
                external_id=row["external_id"],
                subject_name=row["subject_name"],
                lesson_type=row["lesson_type"],
                teacher=row["teacher"],
                group_name=row["group_name"],
                start_at=datetime.fromisoformat(row["start_at"]),
                end_at=datetime.fromisoformat(row["end_at"]),
                room=row["room"],
                source_url=row["source_url"],
                is_online=bool(row["is_online"]),
            )
            for row in rows
        ]

    def set_rule(self, subject_name: str, mode: RuleMode) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO subject_rules(subject_name, mode) VALUES(?, ?) "
                "ON CONFLICT(subject_name) DO UPDATE SET mode = excluded.mode",
                (subject_name, mode.value),
            )

    def all_rules(self) -> dict[str, RuleMode]:
        with self.connection() as conn:
            rows = conn.execute("SELECT subject_name, mode FROM subject_rules").fetchall()
        return {row["subject_name"]: RuleMode(row["mode"]) for row in rows}

    def all_links(self) -> dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute("SELECT subject_name, url FROM lecture_links").fetchall()
        return {row["subject_name"]: row["url"] for row in rows}

    def all_resolved_links(self) -> dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute("SELECT lesson_id, url FROM resolved_links").fetchall()
        return {row["lesson_id"]: row["url"] for row in rows}

    def get_rule(self, subject_name: str) -> RuleMode:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT mode FROM subject_rules WHERE subject_name = ?", (subject_name,)
            ).fetchone()
        return RuleMode(row["mode"]) if row else RuleMode.ASK

    def set_link(self, subject_name: str, url: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO lecture_links(subject_name, url) VALUES(?, ?) "
                "ON CONFLICT(subject_name) DO UPDATE SET url = excluded.url",
                (subject_name, url),
            )

    def get_link(self, subject_name: str) -> str:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT url FROM lecture_links WHERE subject_name = ?", (subject_name,)
            ).fetchone()
        return row["url"] if row else ""

    def set_sources(self, subject_name: str, urls: list[str]) -> None:
        """Replace the СДО pages that are searched for this subject's webinars."""
        cleaned = [url.strip() for url in urls if url.strip()]
        with self.connection() as conn:
            conn.execute("DELETE FROM lecture_sources WHERE subject_name = ?", (subject_name,))
            conn.executemany(
                "INSERT OR IGNORE INTO lecture_sources(subject_name, url) VALUES(?, ?)",
                [(subject_name, url) for url in cleaned],
            )

    def get_sources(self, subject_name: str) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT url FROM lecture_sources WHERE subject_name = ? ORDER BY rowid",
                (subject_name,),
            ).fetchall()
        return [row["url"] for row in rows]

    def set_resolved_link(self, lesson_id: str, url: str, now: datetime | None = None) -> None:
        """Remember the room found for one lesson. Every lesson gets its own webinar."""
        stamp = (now or datetime.now().astimezone()).isoformat()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO resolved_links(lesson_id, url, resolved_at) VALUES(?, ?, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET url = excluded.url, "
                "resolved_at = excluded.resolved_at",
                (lesson_id, url, stamp),
            )

    def get_resolved_link(self, lesson_id: str) -> str:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT url FROM resolved_links WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()
        return row["url"] if row else ""

    def add_qr_event(
        self, token_hash: str, status: str, lesson_id: str | None = None, message: str | None = None
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO qr_events(lesson_id, token_hash, detected_at, status, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (lesson_id, token_hash, datetime.now().astimezone().isoformat(), status, message),
            )
            return int(cursor.lastrowid)

    def update_qr_event(self, event_id: int, status: str, message: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE qr_events SET status = ?, message = ? WHERE id = ?",
                (status, message, event_id),
            )

    def recent_qr_events(self, limit: int = 50) -> list[QrEvent]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM qr_events ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            QrEvent(
                id=row["id"],
                lesson_id=row["lesson_id"],
                token_hash=row["token_hash"],
                detected_at=datetime.fromisoformat(row["detected_at"]),
                status=row["status"],
                message=row["message"],
            )
            for row in rows
        ]

    def seen_recently(self, token_hash: str, after: datetime) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM qr_events WHERE token_hash = ? AND detected_at >= ? "
                "AND status IN ('detected', 'retrying', 'submitted') LIMIT 1",
                (token_hash, after.isoformat()),
            ).fetchone()
        return row is not None
