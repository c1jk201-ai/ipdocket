import io

from app.services.uploads.upload_validation import filter_upload_files


class DummyUpload:
    def __init__(self, data: bytes, filename: str):
        self.filename = filename
        self.stream = io.BytesIO(data)


def test_filter_upload_files_accepts_pdf_with_bom_and_whitespace():
    data = b"\xef\xbb\xbf  \n%PDF-1.7\n%..." + b"A" * 10
    f = DummyUpload(data, "scan.pdf")
    valid, rejected = filter_upload_files([f], {".pdf"})
    assert f in valid
    assert rejected == []


def test_filter_upload_files_rejects_non_pdf():
    data = b"NOTPDF"
    f = DummyUpload(data, "fake.pdf")
    valid, rejected = filter_upload_files([f], {".pdf"})
    assert valid == []
    assert rejected == ["fake.pdf"]
