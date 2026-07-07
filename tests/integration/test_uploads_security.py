from __future__ import annotations


def _login(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def _create_accounting_user(db_session):
    from app.models.user import User

    user = User(username="accounting_user", email="accounting@example.com", role="accounting")
    db_session.add(user)
    db_session.commit()
    return user


def test_uploads_requires_auth(client):
    response = client.get("/uploads/does-not-exist.txt")
    assert response.status_code in (302, 401, 403)


def test_uploads_forbidden_for_non_privileged(limited_client):
    response = limited_client.get("/uploads/does-not-exist.txt")
    assert response.status_code == 403


def test_uploads_allows_admin_access(admin_client):
    response = admin_client.get("/uploads/does-not-exist.txt")
    assert response.status_code in (200, 404)


def test_uploads_forbidden_for_invoice_manager_untracked_file(client, db_session):
    user = _create_accounting_user(db_session)
    _login(client, user.id)

    response = client.get("/uploads/secret.txt")
    assert response.status_code == 403


def test_uploads_hides_orphan_file_assets_from_invoice_manager(client, db_session):
    from app.models.assets import FileAsset

    user = _create_accounting_user(db_session)
    db_session.add(
        FileAsset(
            file_asset_id="orphan-download",
            file_path="orphan.pdf",
            original_name="orphan.pdf",
            mime_type="application/pdf",
        )
    )
    db_session.commit()
    _login(client, user.id)

    response = client.get("/uploads/orphan.pdf")
    assert response.status_code == 404


def test_preview_hides_orphan_file_assets_from_invoice_manager(client, db_session):
    from app.models.assets import FileAsset

    user = _create_accounting_user(db_session)
    db_session.add(
        FileAsset(
            file_asset_id="orphan-preview",
            file_path="orphan-preview.pdf",
            original_name="orphan-preview.pdf",
            mime_type="application/pdf",
        )
    )
    db_session.commit()
    _login(client, user.id)

    response = client.get("/files/orphan-preview/preview")
    assert response.status_code == 404


def test_preview_rejects_svg_mime_even_for_privileged_user(admin_client, db_session):
    from app.models.assets import FileAsset, MatterFileAsset

    db_session.add(
        FileAsset(
            file_asset_id="svg-preview",
            file_path="svg-preview.svg",
            original_name="svg-preview.svg",
            mime_type="image/svg+xml",
        )
    )
    db_session.add(
        MatterFileAsset(
            matter_file_id="mf-svg-preview",
            matter_id="MATTER-SVG",
            file_asset_id="svg-preview",
            role="ATTACHMENT",
        )
    )
    db_session.commit()

    response = admin_client.get("/files/svg-preview/preview")
    assert response.status_code == 415


def test_preview_404_for_deleted_file_asset(admin_client, db_session):
    from app.models.assets import FileAsset, MatterFileAsset

    db_session.add(
        FileAsset(
            file_asset_id="deleted-preview",
            file_path="deleted-preview.pdf",
            original_name="deleted-preview.pdf",
            mime_type="application/pdf",
            is_deleted=True,
        )
    )
    db_session.add(
        MatterFileAsset(
            matter_file_id="mf-deleted-preview",
            matter_id="MATTER-DELETED",
            file_asset_id="deleted-preview",
            role="ATTACHMENT",
        )
    )
    db_session.commit()

    response = admin_client.get("/files/deleted-preview/preview")
    assert response.status_code == 404
