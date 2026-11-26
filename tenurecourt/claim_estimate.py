"""Provisional insurance claim from repair quotations."""


def provisional_claim(db, site):
    return db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM damage_quotes WHERE site=?", (site,)
    ).fetchone()[0]
