from app.services.files.file_classification import is_previewable


def test_svg_mime_is_not_previewable() -> None:
    assert is_previewable("evil.svg", "image/svg+xml") is False


def test_safe_image_mime_is_previewable() -> None:
    assert is_previewable("photo.png", "image/png") is True
