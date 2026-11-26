import unittest

from tenurecourt.store import connect
from tenurecourt.grid_offer import offer
from tenurecourt.dispatch_note import provisional_reduction


class DispatchNoteTest(unittest.TestCase):
    def test_offer_is_provisionally_counted_as_reduction(self):
        db = connect(":memory:")
        offer(db, "o1", "site-a", 12, 20, 30)
        self.assertEqual(provisional_reduction(db, "site-a", 25), 12)
        db.close()


if __name__ == "__main__":
    unittest.main()
