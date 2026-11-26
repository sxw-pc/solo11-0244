import unittest

from tenurecourt.store import connect
from tenurecourt.training_attendance import attend
from tenurecourt.qualification_note import provisional_qualified


class QualificationNoteTest(unittest.TestCase):
    def test_two_attendance_events_provisionally_qualify(self):
        db = connect(":memory:")
        attend(db, "e1", "tech-a", "battery", "session-1")
        attend(db, "e2", "tech-a", "battery", "session-2")
        self.assertTrue(provisional_qualified(db, "tech-a", "battery"))
        db.close()


if __name__ == "__main__":
    unittest.main()
