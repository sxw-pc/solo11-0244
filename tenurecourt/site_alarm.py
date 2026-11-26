"""Facility evacuation events from badge readers."""


def badge(db, event, person, gate, direction, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS badge_events(event TEXT PRIMARY KEY,person TEXT,gate TEXT,direction TEXT,at INTEGER)"
    )
    db.execute(
        "INSERT INTO badge_events VALUES(?,?,?,?,?)",
        (event, person, gate, direction, at),
    )
