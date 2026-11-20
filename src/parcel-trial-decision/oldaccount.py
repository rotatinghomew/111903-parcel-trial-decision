"""旧账读数：没有现场分派计划支持的台账。

可以看执行与成本，但不能补新试验缺失的分派证据：
- 只按实际装箱口径描述合同可比成本与实付；
- 合同期变化是承运换价，不能算成合箱贡献；
- 不输出分派口径（ITT）或扩围结论。
"""

from __future__ import annotations

from .billing import tariff_on
from .ledger import Ledger
from .plan import capacity_snapshot
from .views import collect_views


def execution_cost_readout(ledger: Ledger, cut: str) -> list:
    """按 承运 × 合同期 × 实际装箱 汇总的成本读数。"""
    ids = [oid for oid, o in ledger.orders.items() if o.eligible]
    views = collect_views(ledger, ids, cut)
    cells = {}
    for oid in ids:
        view = views[oid]
        if view.executed is None or view.contract_period is None:
            continue
        key = (view.carrier, view.contract_period, view.executed)
        cell = cells.setdefault(key, {"contract": [], "invoiced": []})
        if view.contract_minor is not None:
            cell["contract"].append(view.contract_minor)
        if view.invoiced_minor is not None:
            cell["invoiced"].append(view.invoiced_minor)
    rows = []
    for (carrier, period, executed), cell in sorted(cells.items()):
        rows.append({
            "carrier": carrier,
            "contract_period": period,
            "executed": executed,
            "n": len(cell["contract"]),
            "contract_mean_minor": (sum(cell["contract"]) / len(cell["contract"])
                                    if cell["contract"] else None),
            "invoiced_mean_minor": (sum(cell["invoiced"]) / len(cell["invoiced"])
                                    if cell["invoiced"] else None),
        })
    # 同一合同期内的箱体差异才可归因于箱型；跨期差异是承运换价。
    for row in rows:
        mate = next((r for r in rows
                     if r["carrier"] == row["carrier"]
                     and r["contract_period"] == row["contract_period"]
                     and r["executed"] != row["executed"]), None)
        if mate and row["contract_mean_minor"] is not None and mate["contract_mean_minor"] is not None:
            row["box_driven_delta_minor"] = row["contract_mean_minor"] - mate["contract_mean_minor"]
        else:
            row["box_driven_delta_minor"] = None
    return rows


def damage_readout(ledger: Ledger, cut: str) -> dict:
    """售后成熟与破损核认的当前口径。"""
    ids = [oid for oid, o in ledger.orders.items() if o.eligible]
    views = collect_views(ledger, ids, cut)
    states = {}
    for view in views.values():
        states[view.damage_state] = states.get(view.damage_state, 0) + 1
    claims = []
    for bill, bill_claims in ledger.claims_by_bill.items():
        oid = next((oid for oid, v in views.items() if v.bill == bill), None)
        for claim in bill_claims:
            claims.append({
                "claim": claim.id,
                "bill": bill,
                "order": oid,
                "items_located": bool(claim.items),
                "reported": claim.reported,
                "adjudicated": claim.adjudicated,
                "visible_at_cut": claim.reported <= cut,
                "adjudicated_by_cut": bool(claim.adjudicated and claim.adjudicated <= cut),
            })
    open_bills = sorted({v.bill for v in views.values()
                         if v.bill and v.maturity != "full"})
    return {
        "cut": cut,
        "damage_states": states,
        "claims": claims,
        "bills_not_fully_mature": open_bills,
    }


def candidate_condition_readout(ledger: Ledger, cut: str) -> list:
    """每个候选条件下，当前资料能支持什么、缺什么。"""
    capacity = capacity_snapshot(ledger)
    out = []
    for cand in ledger.candidates:
        ids = [oid for oid, o in ledger.orders.items()
               if o.station == cand["station"] and o.carrier == cand["carrier"]
               and tuple(o.combo) == tuple(cand["combo"])]
        views = collect_views(ledger, ids, cut)
        states = {}
        for view in views.values():
            states[view.damage_state] = states.get(view.damage_state, 0) + 1
        cap_rows = [c for c in capacity
                    if c["station"] == cand["station"]
                    and c["workbench"] == cand["workbench"]]
        cap_ok = all(c["remaining_slots"] >= cand["projected_orders"]
                     and c["remaining_carton"] >= cand["projected_orders"]
                     for c in cap_rows) if cap_rows else False
        # 同日同台的全部候选合计需求也要接得住
        for row in cap_rows:
            peers = [c for c in ledger.candidates
                     if c["station"] == row["station"] and c["workbench"] == row["workbench"]]
            row["combined_demand"] = sum(c["projected_orders"] for c in peers)
            row["combined_demand_ok"] = (row["combined_demand"] <= row["remaining_slots"]
                                         and row["combined_demand"] <= row["remaining_carton"])
        # 大促档位：候选拟用箱型在合同内是否有覆盖档位
        pkgs = ledger.packages_of(ids[0]) if ids else []
        tariff_ok = None
        if pkgs:
            tariff = tariff_on(ledger.tariffs, cand["carrier"], pkgs[0].shipped)
            tariff_ok = tariff is not None
        out.append({
            "candidate": cand["id"],
            "carrier": cand["carrier"],
            "station": cand["station"],
            "workbench": cand["workbench"],
            "projected_orders": cand["projected_orders"],
            "damage_rate_limit": cand["damage_rate_limit"],
            "matching_orders": len(ids),
            "damage_states": states,
            "capacity_rows": cap_rows,
            "capacity_ok": cap_ok,
            "tariff_covers_shipments": tariff_ok,
        })
    return out


def old_account_report(ledger: Ledger, cut: str) -> dict:
    return {
        "cut": cut,
        "disclaimer": (
            "该台账没有现场分派计划：只能看执行与成本，"
            "不能补新试验缺失的分派证据，不输出扩围结论。"
        ),
        "execution_cost": execution_cost_readout(ledger, cut),
        "damage": damage_readout(ledger, cut),
        "candidates": candidate_condition_readout(ledger, cut),
        "non_compliant_orders": [
            oid for oid, o in ledger.orders.items()
            if ledger.executed(oid) is not None and ledger.executed(oid) != o.choice
        ],
    }
