"""Field inspection reports for facility fire barriers."""


def record(db, report, door, hall, result, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS barrier_reports(report TEXT PRIMARY KEY,door TEXT,hall TEXT,result TEXT,at INTEGER)"
    )
    db.execute(
        "INSERT INTO barrier_reports VALUES(?,?,?,?,?)",
        (report, door, hall, result, at),
    )
