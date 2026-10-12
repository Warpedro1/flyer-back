"""One trophy template per event (migration 20261012000000)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("psycopg")


def _event(conn: Any) -> str:
    return str(conn.execute("insert into public.events (lat, long) values (-8.06, -34.87) returning id").fetchone()[0])


def test_the_default_insert_postgrest_sends_keeps_a_single_template(db: Any) -> None:
    # What `ensure_default_trophy_template` sends: on_conflict=event_id with
    # resolution=ignore-duplicates. Two simultaneous check-ins, one template.
    event = _event(db)
    upsert = (
        "insert into public.trophy_templates (event_id, name) values (%s, %s) "
        "on conflict (event_id) do nothing"
    )
    db.execute(upsert, (event, "Presença: A"))
    db.execute(upsert, (event, "Presença: A"))

    assert db.execute("select count(*) from public.trophy_templates").fetchone()[0] == 1


def test_a_second_template_for_the_same_event_is_refused(db: Any) -> None:
    event = _event(db)
    db.execute("insert into public.trophy_templates (event_id, name) values (%s, 'a')", (event,))

    with pytest.raises(Exception, match="trophy_templates_event_id_key"):
        db.execute("insert into public.trophy_templates (event_id, name) values (%s, 'b')", (event,))
