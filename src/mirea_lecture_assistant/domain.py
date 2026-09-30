from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RuleMode(StrEnum):
    AUTO = "AUTO"
    ASK = "ASK"
    IGNORE = "IGNORE"


class SessionState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Lesson:
    external_id: str
    subject_name: str
    lesson_type: str
    start_at: datetime
    end_at: datetime
    teacher: str | None = None
    group_name: str = ""
    room: str | None = None
    source_url: str | None = None
    is_online: bool = False


@dataclass(slots=True)
class QrEvent:
    id: int
    lesson_id: str | None
    token_hash: str
    detected_at: datetime
    status: str
    message: str | None = None


@dataclass(slots=True)
class PendingAttendance:
    raw_data: str
    lesson_id: str | None
    lecture_url: str | None
    detected_at: datetime
