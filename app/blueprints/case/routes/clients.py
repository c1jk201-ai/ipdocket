from flask import jsonify, request
from flask_login import login_required
from sqlalchemy import or_

from app.blueprints.case import bp
from app.models.client import Client
from app.services.client.client_party_sync import ensure_clients_synced_from_party
from app.utils.search import sqlalchemy_contains_query


@bp.get("/api/clients/search")
@login_required
def api_clients_search():
    ensure_clients_synced_from_party()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    items = (
        Client.query.filter(
            or_(
                sqlalchemy_contains_query(Client.name, q),
                sqlalchemy_contains_query(Client.email, q),
                sqlalchemy_contains_query(Client.registration_number, q),
            ),
            or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)),
        )
        .order_by(Client.name)
        .limit(20)
        .all()
    )

    return jsonify(
        [
            {
                "id": c.id,
                "name": c.name,
                "email": c.email or "",
                "registration_number": c.registration_number or "",
            }
            for c in items
        ]
    )
