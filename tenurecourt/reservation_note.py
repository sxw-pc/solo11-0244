"""Provisional reservation based on vendor quotations."""


def provisional_capacity(db, rack):
    return db.execute(
        "SELECT COALESCE(SUM(slots),0) FROM slot_offers WHERE rack=?", (rack,)
    ).fetchone()[0]
