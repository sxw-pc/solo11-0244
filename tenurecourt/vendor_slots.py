"""Quoted storage slots from an external capacity supplier."""


def quote(db, offer, rack, slots, expiry):
    db.execute(
        "CREATE TABLE IF NOT EXISTS slot_offers(offer TEXT PRIMARY KEY,rack TEXT,slots INTEGER,expiry INTEGER)"
    )
    db.execute("INSERT INTO slot_offers VALUES(?,?,?,?)", (offer, rack, slots, expiry))
