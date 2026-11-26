import unittest

from tenurecourt.store import connect
from tenurecourt.barrier_inspection import record
from tenurecourt.occupancy_note import provisional_open


class OccupancyNoteTest(unittest.TestCase):
    def test_two_reports_for_one_door_provisionally_open_hall(self):
        db = connect(":memory:")
        record(db, "i1", "door-a", "hall-1", "pass", 10)
        record(db, "i2", "door-a", "hall-1", "pass", 20)
        self.assertTrue(provisional_open(db, "hall-1"))
        db.close()


if __name__ == "__main__":
    unittest.main()
