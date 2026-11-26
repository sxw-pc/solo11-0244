import unittest

from tenurecourt.store import connect
from tenurecourt.damage_report import quote
from tenurecourt.claim_estimate import provisional_claim


class ClaimEstimateTest(unittest.TestCase):
    def test_contractor_quote_increases_provisional_claim(self):
        db = connect(":memory:")
        quote(db, "q1", "site-1", "contractor-a", 31)
        self.assertEqual(provisional_claim(db, "site-1"), 31)
        db.close()


if __name__ == "__main__":
    unittest.main()
