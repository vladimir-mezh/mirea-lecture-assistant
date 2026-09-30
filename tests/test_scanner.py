from io import BytesIO

import qrcode

from mirea_lecture_assistant.qr import ScreenScanner


def test_decodes_qr_from_png():
    payload = "https://pulse.mirea.ru/selfapprove?token=123e4567-e89b-12d3-a456-426614174000"
    image = qrcode.make(payload)
    stream = BytesIO()
    image.save(stream, format="PNG")

    batch = ScreenScanner().decode_png(stream.getvalue())
    assert batch.decoded == (payload,)
    assert not batch.unreadable_qr
