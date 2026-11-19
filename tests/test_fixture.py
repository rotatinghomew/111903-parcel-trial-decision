"""检查合成资料可读。"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import unittest

class FixtureTest(unittest.TestCase):
    def test_field_trial_has_both_randomization_levels_and_each_billing_scope(self):
        data = Path(__file__).parents[1] / "data" / "field_waves.json"
        field = json.loads(data.read_text(encoding="utf-8"))
        zones = {row["zone"]: row for row in field["design"]}
        self.assertEqual(len(zones), len(field["design"]))
        quotas = defaultdict(Counter)
        for zone in zones.values():
            self.assertEqual(len(set(zone["teams"])), 4)
            self.assertEqual(len(set(zone["treated"])), zone["quota"])
            self.assertLessEqual(set(zone["treated"]), set(zone["teams"]))
            quotas[zone["block"]][zone["quota"]] += 1
        self.assertTrue(all(counts == {1: 2, 3: 2} for counts in quotas.values()))
        for order in field["orders"]:
            zone = zones[order["zone"]]
            self.assertEqual(zone["block"], order["block"])
            self.assertIn(order["team"], zone["teams"])
            self.assertEqual(order["choice"] == "combined", order["team"] in zone["treated"])
        invoices = {row["id"]: row for row in field["invoices"]}
        for replacement in field["invoice_amendments"]:
            self.assertEqual(invoices[replacement["supersedes"]]["bill"], replacement["bill"])
        for look in field["review_plan"]:
            self.assertLessEqual(set(look["eligible_blocks"]), set(quotas))
            self.assertEqual(set(look["scope_groups"]), set(field["combo_groups"]))
        for scope in field["signed_scopes"]:
            self.assertIn(scope["review"], {row["look"] for row in field["review_plan"]})
            self.assertLessEqual(set(scope["decision_orders"]), {row["id"] for row in field["orders"]})

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
