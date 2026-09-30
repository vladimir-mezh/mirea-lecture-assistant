from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .domain import Lesson, QrEvent, RuleMode

# How many refreshes in a row may omit a lesson before it is treated as cancelled.
# Refreshes run once a minute, so this is also roughly the tolerated outage in
# minutes: three used to drop the running pair after a three-minute Pulse hiccup.
MISSING_TOLERANCE = 15
# Lessons of the next day survive an hour-long outage.
NEAR_FUTURE = timedelta(days=1)
NEAR_FUTURE_TOLERANCE = 60

log = logging.getLogger(__name__)

# Tables worth carrying over from a damaged file, and the columns whose values
# must still make sense for a row to be kept.
RECOVERED_TABLES = (
    "settings",
    "subject_rules",
    "lecture_links",
    "lecture_sources",
    "resolved_links",
    "lessons",
    "qr_events",
)
DATE_COLUMNS = {
    "lessons": ("start_at", "end_at"),
    "qr_events": ("detected_at",),
    "resolved_links": ("resolved_at",),
}
# Errors of a busy or unreachable file: nothing in it is damaged.
NOT_DAMAGE = ("locked", "busy", "unable to open", "readonly", "disk i/o", "full")


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _is_damage(error: sqlite3.DatabaseError) -> bool:
    return not any(part in str(error).lower() for part in NOT_DAMAGE)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection per OS thread: opening a new one per query cost a file open,
        # a PRAGMA and a commit on every scan frame and table refresh. Keyed by the
        # thread id, not threading.local: Qt pool threads start every task with a
        # fresh Python thread state, and each task leaked a connection.
        self._connections: dict[int, sqlite3.Connection] = {}
        self._connections_lock = threading.Lock()
        # Set when a damaged file was replaced at start: what was saved and where.
        self.recovery: dict | None = None
        try:
            self.migrate()
            self._check_integrity()
        except sqlite3.DatabaseError as error:
            if not _is_damage(error):
                raise
            self.recovery = self._recover(error)

    def _check_integrity(self) -> None:
        with self.connection() as conn:
            verdict = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if verdict != ["ok"]:
            raise sqlite3.DatabaseError("quick_check: " + "; ".join(map(str, verdict[:3])))

    def _recover(self, error: sqlite3.DatabaseError) -> dict:
        """Put a damaged file aside and carry every readable, sensible row over.

        A damaged file used to stop the app at start (or on the history page),
        and only a manual `.recover` with the sqlite3 tool brought the settings
        back. The damaged files stay in a backup folder next to the new one.
        """
        log.error("database_damaged error=%s", error)
        self.close()
        backup = self.path.parent / f"db-backup-{datetime.now().astimezone():%Y%m%d-%H%M%S}"
        backup.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source = self.path.with_name(self.path.name + suffix)
            if source.exists():
                os.replace(source, backup / source.name)
        salvaged = self._salvage(backup / self.path.name)
        daily = sorted(self.backup_dir.glob("assistant-*.sqlite3"))
        if daily:
            # Rows the damaged file lost may still be in the last daily copy; the
            # newer rows from the damaged file are inserted first and win.
            for table, rows in self._salvage(daily[-1]).items():
                salvaged.setdefault(table, []).extend(rows)
        self.migrate()
        restored = skipped = 0
        with self.connection() as conn:
            for table, rows in salvaged.items():
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                for row in rows:
                    values = {key: row[key] for key in row if key in columns}
                    if not self._row_is_sensible(table, values):
                        skipped += 1
                        continue
                    names = ", ".join(values)
                    marks = ", ".join("?" * len(values))
                    try:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {table}({names}) VALUES ({marks})",
                            tuple(values.values()),
                        )
                        restored += 1
                    except sqlite3.Error:
                        skipped += 1
        report = {"backup": str(backup), "restored": restored, "skipped": skipped}
        log.warning(
            "database_recovered restored=%s skipped=%s backup=%s", restored, skipped, backup
        )
        return report

    @property
    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    def backup(self, keep: int = 5) -> Path | None:
        """Today's copy of the database, the last ``keep`` days kept."""
        folder = self.backup_dir
        target = folder / f"assistant-{datetime.now().astimezone():%Y%m%d}.sqlite3"
        if target.exists():
            return target
        try:
            folder.mkdir(parents=True, exist_ok=True)
            destination = sqlite3.connect(target)
            try:
                self._thread_connection().backup(destination)
            finally:
                destination.close()
        except (OSError, sqlite3.Error):
            log.warning("database_backup_failed", exc_info=True)
            target.unlink(missing_ok=True)
            return None
        for old in sorted(folder.glob("assistant-*.sqlite3"))[:-keep]:
            old.unlink(missing_ok=True)
        return target

    @staticmethod
    def _salvage(damaged: Path) -> dict[str, list[dict]]:
        """Every row still readable, from the front and from the back of each table."""
        salvaged: dict[str, list[dict]] = {}
        try:
            conn = sqlite3.connect(damaged, timeout=1)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return salvaged
        try:
            for table in RECOVERED_TABLES:
                found: dict[tuple, dict] = {}
                for order in ("ASC", "DESC"):
                    # A bad page ends a scan; reading from the other end gets past it.
                    try:
                        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid {order}")
                        for row in cursor:
                            item = dict(row)
                            found.setdefault(tuple(item.items()), item)
                    except sqlite3.Error:
                        continue
                salvaged[table] = list(found.values())
        finally:
            conn.close()
        return salvaged

    @staticmethod
    def _row_is_sensible(table: str, values: dict) -> bool:
        if not values:
            return False
        for column in DATE_COLUMNS.get(table, ()):
            if column in values and _parse_time(values[column]) is None:
                return False
        if table == "settings":
            try:
                json.loads(values.get("value"))
            except (TypeError, ValueError):
                return False
        return True

    def _thread_connection(self) -> sqlite3.Connection:
        ident = threading.get_ident()
        conn = self._connections.get(ident)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            with self._connections_lock:
                self._connections[ident] = conn
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
            connections, self._connections = self._connections, {}
        for conn in connections.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass

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
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            # One unreadable value must not take the whole window down with it.
            log.warning("setting_unreadable key=%s", key)
            return default

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
                # A row with an unreadable time is of no use and is dropped too.
                if _parse_time(row["end_at"]) is None
                or _parse_time(row["start_at"]) is None
                or datetime.fromisoformat(row["end_at"]) < keep_from
                or row["missing_count"] >= self._tolerance_for(row, now, missing_tolerance)
            ]
            conn.executemany(
                "DELETE FROM lessons WHERE external_id = ?", [(item,) for item in stale]
            )
        return missing, len(stale)

    @staticmethod
    def _tolerance_for(row, now: datetime, tolerance: int) -> int:
        """How many misses a lesson survives before it counts as cancelled.

        A running pair is never dropped, and one within the next day needs an
        hour of misses: a long Pulse outage used to delete the next pair before
        it began. A real cancellation still disappears, only later.
        """
        start = datetime.fromisoformat(row["start_at"])
        end = datetime.fromisoformat(row["end_at"])
        if start <= now <= end:
            return 1_000_000
        if now < start <= now + NEAR_FUTURE:
            return max(tolerance, NEAR_FUTURE_TOLERANCE)
        return tolerance

    def get_lesson(self, external_id: str) -> Lesson | None:
        return next((x for x in self.list_lessons() if x.external_id == external_id), None)

    def list_lessons(self) -> list[Lesson]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM lessons ORDER BY start_at").fetchall()
        lessons = []
        for row in rows:
            start, end = _parse_time(row["start_at"]), _parse_time(row["end_at"])
            if start is None or end is None:
                continue  # a damaged row; the next schedule refresh replaces it
            lessons.append(
                Lesson(
                    external_id=row["external_id"],
                    subject_name=row["subject_name"],
                    lesson_type=row["lesson_type"],
                    teacher=row["teacher"],
                    group_name=row["group_name"],
                    start_at=start,
                    end_at=end,
                    room=row["room"],
                    source_url=row["source_url"],
                    is_online=bool(row["is_online"]),
                )
            )
        return lessons

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

    def forget_resolved_link(self, lesson_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM resolved_links WHERE lesson_id = ?", (lesson_id,))

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
                detected_at=detected,
                status=row["status"],
                message=row["message"],
            )
            for row in rows
            # Damaged dates used to crash the history page and with it the start.
            if (detected := _parse_time(row["detected_at"])) is not None
        ]

    def seen_recently(self, token_hash: str, after: datetime) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM qr_events WHERE token_hash = ? AND detected_at >= ? "
                "AND status IN ('detected', 'retrying', 'submitted', 'rejected') LIMIT 1",
                (token_hash, after.isoformat()),
            ).fetchone()
        return row is not None
