from app.blueprints.billing_invoices.db import get_db, init_db


def _seed_invoice(
    conn,
    *,
    invoice_id: int,
    internal_reference: str = "",
    ipm_case_id: str = "",
    ipm_case_ref: str = "",
) -> None:
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
            total_minor,
            internal_reference,
            ipm_case_id,
            ipm_case_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            1,
            1,
            f"INV-{invoice_id:04d}",
            "2026-03-10",
            "2026-03-31",
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
            internal_reference,
            ipm_case_id,
            ipm_case_ref,
        ),
    )


def _seed_matter(conn, *, matter_id: str, our_ref: str, right_name: str) -> None:
    conn.execute(
        "INSERT INTO matter (matter_id, our_ref, right_name) VALUES (?, ?, ?)",
        (matter_id, our_ref, right_name),
    )


def test_add_invoice_case_link_accepts_multiple_our_refs(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=1)
        _seed_matter(conn, matter_id="MATTER-001", our_ref="25DD0110US", right_name="Text Text Text")
        _seed_matter(conn, matter_id="MATTER-002", our_ref="25DD0111US", right_name="Text Text Text")
        conn.commit()
        conn.close()

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/case-links/add",
        data={"case_ref": "25DD0110US 25DD0111US"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)

    with app.app_context():
        conn = get_db()
        rows = conn.execute(
            """
            SELECT matter_id, our_ref
            FROM external_invoice_case_map
            WHERE external_invoice_id=?
            ORDER BY id
            """,
            (1,),
        ).fetchall()
        conn.close()

    assert [(row["matter_id"], row["our_ref"]) for row in rows] == [
        ("MATTER-001", "25DD0110US"),
        ("MATTER-002", "25DD0111US"),
    ]


def test_view_invoice_renders_multiple_internal_reference_recommendations(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=1, internal_reference="25DD0110US 25DD0111US")
        _seed_matter(conn, matter_id="MATTER-001", our_ref="25DD0110US", right_name="Text Text Text")
        _seed_matter(conn, matter_id="MATTER-002", our_ref="25DD0111US", right_name="Text Text Text")
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/1")

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "Suggested matches from invoice references: 2" in html
    assert "Link 2 suggested matters" in html
    assert "25DD0110US" in html
    assert "25DD0111US" in html
    assert "Text Text Text" in html
    assert "Text Text Text" in html
    assert 'name="case_ref" value="MATTER-001"' in html
    assert 'name="case_ref" value="MATTER-002"' in html


def test_add_invoice_case_link_expands_internal_reference_range(
    admin_client, app, clean_legacy_invoice_db
):
    refs = [f"24TD0{num:03d}US" for num in range(236, 241)]

    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=2)
        for idx, ref in enumerate(refs, start=1):
            _seed_matter(
                conn,
                matter_id=f"MATTER-{idx:03d}",
                our_ref=ref,
                right_name=f"Text {idx}",
            )
        conn.commit()
        conn.close()

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/2/case-links/add",
        data={"case_ref": "24TD0236US-24TD0240US"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)

    with app.app_context():
        conn = get_db()
        rows = conn.execute(
            """
            SELECT matter_id, our_ref
            FROM external_invoice_case_map
            WHERE external_invoice_id=?
            ORDER BY our_ref
            """,
            (2,),
        ).fetchall()
        conn.close()

    assert [(row["matter_id"], row["our_ref"]) for row in rows] == [
        ("MATTER-001", "24TD0236US"),
        ("MATTER-002", "24TD0237US"),
        ("MATTER-003", "24TD0238US"),
        ("MATTER-004", "24TD0239US"),
        ("MATTER-005", "24TD0240US"),
    ]


def test_view_invoice_expands_internal_reference_range_recommendations(
    admin_client, app, clean_legacy_invoice_db
):
    refs = [f"24TD0{num:03d}US" for num in range(236, 241)]

    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=3, internal_reference="24TD0236US-24TD0240US")
        for idx, ref in enumerate(refs, start=1):
            _seed_matter(
                conn,
                matter_id=f"RANGE-{idx:03d}",
                our_ref=ref,
                right_name=f"Text Text {idx}",
            )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/3")

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "Suggested matches from invoice references: 5" in html
    assert "Link 5 suggested matters" in html
    for ref in refs:
        assert ref in html


def test_add_invoice_case_link_expands_shorthand_suffix_refs(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=4)
        _seed_matter(conn, matter_id="SHORT-001", our_ref="26PD0166US", right_name="Text Text 1")
        _seed_matter(conn, matter_id="SHORT-002", our_ref="26PD0167US", right_name="Text Text 2")
        conn.commit()
        conn.close()

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/4/case-links/add",
        data={"case_ref": "26PD0166, 0167US"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)

    with app.app_context():
        conn = get_db()
        rows = conn.execute(
            """
            SELECT matter_id, our_ref
            FROM external_invoice_case_map
            WHERE external_invoice_id=?
            ORDER BY our_ref
            """,
            (4,),
        ).fetchall()
        conn.close()

    assert [(row["matter_id"], row["our_ref"]) for row in rows] == [
        ("SHORT-001", "26PD0166US"),
        ("SHORT-002", "26PD0167US"),
    ]


def test_view_invoice_renders_shorthand_internal_reference_recommendations(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=5, internal_reference="26PD0166, 0167US")
        _seed_matter(conn, matter_id="SHORT-101", our_ref="26PD0166US", right_name="Text Text 1")
        _seed_matter(conn, matter_id="SHORT-102", our_ref="26PD0167US", right_name="Text Text 2")
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/5")

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "Suggested matches from invoice references: 2" in html
    assert "Link 2 suggested matters" in html
    assert "26PD0166US" in html
    assert "26PD0167US" in html
    assert "Text Text 1" in html
    assert "Text Text 2" in html
    assert "Text Text Text Text" not in html


def test_view_invoice_primary_case_snapshot_renders_as_suggestion_not_link(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(
            conn,
            invoice_id=6,
            ipm_case_id="PRIMARY-001",
            ipm_case_ref="26PD0999US",
        )
        _seed_matter(
            conn,
            matter_id="PRIMARY-001",
            our_ref="26PD0999US",
            right_name="Text Text",
        )
        conn.commit()
        before_count = conn.execute(
            "SELECT COUNT(*) AS c FROM external_invoice_case_map WHERE external_invoice_id=?",
            (6,),
        ).fetchone()["c"]
        conn.close()

    assert before_count == 0

    resp = admin_client.get("/accounting/invoice-system/invoices/6")

    assert resp.status_code == 200
    html = resp.data.decode("utf-8", errors="replace")
    assert "No confirmed matter links yet." in html
    assert "Suggested matches from invoice references: 1" in html
    assert 'name="case_ref" value="PRIMARY-001"' in html
    assert 'name="matter_id" value="PRIMARY-001"' not in html
    assert "26PD0999US" in html
    assert "Text Text" in html

    with app.app_context():
        conn = get_db()
        after_count = conn.execute(
            "SELECT COUNT(*) AS c FROM external_invoice_case_map WHERE external_invoice_id=?",
            (6,),
        ).fetchone()["c"]
        conn.close()

    assert after_count == 0


def test_view_invoice_orders_linked_cases_by_our_ref_desc(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=7)
        for matter_id, our_ref, right_name in (
            ("ORDER-246", "26TD0246US", "FFRA Text"),
            ("ORDER-243", "26TD0243US", "FFRA"),
            ("ORDER-245", "26TD0245US", "FFRA Text"),
            ("ORDER-244", "26TD0244US", "FFRA"),
        ):
            _seed_matter(conn, matter_id=matter_id, our_ref=our_ref, right_name=right_name)
        for matter_id, our_ref in (
            ("ORDER-246", "26TD0246US"),
            ("ORDER-243", "26TD0243US"),
            ("ORDER-245", "26TD0245US"),
            ("ORDER-244", "26TD0244US"),
        ):
            conn.execute(
                "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
                (matter_id, our_ref, 7),
            )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/invoices/7")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Matter matching" in html
    assert "(4)" in html
    assert html.index("26TD0246US") < html.index("26TD0245US")
    assert html.index("26TD0245US") < html.index("26TD0244US")
    assert html.index("26TD0244US") < html.index("26TD0243US")


def test_case_matching_links_api_orders_linked_cases_by_our_ref_desc(
    admin_client, app, clean_legacy_invoice_db
):
    with app.app_context():
        init_db()
        conn = get_db()
        _seed_invoice(conn, invoice_id=8)
        for matter_id, our_ref, right_name in (
            ("API-250", "26TD0250US", "THERMAVAULT NP"),
            ("API-247", "26TD0247US", "CNTB"),
            ("API-249", "26TD0249US", "THERMAVAULT NP"),
            ("API-248", "26TD0248US", "CNTB Text"),
        ):
            _seed_matter(conn, matter_id=matter_id, our_ref=our_ref, right_name=right_name)
            conn.execute(
                "INSERT INTO external_invoice_case_map (matter_id, our_ref, external_invoice_id) VALUES (?, ?, ?)",
                (matter_id, our_ref, 8),
            )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/case-matching/linksNewinvoice_id=8")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert [item["our_ref"] for item in payload["items"]] == [
        "26TD0250US",
        "26TD0249US",
        "26TD0248US",
        "26TD0247US",
    ]
