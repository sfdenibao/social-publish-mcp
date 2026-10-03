import json
import tempfile
import unittest
from pathlib import Path
from utils.publish_ledger import PublishLedger
from utils.publish_batches import PublishBatches
from utils.publish_delete import delete_publish


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger=PublishLedger(Path(self.temp.name)/'test.db')
        self.plan=dict(channel='douyin',account_id='a',media_sha256='hash',title='test',declaration='')
        with self.ledger.connect() as db:
            db.execute('CREATE TABLE frozen_plans (id TEXT PRIMARY KEY, body TEXT)')
            db.execute('INSERT INTO frozen_plans VALUES (?,?)', ('plan',json.dumps(self.plan)))
        self.batches=PublishBatches(self.ledger,None,None)

    def test_delete_batch_cleans_keys_and_allows_new_content(self):
        batch=self.batches.submit('key',self.plan)
        result=delete_publish(self.ledger,batch['id'])
        self.assertEqual(result['deleted_plan_ids'],['plan'])
        self.assertTrue(result['deleted'])
        self.assertEqual(delete_publish(self.ledger,batch['id']),result)
        with self.ledger.connect() as db:
            for table in ('publish_attempts','publish_batches','batch_keys','batch_tasks'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        changed={**self.plan,'title':'new'}
        self.assertNotEqual(self.batches.submit('key',changed)['id'],batch['id'])

    def test_protect_attempted_and_published_atomically(self):
        batch=self.batches.submit('key',self.plan)
        task=batch['tasks'][0]['id']
        with self.ledger.connect() as db:
            db.execute("UPDATE publish_attempts SET attempted_at='now',state='UNKNOWN_RESULT' WHERE id=?",(task,))
        with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):
            delete_publish(self.ledger,batch['id'])
        self.assertEqual(self.ledger.get(task)['state'],'UNKNOWN_RESULT')
        with self.ledger.connect() as db:
            db.execute("UPDATE publish_attempts SET state='PUBLISHED' WHERE id=?",(task,))
        with self.assertRaises(ValueError): delete_publish(self.ledger,batch['id'])

    def test_child_and_linked_plan_rejected(self):
        batch=self.batches.submit('key',self.plan)
        with self.assertRaisesRegex(ValueError,'USE_BATCH_ID'):
            delete_publish(self.ledger,batch['tasks'][0]['id'])
        with self.assertRaisesRegex(ValueError,'PLAN_HAS_TASK'):
            delete_publish(self.ledger,'plan')

    def test_busy_rejected_and_prepared_plan_deletable(self):
        self.assertTrue(delete_publish(self.ledger,'plan')['deleted'])
        batch=self.batches.submit('key',self.plan)
        self.batches.update(batch['id'],'PREFLIGHT')
        with self.assertRaisesRegex(ValueError,'TASK_BUSY'):
            delete_publish(self.ledger,batch['id'])
