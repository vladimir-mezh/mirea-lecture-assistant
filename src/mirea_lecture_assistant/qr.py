from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .database import Database

ALLOWED_DOMAINS = {
    "pulse.mirea.ru",
    "attendance-app.mirea.ru",
    "attendance.mirea.ru",
    "att.mirea.ru",
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ValidatedQr:
    raw_data: str
    token_hash: str


@dataclass(frozen=True, slots=True)
class ScanBatch:
    decoded: tuple[str, ...]
    unreadable_qr: bool = False


def validate_qr(
    raw_data: str, *, allow_bare_token: bool = False
) -> tuple[ValidatedQr | None, str | None]:
    value = raw_data.strip()
    token: str | None = None
    if UUID_RE.fullmatch(value):
        if not allow_bare_token:
            return None, "Автоматически принимаются только URL посещаемости МИРЭА"
        token = value
    else:
        candidate = value
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate.lstrip("/")
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return None, "Неверный формат QR-кода"
        if (parsed.hostname or "").lower() not in ALLOWED_DOMAINS:
            return None, "Это не QR-код посещаемости МИРЭА"
        token = parse_qs(parsed.query).get("token", [None])[0]
    if not token:
        return None, "QR-код не содержит токен посещаемости"
    if not UUID_RE.fullmatch(token):
        return None, "Токен посещаемости имеет неверный формат"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return ValidatedQr(raw_data=value, token_hash=digest), None


class QrDeduplicator:
    def __init__(self, database: Database, ttl_minutes: int = 30):
        self.database = database
        self.ttl = timedelta(minutes=ttl_minutes)

    def is_duplicate(self, token_hash: str, now: datetime | None = None) -> bool:
        now = now or datetime.now().astimezone()
        return self.database.seen_recently(token_hash, now - self.ttl)


class ScreenScanner:
    """Cross-platform whole-desktop QR scanner with optional dependencies."""

    @staticmethod
    def _decode_image(image) -> ScanBatch:
        import zxingcpp

        decoded: list[str] = []
        unreadable = False
        barcodes = zxingcpp.read_barcodes(
            image,
            formats=zxingcpp.BarcodeFormat.QRCode,
            return_errors=True,
        )
        for barcode in barcodes:
            text = barcode.text.strip()
            if barcode.valid and text:
                if text not in decoded:
                    decoded.append(text)
            else:
                unreadable = True
        return ScanBatch(tuple(decoded), unreadable)

    def scan_once(self) -> ScanBatch:
        import mss
        import numpy as np

        results: list[str] = []
        unreadable = False
        with mss.mss() as capture:
            for monitor in capture.monitors[1:]:
                image = np.asarray(capture.grab(monitor))[:, :, :3]
                batch = self._decode_image(image)
                unreadable = unreadable or batch.unreadable_qr
                for text in batch.decoded:
                    if text not in results:
                        results.append(text)
        return ScanBatch(tuple(results), unreadable)

    def decode_png(self, png: bytes) -> ScanBatch:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        return self._decode_image(image)
