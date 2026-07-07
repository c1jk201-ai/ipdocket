from app.blueprints.billing_invoices.db import get_db, init_db
from app.blueprints.billing_invoices.repos.invoice_case_repo import (
    link_case_to_invoice,
    unlink_case_from_invoice,
)


def _seed_invoice_case_repo_basics(conn, *, invoice_id: int = 901) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO business_profile (id, name, currency, vat_rate, next_invoice_no) VALUES (?, ?, ?, ?, ?)",
        (1, "BP1", "USD", 10.0, 1),
    )
    conn.execute("INSERT OR REPLACE INTO clients (id, name) VALUES (?, ?)", (1, "Client A"))
    conn.execute(
        """
        INSERT INTO invoices (
            id,
            client_id,
            business_profile_id,
            number,
            issue_date,
            due_date,
            status,
            billing_status,
            payment_status,
            payment_verified,
            currency,
            subtotal,
            tax,
            total,
            subtotal_minor,
            tax_minor,
            total_minor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            1,
            1,
            f"INV-{invoice_id}",
            "2026-04-14",
            "2026-05-14",
            "sent",
            "sent",
            "unpaid",
            0,
            "USD",
            1000,
            100,
            1100,
            1000,
            100,
            1100,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
        ("MATTER-REPO-1", "26DD0106US", "Repo test matter"),
    )
    conn.commit()


def test_invoice_case_repo_link_writes_legacy_link_snapshot(app, clean_legacy_invoice_db):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM external_invoice_case_link")
        _seed_invoice_case_repo_basics(conn, invoice_id=901)

        assert link_case_to_invoice(conn, invoice_id=901, matter_id="MATTER-REPO-1") is True

        map_row = conn.execute(
            """
            SELECT matter_id, our_ref, is_deleted
              FROM external_invoice_case_map
             WHERE matter_id=? AND external_invoice_id=?
            """,
            ("MATTER-REPO-1", 901),
        ).fetchone()
        link_row = conn.execute(
            """
            SELECT matter_id, our_ref, external_invoice_number, is_deleted
              FROM external_invoice_case_link
             WHERE external_invoice_id=?
            """,
            (901,),
        ).fetchone()
        invoice_row = conn.execute(
            "SELECT ipm_case_id, ipm_case_ref FROM invoices WHERE id=?",
            (901,),
        ).fetchone()
        conn.close()

    assert map_row["matter_id"] == "MATTER-REPO-1"
    assert map_row["our_ref"] == "26DD0106US"
    assert str(map_row["is_deleted"]).lower() in {"0", "false"}
    assert link_row["matter_id"] == "MATTER-REPO-1"
    assert link_row["our_ref"] == "26DD0106US"
    assert link_row["external_invoice_number"] == "INV-901"
    assert str(link_row["is_deleted"]).lower() in {"0", "false"}
    assert invoice_row["ipm_case_id"] == "MATTER-REPO-1"
    assert invoice_row["ipm_case_ref"] == "26DD0106US"


def test_invoice_case_repo_unlink_soft_deletes_legacy_link_snapshot(app, clean_legacy_invoice_db):
    with app.app_context():
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM external_invoice_case_link")
        _seed_invoice_case_repo_basics(conn, invoice_id=902)
        link_case_to_invoice(conn, invoice_id=902, matter_id="MATTER-REPO-1")

        unlink_case_from_invoice(conn, invoice_id=902, matter_id="MATTER-REPO-1")

        active_map_count = conn.execute(
            """
            SELECT COUNT(*) AS c
              FROM external_invoice_case_map
             WHERE matter_id=? AND external_invoice_id=?
            """,
            ("MATTER-REPO-1", 902),
        ).fetchone()["c"]
        link_row = conn.execute(
            """
            SELECT matter_id, is_deleted, deleted_at
              FROM external_invoice_case_link
             WHERE external_invoice_id=?
            """,
            (902,),
        ).fetchone()
        invoice_row = conn.execute(
            "SELECT ipm_case_id, ipm_case_ref FROM invoices WHERE id=?",
            (902,),
        ).fetchone()
        conn.close()

    assert active_map_count == 0
    assert link_row["matter_id"] == "MATTER-REPO-1"
    assert str(link_row["is_deleted"]).lower() in {"1", "true"}
    assert link_row["deleted_at"]
    assert invoice_row["ipm_case_id"] is None
    assert invoice_row["ipm_case_ref"] is None
