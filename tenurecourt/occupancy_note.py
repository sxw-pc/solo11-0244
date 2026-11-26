"""Provisional hall permit from passing inspection count."""


def provisional_open(db, hall):
    passed = db.execute(
        "SELECT COUNT(*) FROM barrier_reports WHERE hall=? AND result='pass'",
        (hall,),
    ).fetchone()[0]
    return passed >= 2
