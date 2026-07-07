from app.utils.mime_headers import (
    contains_mime_encoded_words,
    decode_mime_encoded_words,
    normalize_uploaded_filename,
)


def test_decode_mime_encoded_words_decodes_single_filename() -> None:
    raw = "=?UTF-8?B?MjZQRDAxNjFLUl/stpzsm5DrsojtmLjthrXsp4DshJwucGRm?="
    assert decode_mime_encoded_words(raw) == "26PD0161KR_출원번호통지서.pdf"


def test_decode_mime_encoded_words_decodes_folded_filename() -> None:
    raw = "=?UTF-8?B?MjZQRDAxNThLUl/rgqnrtoDtmZXsnbjspp0=?=\r\n\t=?UTF-8?B?LnBkZg==?="
    assert decode_mime_encoded_words(raw) == "26PD0158KR_납부확인증.pdf"


def test_decode_mime_encoded_words_preserves_plain_text_around_fragment() -> None:
    raw = "Text: =?UTF-8?B?MjZQRDAxNjFLUl/stpzsm5DrsojtmLjthrXsp4DshJwucGRm?= Text 1Text"
    assert decode_mime_encoded_words(raw) == "Text: 26PD0161KR_출원번호통지서.pdf Text 1Text"


def test_normalize_uploaded_filename_uses_default_for_blank() -> None:
    assert normalize_uploaded_filename("  ", default="fallback.bin") == "fallback.bin"


def test_contains_mime_encoded_words_matches_encoded_fragment() -> None:
    assert contains_mime_encoded_words("x =?UTF-8?B?dGVzdA==?= y") is True
    assert contains_mime_encoded_words("plain filename.pdf") is False
