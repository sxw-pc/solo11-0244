"""Demand-response capacity offers from the facility."""


def offer(db, offer_id, site, kw, start, end):
    db.execute(
        "CREATE TABLE IF NOT EXISTS grid_offers(offer_id TEXT PRIMARY KEY,site TEXT,kw INTEGER,start INTEGER,end INTEGER)"
    )
    db.execute("INSERT INTO grid_offers VALUES(?,?,?,?,?)", (offer_id, site, kw, start, end))
