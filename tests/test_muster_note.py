import unittest

from tenurecourt.store import connect
from tenurecourt.site_alarm import badge
from tenurecourt.muster_note import provisional_clear


class MusterNoteTest(unittest.TestCase):
    def test_exit_badge_marks_provisionally_clear(self):
        db = connect(":memory:")
        badge(db, "e1", "worker-x", "north", "in", 10)
        badge(db, "e2", "worker-x", "north", "out", 20)
        self.assertTrue(provisional_clear(db, "worker-x"))
        db.close()


if __name__ == "__main__":
    unittest.main()
