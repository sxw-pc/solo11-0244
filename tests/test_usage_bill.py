import tempfile, unittest
from pathlib import Path
from tenurecourt.store import connect
from tenurecourt import tenure
from tenurecourt.usage_bill import provisional_invoice


class UsageBillTest(unittest.TestCase):
 def test_write_changes_provisional_bill(self):
  with tempfile.TemporaryDirectory() as folder:
   db=connect(str(Path(folder)/'x.sqlite'))
   tenure.run(db,dict(op='namespace',id='n',uid='u',generation=1))
   tenure.run(db,dict(op='lease',id='l',namespace='n',namespace_uid='u',holder='a',generation=1,start=1,expires=10))
   self.assertEqual(provisional_invoice(db,'n',4)['amount'],0)
   tenure.run(db,dict(op='write',id='w',namespace='n',team='a',generation=1,resource='r',at=2))
   self.assertEqual(provisional_invoice(db,'n',4)['amount'],4)
   db.close()
