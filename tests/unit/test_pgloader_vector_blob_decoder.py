from __future__ import annotations


def test_decode_pgloader_vector_blob_basic() -> None:
    from app.blueprints.case.views.file_assets import _maybe_decode_pgloader_vector_blob

    decoded, ok = _maybe_decode_pgloader_vector_blob(b"#(65 66 67)")
    assert ok is True
    assert decoded == b"ABC"


def test_decode_pgloader_vector_blob_with_whitespace() -> None:
    from app.blueprints.case.views.file_assets import (
        _OLE_SIGNATURE,
        _maybe_decode_pgloader_vector_blob,
    )

    decoded, ok = _maybe_decode_pgloader_vector_blob(b" \n#(208 207 17 224 161 177 26 225)\n")
    assert ok is True
    assert decoded[:8] == _OLE_SIGNATURE


def test_decode_pgloader_vector_blob_rejects_out_of_range() -> None:
    from app.blueprints.case.views.file_assets import _maybe_decode_pgloader_vector_blob

    raw = b"#(256 1 2)"
    decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
    assert ok is False
    assert decoded == raw


def _make_ole_header(*, num_fat_sectors: int) -> bytes:
    from app.blueprints.case.views.file_assets import _OLE_SIGNATURE

    hdr = bytearray(512)
    hdr[:8] = _OLE_SIGNATURE
    hdr[30:32] = (9).to_bytes(2, "little")  # sector_shift=9 (512 bytes)
    hdr[44:48] = int(num_fat_sectors).to_bytes(4, "little")
    hdr[48:52] = (1).to_bytes(4, "little")  # first_dir_sector
    hdr[60:64] = (2).to_bytes(4, "little")  # first_mini_fat
    hdr[64:68] = (2).to_bytes(4, "little")  # num_mini_fat
    return bytes(hdr)


def test_ole_header_suggests_truncation() -> None:
    from app.blueprints.case.views.file_assets import _ole_header_suggests_truncation

    # 4096 bytes total => only 7 sectors available for 512-byte sector_size.
    data = _make_ole_header(num_fat_sectors=9) + (b"\x00" * (4096 - 512))
    assert _ole_header_suggests_truncation(data) is True


def test_ole_header_does_not_suggest_truncation_when_plausible() -> None:
    from app.blueprints.case.views.file_assets import _ole_header_suggests_truncation

    data = _make_ole_header(num_fat_sectors=1) + (b"\x00" * (4096 - 512))
    assert _ole_header_suggests_truncation(data) is False
