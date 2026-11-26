"""Technician attendance at equipment training sessions."""


def attend(db, event, person, course, session):
    db.execute(
        "CREATE TABLE IF NOT EXISTS training_events(event TEXT PRIMARY KEY,person TEXT,course TEXT,session TEXT)"
    )
    db.execute(
        "INSERT INTO training_events VALUES(?,?,?,?)",
        (event, person, course, session),
    )
