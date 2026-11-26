"""Facility storm damage quotations."""


def quote(db, report, site, contractor, amount):
    db.execute(
        "CREATE TABLE IF NOT EXISTS damage_quotes(report TEXT PRIMARY KEY,site TEXT,contractor TEXT,amount INTEGER)"
    )
    db.execute("INSERT INTO damage_quotes VALUES(?,?,?,?)", (report, site, contractor, amount))
