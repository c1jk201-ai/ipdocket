from __future__ import annotations

from types import SimpleNamespace


def test_drain_annuity_queue_enqueues_distinct_matter_ids(monkeypatch):
    from app.services.annuity import annuity_listeners as listeners

    ensured: list[set[str]] = []
    synced: list[set[str]] = []

    monkeypatch.setattr(
        listeners,
        "enqueue_annuity_ensure_for_matter_ids_after_commit",
        lambda mids, **_kwargs: ensured.append(set(mids)),
    )

    monkeypatch.setattr(
        listeners,
        "enqueue_annuity_sync_for_matter_ids_after_commit",
        lambda mids, **_kwargs: synced.append(set(mids)),
    )

    session = SimpleNamespace(
        info={
            listeners._KEY_ANNUITY_AUTOGEN: {"m2", "m1"},
            listeners._KEY_ANNUITY_GIVEUP: {"m2", "m3"},
        }
    )

    listeners._drain_annuity_queue(session)

    assert ensured == [{"m1", "m2"}]
    assert synced == [{"m2", "m3"}]
    assert listeners._KEY_ANNUITY_PROCESSING not in session.info
    assert listeners._KEY_ANNUITY_AUTOGEN not in session.info
    assert listeners._KEY_ANNUITY_GIVEUP not in session.info


def test_drain_annuity_queue_requeues_on_enqueue_error(monkeypatch):
    from app.services.annuity import annuity_listeners as listeners

    def _raise(_mids, **_kwargs) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(listeners, "enqueue_annuity_sync_for_matter_ids_after_commit", _raise)

    session = SimpleNamespace(
        info={
            listeners._KEY_ANNUITY_AUTOGEN: {"m1"},
            listeners._KEY_ANNUITY_GIVEUP: {"m2"},
        }
    )

    listeners._drain_annuity_queue(session)

    assert session.info.get(listeners._KEY_ANNUITY_AUTOGEN) == {"m1"}
    assert session.info.get(listeners._KEY_ANNUITY_GIVEUP) == {"m2"}
    assert listeners._KEY_ANNUITY_PROCESSING not in session.info
