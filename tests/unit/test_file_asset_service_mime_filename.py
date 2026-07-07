import io

from werkzeug.datastructures import FileStorage


def test_stage_upload_decodes_mime_encoded_filename(app, db_session, tmp_path):
    from app.services.storage.file_asset_service import FileAssetService

    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    svc = FileAssetService(upload_root=str(tmp_path))

    staged = svc.stage_upload(
        FileStorage(
            stream=io.BytesIO(b"pdf-bytes"),
            filename="=?UTF-8?B?MjZQRDAxNjFVU19hcHBsaWNhdGlvbl9ub3RpY2UucGRm?=",
            content_type="application/pdf",
        ),
        subdir="tests",
    )

    assert staged.original_name == "26PD0161US_application_notice.pdf"


def test_stage_bytes_decodes_mime_encoded_filename(app, db_session, tmp_path):
    from app.services.storage.file_asset_service import FileAssetService

    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    svc = FileAssetService(upload_root=str(tmp_path))

    staged = svc.stage_bytes(
        b"pdf-bytes",
        filename="=?UTF-8?B?MjZQRDAxNThVU19wYXltZW50X3JlY2VpcHQucGRm?=",
        subdir="tests",
        mime_type="application/pdf",
    )

    assert staged.original_name == "26PD0158US_payment_receipt.pdf"
