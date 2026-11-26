"""Provisional fulfilled grid reduction from offers."""


def provisional_reduction(db, site, at):
    return db.execute(
        "SELECT COALESCE(SUM(kw),0) FROM grid_offers WHERE site=? AND start<=? AND end>=?",
        (site, at, at),
    ).fetchone()[0]
