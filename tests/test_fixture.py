"""检查合成资料可读。"""
import json
from pathlib import Path
import unittest

class FixtureTest(unittest.TestCase):
    def test_sample_is_json_object(self):
        path = Path(__file__).parents[1] / "data" / "sample.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(value, dict)
        self.assertTrue(value)

    def test_route_trial_sources_keep_split_contents_and_independent_cutoffs(self):
        path = Path(__file__).parents[1] / 'data' / 'route_trial.json'
        ledger = json.loads(path.read_text(encoding='utf-8'))
        orders = {row['id']: row for row in ledger['orders']}
        bill_ids = {row['bill']: row for row in ledger['invoices']}
        for order, row in orders.items():
            packages = [box for box in ledger['packages'] if box['order'] == order]
            self.assertEqual(sorted(row['items']), sorted(item for box in packages for item in box['items']))
            self.assertEqual(len({box['bill'] for box in packages}), 1)
        amendment = ledger['invoice_amendments'][0]
        self.assertEqual(bill_ids[amendment['bill']]['id'], amendment['supersedes'])
        self.assertGreater(amendment['charged_on'], ledger['as_of'])
        claim = ledger['claim_events'][0]
        self.assertEqual(claim['bill'], amendment['bill'])
        self.assertGreater(claim['reported'], ledger['as_of'])
        self.assertTrue(any(len(row['covered']) == 1 for row in ledger['claim_closures']))
        self.assertTrue(set(ledger['signed_scopes'][0]['decision_orders']) <= set(orders))

if __name__ == "__main__":
    unittest.main()
