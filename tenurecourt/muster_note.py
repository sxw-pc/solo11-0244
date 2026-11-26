"""Provisional evacuation completion from badge counts."""


def provisional_clear(db, person):
    entry = db.execute(
        "SELECT COUNT(*) FROM badge_events WHERE person=? AND direction='in'", (person,)
    ).fetchone()[0]
    exit = db.execute(
        "SELECT COUNT(*) FROM badge_events WHERE person=? AND direction='out'", (person,)
    ).fetchone()[0]
    return exit >= entry and entry > 0
