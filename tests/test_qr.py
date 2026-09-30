from datetime import datetime

from mirea_lecture_assistant.database import Database
from mirea_lecture_assistant.qr import QrDeduplicator, validate_qr

TOKEN = "123e4567-e89b-12d3-a456-426614174000"


def test_accepts_mirea_url_and_never_exposes_token_as_hash():
    qr, error = validate_qr(f"https://pulse.mirea.ru/selfapprove?token={TOKEN}")
    assert error is None
    assert qr is not None
    assert qr.token_hash != TOKEN
    assert len(qr.token_hash) == 64


def test_rejects_lookalike_domain():
    qr, error = validate_qr(f"https://pulse.mirea.ru.evil.example/?token={TOKEN}")
    assert qr is None
    assert "не QR-код" in error


def test_rejects_non_attendance_qr_and_malformed_token():
    qr, _ = validate_qr("https://example.org/course-materials")
    assert qr is None
    qr, error = validate_qr("https://pulse.mirea.ru/selfapprove?token=not-a-uuid")
    assert qr is None
    assert "неверный формат" in error


def test_automatic_scan_rejects_bare_uuid_but_manual_path_can_allow_it():
    qr, error = validate_qr(TOKEN)
    assert qr is None
    assert "только URL" in error
    qr, error = validate_qr(TOKEN, allow_bare_token=True)
    assert error is None
    assert qr is not None


def test_deduplicates_saved_fingerprint(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    qr, _ = validate_qr(TOKEN, allow_bare_token=True)
    db.add_qr_event(qr.token_hash, "detected")
    assert QrDeduplicator(db).is_duplicate(qr.token_hash, datetime.now().astimezone())


def test_failed_submission_does_not_block_new_scan(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    qr, _ = validate_qr(TOKEN, allow_bare_token=True)
    db.add_qr_event(qr.token_hash, "failed", message="Сессия истекла")
    assert not QrDeduplicator(db).is_duplicate(qr.token_hash, datetime.now().astimezone())


def test_retry_in_progress_is_deduplicated(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    qr, _ = validate_qr(TOKEN, allow_bare_token=True)
    db.add_qr_event(qr.token_hash, "retrying", message="Повтор через 5 секунд")
    assert QrDeduplicator(db).is_duplicate(qr.token_hash, datetime.now().astimezone())


def test_an_address_with_a_login_part_is_never_trusted():
    from mirea_lecture_assistant.qr import validate_qr

    token = "123e4567-e89b-12d3-a456-426614174000"
    for raw in (
        f"https://evil.example\\@pulse.mirea.ru/selfapprove?token={token}",
        f"https://pulse.mirea.ru@evil.example/selfapprove?token={token}",
    ):
        qr, _error = validate_qr(raw)
        assert qr is None
