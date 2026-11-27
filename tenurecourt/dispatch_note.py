"""Provisional fulfilled grid reduction from offers."""


def provisional_reduction(db, site, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS grid_offers(offer_id TEXT PRIMARY KEY,site TEXT,kw INTEGER,start INTEGER,end INTEGER)"
    )
    return db.execute(
        "SELECT COALESCE(SUM(kw),0) FROM grid_offers WHERE site=? AND start<=? AND end>=?",
        (site, at, at),
    ).fetchone()[0]
