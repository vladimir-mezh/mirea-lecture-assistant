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


def test_screen_scan_reads_a_qr_shown_on_a_monitor(monkeypatch):
    """The monitor grab is decoded straight from its RGB bytes, without numpy."""
    import sys
    import types

    payload = "https://pulse.mirea.ru/selfapprove?token=123e4567-e89b-12d3-a456-426614174000"
    picture = qrcode.make(payload).get_image().convert("RGB")

    class Shot:
        size = picture.size
        rgb = picture.tobytes()

    class Capture:
        monitors = ({}, {"left": 0, "top": 0})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def grab(self, _monitor):
            return Shot()

    monkeypatch.setitem(sys.modules, "mss", types.SimpleNamespace(mss=Capture))

    assert ScreenScanner().scan_once().decoded == (payload,)
