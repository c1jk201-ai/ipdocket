from __future__ import annotations

import sys
import types
from email import policy
from email.parser import BytesParser


def test_extract_msg_attachment_filename_safe_falls_back_on_decode_error(app):
    from app.blueprints.case.views.file_assets import _extract_msg_attachment_filename_safe

    class _Att:
        @property
        def longFilename(self):
            raise UnicodeDecodeError("utf-16", b"\xb7", 0, 1, "incomplete multibyte sequence")

        @property
        def shortFilename(self):
            return None

    with app.app_context():
        out = _extract_msg_attachment_filename_safe(
            _Att(),
            index=3,
            file_asset_id="test_file_asset_id",
        )

    assert out == "attachment_3"


def test_extract_msg_attachment_filename_safe_decodes_mime_encoded_name(app):
    from app.blueprints.case.views.file_assets import _extract_msg_attachment_filename_safe

    class _Att:
        @property
        def longFilename(self):
            return "=?UTF-8?B?MjZQRDAxNjFLUl/stpzsm5DrsojtmLjthrXsp4DshJwucGRm?="

        @property
        def shortFilename(self):
            return None

    with app.app_context():
        out = _extract_msg_attachment_filename_safe(
            _Att(),
            index=1,
            file_asset_id="test_file_asset_id",
        )

    assert out == "26PD0161KR_출원번호통지서.pdf"


def test_extract_msg_body_safe_falls_back_to_text_when_html_decode_fails(app):
    from app.blueprints.case.views.file_assets import _extract_msg_body_safe

    class _Msg:
        @property
        def htmlBody(self):
            raise UnicodeDecodeError("utf-16", b"\xb7", 0, 1, "incomplete multibyte sequence")

        @property
        def body(self):
            return "plain text body"

    with app.app_context():
        body_html, body_text = _extract_msg_body_safe(_Msg(), file_asset_id="test_file_asset_id")

    assert body_html is None
    assert body_text == "plain text body"


def test_extract_msg_body_safe_decodes_html_bytes_with_replace(app):
    from app.blueprints.case.views.file_assets import _extract_msg_body_safe

    class _Msg:
        @property
        def htmlBody(self):
            # Invalid UTF-8 sequence should not raise here because helper uses errors='replace'
            return b"<p>hello \xff world</p>"

        @property
        def body(self):
            raise RuntimeError("no text body")

    with app.app_context():
        body_html, body_text = _extract_msg_body_safe(_Msg(), file_asset_id="test_file_asset_id")

    assert body_html is not None
    assert "hello" in body_html
    assert body_text is None


def test_history_parse_msg_bytes_tolerates_html_and_attachment_decode_errors(app, monkeypatch):
    import app.blueprints.case.views.history_letter as history_letter

    class _Att:
        @property
        def longFilename(self):
            raise UnicodeDecodeError("utf-16", b"\xb7", 0, 1, "incomplete multibyte sequence")

        @property
        def shortFilename(self):
            return None

    class _Msg:
        subject = "subject"
        sender = "from@example.com"
        to = "to@example.com"
        cc = ""
        date = None
        attachments = [_Att()]

        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def htmlBody(self):
            raise UnicodeDecodeError("utf-16", b"\xb7", 0, 1, "incomplete multibyte sequence")

        @property
        def body(self):
            return "plain body"

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "extract_msg", types.SimpleNamespace(Message=_Msg))

    with app.app_context():
        parsed, err = history_letter._parse_msg_bytes(b"dummy msg bytes")

    assert err is None
    assert parsed is not None
    assert parsed["body_html"] is None
    assert parsed["body_text"] == "plain body"
    assert parsed["attachments"][0]["filename"] == "attachment_1"


def test_history_parse_msg_bytes_ignores_close_error(app, monkeypatch):
    import app.blueprints.case.views.history_letter as history_letter

    class _Msg:
        subject = "subject"
        sender = "from@example.com"
        to = "to@example.com"
        cc = ""
        date = None
        attachments = []

        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def htmlBody(self):
            return None

        @property
        def body(self):
            return "body"

        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setitem(sys.modules, "extract_msg", types.SimpleNamespace(Message=_Msg))

    with app.app_context():
        parsed, err = history_letter._parse_msg_bytes(b"dummy msg bytes")

    assert err is None
    assert parsed is not None
    assert parsed["body_text"] == "body"


def test_extract_eml_body_safe_falls_back_on_bad_plain_charset(app):
    from app.blueprints.case.views.file_assets import _extract_eml_body_safe

    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: test\r\n"
        b"Content-Type: text/plain; charset=utf-16\r\n"
        b"\r\n"
        b"\xb7\xff\xaa"
    )
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    with app.app_context():
        body_html, body_text = _extract_eml_body_safe(msg)

    assert body_html is None
    assert isinstance(body_text, str)
    assert len(body_text) > 0


def test_extract_eml_body_safe_falls_back_on_bad_html_charset(app):
    from app.blueprints.case.views.file_assets import _extract_eml_body_safe

    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: test\r\n"
        b"Content-Type: multipart/alternative; boundary=abc\r\n"
        b"\r\n"
        b"--abc\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"plain body\r\n"
        b"--abc\r\n"
        b"Content-Type: text/html; charset=utf-16\r\n"
        b"\r\n"
        b"<p>\xb7\xff\xaa</p>\r\n"
        b"--abc--\r\n"
    )
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    with app.app_context():
        body_html, body_text = _extract_eml_body_safe(msg)

    assert isinstance(body_text, str)
    assert isinstance(body_html, str)
    assert "<p>" in body_html
