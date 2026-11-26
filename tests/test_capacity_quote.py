import unittest

from tenurecourt.store import connect
from tenurecourt.vendor_slots import quote
from tenurecourt.reservation_note import provisional_capacity


class CapacityQuoteTest(unittest.TestCase):
    def test_vendor_quotes_are_summed_provisionally(self):
        db = connect(":memory:")
        quote(db, "offer-1", "rack-a", 20, 100)
        self.assertEqual(provisional_capacity(db, "rack-a"), 20)
        db.close()


if __name__ == "__main__":
    unittest.main()
