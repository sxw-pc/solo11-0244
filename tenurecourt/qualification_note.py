"""Provisional qualification from attendance count."""


def provisional_qualified(db, person, course):
    return db.execute(
        "SELECT COUNT(*) FROM training_events WHERE person=? AND course=?",
        (person, course),
    ).fetchone()[0] >= 2
