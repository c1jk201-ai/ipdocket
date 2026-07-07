from app.services.mail.email_ingestion import _parse_eml


def test_utf8_subject_decoding():
    subject = "Test Subject"
    raw = b"Subject: " + subject.encode("utf-8") + b"\r\n\r\n"
    meta, body, html, attachments = _parse_eml(raw)
    assert meta["subject"] == subject


def test_attachment_filename_decoding_from_mime_header():
    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=frontier\r\n"
        b"\r\n"
        b"--frontier\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"body\r\n"
        b"--frontier\r\n"
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="=?UTF-8?B?MjZQRDAxNjFVU19UZXh0LnBkZg==?="\r\n'
        b"\r\n"
        b"cGRm\r\n"
        b"--frontier--\r\n"
    )

    _meta, _body, _html, attachments = _parse_eml(raw)

    assert attachments[0]["filename"] == "26PD0161US_Text.pdf"


def test_history_letter_preview_decodes_encoded_subject_and_attachment(app):
    from app.blueprints.case.views.history_letter import _parse_eml_bytes

    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Subject: =?UTF-8?B?VGVzdCBTdWJqZWN0?=\r\n"
        b"From: =?UTF-8?B?VGVzdCBTZW5kZXI=?= <sender@example.com>\r\n"
        b"Content-Type: multipart/mixed; boundary=frontier\r\n"
        b"\r\n"
        b"--frontier\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"body\r\n"
        b"--frontier\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?UTF-8?B?MjZQRDAxNjFVU19UZXh0LnBkZg==?="\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"cGRm\r\n"
        b"--frontier--\r\n"
    )

    with app.app_context():
        parsed = _parse_eml_bytes(raw)

    assert parsed["subject"] == "Test Subject"
    assert "sender@example.com" in parsed["from_addr"]
    assert "=?" not in parsed["from_addr"]
    assert parsed["attachments"][0]["filename"] == "26PD0161US_Text.pdf"
