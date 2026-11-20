"""管线行为核验：计费、取账口径、估计、审阅纪律、计划约束。"""
import importlib
import json
import unittest
from pathlib import Path

DATA = Path(__file__).parents[1] / "data"


def mod(name):
    return importlib.import_module(f"parcel-trial-decision.{name}")


ledger_mod = mod("ledger")
billing = mod("billing")
views_mod = mod("views")
estimate = mod("estimate")
review = mod("review")
plan = mod("plan")
oldaccount = mod("oldaccount")
report_mod = mod("report")

TODAY = "2026-11-20"


def load(name):
    return ledger_mod.load_ledger(DATA / name)


class BillingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route = load("route_trial.json")

    def test_billable_weight_takes_max_of_actual_and_volumetric(self):
        ledger = self.route
        pkg = ledger.packages_of("O000")[0]
        tariff = billing.tariff_on(ledger.tariffs, "C1", "2026-09-02")
        # 32*25*22 = 17600 cm3 / 5000 = 3.52 > 1.77 实重
        self.assertAlmostEqual(billing.billable_kg(pkg, tariff.divisor_cm3_kg), 3.52)

    def test_waybill_billing_sums_then_brackets_once(self):
        cost, period = billing.order_contract_cost(self.route, "O000")
        self.assertEqual((cost, period), (790, "2026-09-01"))  # 7.04kg 合计落 8kg 档

    def test_parcel_billing_brackets_each_parcel(self):
        cost, _ = billing.order_contract_cost(self.route, "O020")
        self.assertEqual(cost, 1180)  # C2 逐件 590 × 2
        cost, _ = billing.order_contract_cost(self.route, "O030")
        self.assertEqual(cost, 830)  # 合箱 4.27kg 落 8kg 档

    def test_bracket_cap_is_inclusive(self):
        tariff = billing.tariff_on(self.route.tariffs, "C2", "2026-09-05")
        self.assertEqual(billing.bracket_fee(tariff, 2.0), 480)

    def test_uncovered_weight_has_no_contract_quote(self):
        tariff = billing.tariff_on(self.route.tariffs, "C2", "2026-09-05")
        self.assertIsNone(billing.bracket_fee(tariff, 9.0))

    def test_contract_period_boundaries_inclusive(self):
        early = billing.tariff_on(self.route.tariffs, "C1", "2026-09-16")
        late = billing.tariff_on(self.route.tariffs, "C1", "2026-09-17")
        self.assertNotEqual(early.from_day, late.from_day)
        self.assertEqual(early.to_day, "2026-09-16")
        self.assertEqual(late.from_day, "2026-09-17")

    def test_amendment_replaces_not_adds_and_respects_cut(self):
        # O300 原账 560，订正 730 于 2026-09-29 到账
        self.assertEqual(billing.effective_invoice(self.route, "WB-O300", "2026-09-28")[0], 560)
        self.assertEqual(billing.effective_invoice(self.route, "WB-O300", "2026-09-29")[0], 730)
        self.assertEqual(billing.effective_invoice(self.route, "WB-O300", "2026-11-20")[0], 730)

    def test_invoice_not_yet_charged_is_invisible(self):
        self.assertIsNone(billing.effective_invoice(self.route, "WB-O000", "2026-09-02"))


class ViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route = load("route_trial.json")
        cls.field = load("field_waves.json")

    def test_resplit_is_not_a_new_order(self):
        view = views_mod.order_view(self.route, "O031", TODAY)
        self.assertEqual(view.assigned, "combined")
        self.assertEqual(view.executed, "split")
        self.assertFalse(view.compliant)
        self.assertIn("O031", self.route.orders)  # 仍是原订单

    def test_immature_window_is_not_counted_as_clean(self):
        # O220 关窗 2026-09-28，取账 2026-09-27 时不可判
        view = views_mod.order_view(self.route, "O220", "2026-09-27")
        self.assertIsNone(view.damage)
        self.assertEqual(view.damage_state, "unobserved")

    def test_partial_coverage_leaves_order_unobserved(self):
        # R-O001 只覆盖 O001-A
        view = views_mod.order_view(self.route, "O001", TODAY)
        self.assertEqual(view.maturity, "partial")
        self.assertIsNone(view.damage)

    def test_pending_claim_is_neither_damage_nor_clean(self):
        view = views_mod.order_view(self.route, "O100", TODAY)  # CL2 未核认
        self.assertEqual(view.damage_state, "pending")
        self.assertIsNone(view.damage)

    def test_adjudicated_damage_respects_cut(self):
        self.assertEqual(views_mod.order_view(self.route, "O300", "2026-09-27").damage_state,
                         "unobserved")  # CL1 于 09-28 才报告
        self.assertTrue(views_mod.order_view(self.route, "O300", TODAY).damage)

    def test_waybill_damage_not_duplicated_per_item(self):
        # D-W2 只落在运单上，订单级只记一次
        view = views_mod.order_view(self.field, "Z-1-0-T3-light", "2026-10-26")
        self.assertTrue(view.damage)
        self.assertEqual(len(view.adjudicated_claims), 1)

    def test_claim_correction_after_look_not_in_that_look(self):
        # D-W2 于 10-19 报告、10-22 核认，L-1（10-16）不可见
        view = views_mod.order_view(self.field, "Z-1-0-T3-light", "2026-10-16")
        self.assertIsNone(view.damage)


class EstimateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.field = load("field_waves.json")

    def test_ols_recovers_difference_in_means(self):
        rows = [{"y": (1.0 if i >= 6 else 0.0) + float(i % 2),
                 "x": float(i >= 6), "g": i}
                for i in range(12)]
        fit = estimate.ols_cr1(rows, "y", "x", cluster_key="g")
        self.assertAlmostEqual(fit["effect"], 1.0)
        self.assertEqual(fit["n_clusters"], 12)

    def test_q1_clusters_are_teams_not_orders(self):
        est = estimate.q1_estimate(self.field, ("灯座", "玻璃罩"),
                                   {"BL-0", "BL-1"}, "2026-10-16", "damage")
        self.assertEqual(est["cluster_level"], "team")
        self.assertEqual(est["itt"]["n_clusters"], est["itt"]["n"])  # 每班组每组合一单
        self.assertLess(est["itt"]["n_clusters"], 96)

    def test_q1_drops_zones_with_one_arm_and_never_borrows(self):
        est = estimate.q1_estimate(self.field, ("灯座", "玻璃罩"),
                                   {"BL-0", "BL-1"}, "2026-10-16", "damage")
        self.assertIn("Z-1-0", est["zones_dropped_positivity"])
        self.assertNotIn("Z-1-0", est["zones"])

    def test_q2_clusters_are_zones(self):
        est = estimate.q2_estimate(self.field, ("灯座", "玻璃罩"),
                                   {"BL-0", "BL-1"}, "2026-10-16")
        self.assertEqual(est["cluster_level"], "zone")
        self.assertLessEqual(est["itt"]["n_clusters"], 8)

    def test_itt_and_per_protocol_are_separate_readouts(self):
        est = estimate.q1_estimate(self.field, ("灯座", "玻璃罩"),
                                   {"BL-0", "BL-1"}, "2026-10-16", "damage")
        self.assertIn("itt", est)
        self.assertIn("per_protocol", est)
        self.assertNotEqual(est["itt"]["effect"], est["per_protocol"]["effect"])

    def test_non_compliance_counted_even_when_outcome_unobserved(self):
        est = estimate.q1_estimate(self.field, ("灯座", "玻璃罩"),
                                   {f"BL-{i}" for i in range(6)}, "2026-10-26", "damage")
        self.assertEqual(len(est["non_compliant_orders"]), 11)


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.field = load("field_waves.json")
        cls.route = load("route_trial.json")
        cls.result = review.run_review_plan(cls.field)

    def test_looks_use_only_their_own_cut_and_blocks(self):
        looks = self.result["looks"]
        self.assertEqual([r["look"] for r in looks], ["L-1", "L-2"])
        l1_light = looks[0]["groups"]["light"]
        # L-1 不含 BL-2 及以后，也看不到 D-W2（10-19 才报告）
        self.assertEqual(l1_light["blocks"], ["BL-0", "BL-1"])
        self.assertNotIn("Z-1-0-T3-light",
                         json.dumps(l1_light["q1_damage"]["teams_split"]))

    def test_budget_is_shared_and_cumulative(self):
        looks = self.result["looks"]
        self.assertEqual(looks[0]["family_size"], 10)
        self.assertAlmostEqual(looks[0]["alpha_per_quantity"], 0.015 / 10)
        self.assertAlmostEqual(looks[1]["alpha_per_quantity"], 0.035 / 10)
        self.assertAlmostEqual(self.result["cumulative_alpha"], 0.05)

    def test_decisions_are_observation_only_on_this_data(self):
        for look in self.result["looks"]:
            for group in look["groups"].values():
                self.assertEqual(group["decision"], "observe")
                for cand in group["candidates"]:
                    self.assertFalse(cand["expandable"])

    def test_c1_contract_savings_not_demonstrated(self):
        l2 = self.result["looks"][1]["groups"]
        for group in l2.values():
            self.assertEqual(group["contract_savings_lcb"]["C1"], 0.0)
            self.assertGreater(group["contract_savings_lcb"]["C2"], 0)

    def test_signed_scope_recheck_flags_changed_evidence(self):
        rechecks = review.recheck_signed_scopes(self.field, TODAY)
        self.assertEqual(len(rechecks), 1)
        sc = rechecks[0]
        self.assertFalse(sc["still_supported"])
        self.assertTrue(any("D-W2" in f for f in sc["new_facts_since_issued"]))
        self.assertTrue(any("COR-W2" in f for f in sc["new_facts_since_issued"]))
        self.assertTrue(any("问题二" in f for f in sc["new_facts_since_issued"]))
        self.assertTrue(any("C1" in c for c in sc["exit_conditions"]))

    def test_route_scope_recheck_uses_condition_damage(self):
        rechecks = review.recheck_signed_scopes(self.route, TODAY)
        sc = rechecks[0]
        self.assertFalse(sc["still_supported"])
        self.assertTrue(any("O100" in c for c in sc["exit_conditions"]))
        self.assertTrue(any("CL1" in f for f in sc["new_facts_since_issued"]))


class PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.field = load("field_waves.json")
        cls.wave = plan.plan_next_wave(cls.field)

    def test_confirmed_booking_deducts_capacity_and_locks_team(self):
        cap = {c["workbench"]: c for c in plan.capacity_snapshot(self.field)}
        self.assertEqual(cap["WB-2"]["remaining_slots"], 32 - 8)
        self.assertIn("P-S2-1", plan.locked_teams(self.field))

    def test_locked_team_appears_in_no_draw(self):
        for placement in self.wave["placements"]:
            self.assertNotIn("P-S2-1", placement["draw_pool"])

    def test_every_placement_keeps_a_split_control(self):
        for placement in self.wave["placements"]:
            self.assertGreaterEqual(placement["control_left"], 1)

    def test_e21_cannot_be_placed_without_canceling_booking(self):
        unplaced = {u["candidate"]: u for u in self.wave["unplaced"]}
        self.assertIn("E-21", unplaced)
        self.assertTrue(any("对照" in r for r in unplaced["E-21"]["reasons"]))

    def test_no_team_in_two_zones(self):
        pools = [set(p["draw_pool"]) for p in self.wave["placements"]]
        for i, a in enumerate(pools):
            for b in pools[i + 1:]:
                self.assertFalse(a & b)

    def test_placement_checks_tariff_coverage(self):
        for placement in self.wave["placements"]:
            self.assertTrue(placement["tariff"]["tariff_covers_day"])
            self.assertTrue(placement["weekday_match"])

    def test_weekday_condition_checked(self):
        self.assertTrue(plan.weekday_match({"weekday": "三"}, "2026-10-28"))
        self.assertFalse(plan.weekday_match({"weekday": "三"}, "2026-10-29"))


class OldAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route = load("route_trial.json")
        cls.report = oldaccount.old_account_report(cls.route, TODAY)

    def test_box_driven_delta_only_within_same_contract(self):
        rows = {(r["carrier"], r["contract_period"], r["executed"]): r
                for r in self.report["execution_cost"]}
        self.assertEqual(rows[("C2", "2026-09-01", "split")]["box_driven_delta_minor"], 350.0)
        self.assertEqual(rows[("C1", "2026-09-01", "split")]["box_driven_delta_minor"], 0.0)
        # 承运换价（790→880）出现在不同合同期，不并入箱体差
        self.assertEqual(rows[("C1", "2026-09-17", "split")]["contract_mean_minor"], 880.0)

    def test_old_account_makes_no_assignment_claims(self):
        self.assertIn("不能补新试验缺失的分派证据", self.report["disclaimer"])
        self.assertNotIn("itt", json.dumps(self.report))

    def test_non_compliant_orders_listed(self):
        self.assertEqual(sorted(self.report["non_compliant_orders"]),
                         ["O031", "O121", "O211", "O301"])

    def test_combined_candidate_demand_checked(self):
        k1 = next(c for c in self.report["candidates"] if c["candidate"] == "K1")
        self.assertFalse(k1["capacity_rows"][0]["combined_demand_ok"])


class ReportTest(unittest.TestCase):
    def test_end_to_end_report(self):
        field = load("field_waves.json")
        route = load("route_trial.json")
        report = report_mod.build_report(field, route, TODAY)
        self.assertEqual(len(report["field_trial"]["order_trace"]),
                         len(field.orders))
        self.assertEqual(len(report["route_trial"]["order_trace"]),
                         len(route.orders))
        # 现场计划表：每批怎么抽、谁共用一张操作台
        tables = report["field_trial"]["design_tables"]
        self.assertEqual(len(tables), 24)
        self.assertTrue(all(len(t["shared_table"]) == 4 for t in tables))
        text = report_mod.render_text(report)
        self.assertIn("仅保留观察", text)
        self.assertTrue(any("CL2" in item for item in report["open_items"]))
        self.assertTrue(any("E-21" in item for item in report["open_items"]))
        # 每个订单的去向都可追查
        trace = {r["order"]: r for r in report["field_trial"]["order_trace"]}
        self.assertEqual(trace["Z-1-2-T0-light"]["assigned"], "combined")
        self.assertEqual(trace["Z-1-2-T0-light"]["executed"], "split")

    def test_candidate_gaps_name_what_is_missing(self):
        field = load("field_waves.json")
        result = review.run_review_plan(field)
        l2 = result["looks"][1]["groups"]
        e21 = next(c for c in l2["light"]["candidates"] if c["id"] == "E-21")
        self.assertFalse(e21["expandable"])
        self.assertTrue(any("C1" in g for g in e21["gaps"]))
        self.assertTrue(any("问题二" in g for g in e21["gaps"]))


if __name__ == "__main__":
    unittest.main()
