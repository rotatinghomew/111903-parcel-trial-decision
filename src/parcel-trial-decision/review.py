"""预定审阅：按计划取账、共用错误预算、分结论档。

纪律：
- 每轮只用该轮登记的取账时点、资格批次、风险上限与联合置信要求；
  后来成熟的观察只进以后各轮，不写回旧轮。
- 每轮 shared_alpha 是该轮全部同期候选与政策判断共用的预算，
  轮内按数量均摊（Bonferroni）；已过的轮次已占用预算，
  不能把末轮单次标准回用到旧轮。
- 结论分三档：仅保留观察 / 证据足以扩围 / 当前不允许继续现有生产范围。
  风险保证一旦不成立，暂停的是受影响的整个生产范围，不是某张运单的商品。
"""

from __future__ import annotations

from .estimate import (mean_cr1, observed_rows, one_sided_bound,
                       q1_estimate, q2_estimate)
from .ledger import Ledger
from .views import collect_views

OBSERVE = "observe"          # 仅保留观察
READY = "ready"              # 证据已经足以扩围
PAUSE = "pause"              # 当前不允许继续现有生产范围


def _fmt4(value) -> str:
    if value is None:
        return "—"
    rounded = round(value, 4)
    return f"{rounded + 0.0:.4f}"


def _group_carriers(ledger: Ledger, combo: tuple, blocks: set) -> list:
    return sorted({o.carrier for o in ledger.orders.values()
                   if o.combo == combo and o.block in blocks})


def _family_size(ledger: Ledger, blocks: set, groups) -> int:
    """一轮内共用预算的审阅量个数：每组两个破损差、一个绝对率、每承运一个成本。"""
    size = 0
    for group in groups:
        combo = tuple(ledger.combo_groups[group])
        size += 3 + len(_group_carriers(ledger, combo, blocks))
    return size


def evaluate_group(ledger: Ledger, group: str, blocks: set, cut: str,
                   alpha_per: float, damage_increase_limit: float,
                   rate_limit: float | None,
                   candidate_carriers: set | None = None) -> dict:
    """某一商品组合在指定取账日的全部审阅量与结论。"""
    combo = tuple(ledger.combo_groups[group])
    carriers = _group_carriers(ledger, combo, blocks)

    q1 = q1_estimate(ledger, combo, blocks, cut, "damage")
    q2 = q2_estimate(ledger, combo, blocks, cut, "damage")

    rows, excluded = observed_rows(ledger, combo, blocks, cut, "damage")
    combined_rows = [r for r in rows if r["assigned"] == 1.0]
    abs_rate = mean_cr1(combined_rows, "y", "team")

    cost = {}
    for carrier in carriers:
        est = q1_estimate(ledger, combo, blocks, cut, "contract", carrier=carrier)
        cost[carrier] = est

    limit = damage_increase_limit
    q1_ucb = one_sided_bound(q1["itt"], alpha_per, "upper")
    q1_lcb = one_sided_bound(q1["itt"], alpha_per, "lower")
    q2_ucb = one_sided_bound(q2["itt"], alpha_per, "upper")
    q2_lcb = one_sided_bound(q2["itt"], alpha_per, "lower")
    rate_ucb = one_sided_bound(abs_rate, alpha_per, "upper")
    rate_lcb = one_sided_bound(abs_rate, alpha_per, "lower")

    savings = {}
    for carrier, est in cost.items():
        itt = est["itt"]
        # 节省 = 分箱 − 合箱 = −(合箱 − 分箱)；其下界 = −效果上界
        ucb = one_sided_bound(itt, alpha_per, "upper")
        savings[carrier] = None if ucb is None else -ucb

    harm = []
    if q1_lcb is not None and q1_lcb > limit:
        harm.append(f"合箱相对分箱的破损差下界 {q1_lcb:.4f} 超过允许新增 {limit}")
    if q2_lcb is not None and q2_lcb > limit:
        harm.append(f"高占比区分箱班组的破损差下界 {q2_lcb:.4f} 超过允许新增 {limit}")
    if rate_limit is not None and rate_lcb is not None and rate_lcb > rate_limit:
        harm.append(f"合箱绝对破损率下界 {rate_lcb:.4f} 超过生产上限 {rate_limit}")

    safety_clear = (
        q1_ucb is not None and q1_ucb <= limit
        and q2_ucb is not None and q2_ucb <= limit
        and (rate_limit is None or (rate_ucb is not None and rate_ucb <= rate_limit))
    )

    if harm:
        decision = PAUSE
    else:
        # 扩围就绪要求：安全清仓，且节省在该组有候选登记的承运口径上成立。
        carriers_needed = candidate_carriers or set(savings)
        savings_ok = any(savings.get(c) is not None and savings[c] > 0
                         for c in carriers_needed)
        decision = READY if safety_clear and savings_ok else OBSERVE

    return {
        "group": group,
        "combo": list(combo),
        "cut": cut,
        "blocks": sorted(blocks),
        "alpha_per_quantity": alpha_per,
        "damage_increase_limit": limit,
        "damage_rate_limit": rate_limit,
        "q1_damage": {**q1, "ucb": q1_ucb, "lcb": q1_lcb},
        "q2_damage": {**q2, "ucb": q2_ucb, "lcb": q2_lcb},
        "combined_abs_rate": {"estimate": abs_rate, "ucb": rate_ucb, "lcb": rate_lcb},
        "contract_savings_lcb": savings,
        "contract_cost": cost,
        "safety_clear": safety_clear,
        "harm": harm,
        "decision": decision,
        "maturity": {
            "observed_orders": len(rows),
            "excluded": excluded,
        },
    }


def run_review_plan(ledger: Ledger) -> dict:
    """按登记的审阅计划逐轮取账；每轮冻结自己的口径。"""
    looks_out = []
    cumulative = 0.0
    candidates_by_group = {}
    for cand in ledger.candidates:
        for group, combo in ledger.combo_groups.items():
            if tuple(combo) == tuple(cand["combo"]):
                candidates_by_group.setdefault(group, []).append(cand)

    for look in sorted(ledger.review_plan, key=lambda r: r.as_of):
        blocks = set(look.eligible_blocks)
        # 联合置信：该轮所有同期候选与政策判断共用一个预算，按数量均摊。
        family_size = _family_size(ledger, blocks, look.scope_groups)
        alpha_per = look.shared_alpha / family_size if family_size else look.shared_alpha

        groups = {}
        for group in look.scope_groups:
            cands = candidates_by_group.get(group, [])
            rate_limit = min((c["damage_rate_limit"] for c in cands), default=None)
            result = evaluate_group(
                ledger, group, blocks, look.as_of,
                alpha_per, look.damage_increase_limit, rate_limit,
                candidate_carriers={c["carrier"] for c in cands} or None,
            )
            # 候选扩围签核：安全清仓只是前提，还要该候选承运口径上节省成立。
            result["candidates"] = []
            for cand in cands:
                lcb = result["contract_savings_lcb"].get(cand["carrier"])
                cand_rate_ok = (
                    result["combined_abs_rate"]["ucb"] is not None
                    and result["combined_abs_rate"]["ucb"] <= cand["damage_rate_limit"]
                )
                expandable = (
                    result["decision"] != PAUSE
                    and result["safety_clear"]
                    and cand_rate_ok
                    and lcb is not None and lcb > 0
                )
                # 缺口说明：哪一类订单和条件能补上证据缺口
                gaps = []
                q1_ucb = result["q1_damage"]["ucb"]
                q2_ucb = result["q2_damage"]["ucb"]
                if q1_ucb is None or q1_ucb > result["damage_increase_limit"]:
                    gaps.append(
                        f"问题一破损差上界 {_fmt4(q1_ucb)} 未降到 "
                        f"{result['damage_increase_limit']} 以下，"
                        "需要更多覆盖两种分派的成熟订单"
                    )
                if q2_ucb is None or q2_ucb > result["damage_increase_limit"]:
                    gaps.append(
                        f"问题二上界 {_fmt4(q2_ucb)} 未降到 "
                        f"{result['damage_increase_limit']} 以下，"
                        "需要两种占比下都成熟的分箱班组订单"
                    )
                if not cand_rate_ok:
                    gaps.append(
                        f"合箱绝对破损率上界 {_fmt4(result['combined_abs_rate']['ucb'])} "
                        f"高于生产上限 {cand['damage_rate_limit']}"
                    )
                if lcb is None or lcb <= 0:
                    gaps.append(
                        f"{cand['carrier']} 合同口径箱体节省不成立"
                        f"（下界 {_fmt4(lcb)}），现有档位下无箱体来源的节省"
                    )
                result["candidates"].append({
                    "id": cand["id"],
                    "carrier": cand["carrier"],
                    "station": cand["station"],
                    "workbench": cand["workbench"],
                    "carton": cand["carton"],
                    "projected_orders": cand["projected_orders"],
                    "damage_rate_limit": cand["damage_rate_limit"],
                    "savings_lcb_minor": lcb,
                    "rate_ok": cand_rate_ok,
                    "expandable": expandable,
                    "gaps": gaps,
                })
            groups[group] = result

        cumulative += look.shared_alpha
        looks_out.append({
            "look": look.look,
            "as_of": look.as_of,
            "eligible_blocks": sorted(blocks),
            "shared_alpha": look.shared_alpha,
            "family_size": family_size,
            "alpha_per_quantity": alpha_per,
            "cumulative_alpha": cumulative,
            "groups": groups,
        })

    return {
        "looks": looks_out,
        "cumulative_alpha": cumulative,
        "budget_note": (
            "各轮 shared_alpha 为该轮共用预算，轮内均摊；已过轮次的阈值冻结，"
            "不得把末轮单次标准回用到旧轮，也不得因数字好看追加计划外取账。"
        ),
    }


def recheck_signed_scopes(ledger: Ledger, today: str) -> list:
    """用当前资料复核历史签字；签字本身按当时证据解释，不消失。

    若当下证据不再支持，明确列出应撤出生产的操作条件。
    复核是监控读数，不占用审阅预算，也不构成新的正式轮次。
    """
    out = []
    all_blocks = {o.block for o in ledger.orders.values()}
    for scope in ledger.signed_scopes:
        cand = next((c for c in ledger.candidates if c["id"] == scope.get("candidate")), None)
        group = None
        if cand is not None:
            for g, combo in ledger.combo_groups.items():
                if tuple(combo) == tuple(cand["combo"]):
                    group = g
        # 签字后落在决策订单上的新事实
        new_facts = []
        split_damage_recheck = False
        views = collect_views(ledger, scope.get("decision_orders", []), today)
        for oid, view in views.items():
            for claim_id in view.adjudicated_claims:
                claim = next(c for c in ledger.claims_by_bill[view.bill] if c.id == claim_id)
                if claim.adjudicated > scope["issued"]:
                    new_facts.append(f"订单 {oid} 的破损 {claim_id} 于 {claim.adjudicated} 核认")
                    if ledger.orders[oid].choice == "split":
                        split_damage_recheck = True
            for claim_id in view.pending_claims:
                claim = next(c for c in ledger.claims_by_bill[view.bill] if c.id == claim_id)
                if claim.reported > scope["issued"]:
                    new_facts.append(f"订单 {oid} 的破损 {claim_id} 仍未核认")
        for amendment in ledger.amendments:
            if amendment.charged_on > scope["issued"]:
                for oid, view in views.items():
                    if view.bill == amendment.bill:
                        new_facts.append(
                            f"运单 {amendment.bill} 账单订正 {amendment.id} "
                            f"于 {amendment.charged_on} 到账（{amendment.amount_minor} 分）"
                        )

        recheck = {
            "scope_id": scope["id"],
            "issued": scope["issued"],
            "candidate": scope.get("candidate"),
            "group": group,
            "new_facts_since_issued": new_facts,
            "exit_conditions": [],
            "still_supported": True,
            "note": "历史签字按签发时证据解释；以下为当前资料下的复核读数。",
        }
        if group is not None and ledger.design:
            latest = max(ledger.review_plan, key=lambda r: r.as_of, default=None)
            family_size = _family_size(ledger, all_blocks, [group])
            alpha_per = (latest.shared_alpha / family_size) if latest else 0.005
            result = evaluate_group(ledger, group, all_blocks, today, alpha_per,
                                    latest.damage_increase_limit if latest else 0.04,
                                    cand["damage_rate_limit"] if cand else None)
            recheck["current_read"] = {
                "q1_damage_ucb": result["q1_damage"]["ucb"],
                "q2_damage_ucb": result["q2_damage"]["ucb"],
                "abs_rate_ucb": result["combined_abs_rate"]["ucb"],
                "savings_lcb": result["contract_savings_lcb"],
            }
            if split_damage_recheck:
                q2_ucb = result["q2_damage"]["ucb"]
                if q2_ucb is not None:
                    note = (f"分箱运单破损核认落地，已重看合箱推行对分箱班组的压力："
                            f"问题二当前上界 {q2_ucb:.4f}")
                else:
                    note = "分箱运单破损核认落地，问题二当前不可估"
                recheck["new_facts_since_issued"].append(note)
            if result["harm"]:
                recheck["exit_conditions"].append(
                    f"{group} 组合在 {cand['station']}/{cand['carrier']}/"
                    f"{cand['workbench']}/{cand['carton']} 的生产条件"
                    if cand else f"{group} 组合的生产条件"
                )
            elif cand is not None:
                lcb = result["contract_savings_lcb"].get(cand["carrier"])
                if lcb is None or lcb <= 0:
                    recheck["exit_conditions"].append(
                        f"{cand['carrier']} 合同口径下箱体节省不成立，"
                        f"{cand['id']} 的扩围理由需重签"
                    )
        elif cand is not None:
            # 旧账没有分派证据：只看候选条件下当前破损与成熟口径。
            ids = [oid for oid, o in ledger.orders.items()
                   if o.station == cand["station"] and o.carrier == cand["carrier"]
                   and tuple(o.combo) == tuple(cand["combo"])]
            cond_views = collect_views(ledger, ids, today)
            damaged = sorted(oid for oid, v in cond_views.items()
                             if v.damage_state == "damaged")
            pending = sorted(oid for oid, v in cond_views.items()
                             if v.damage_state == "pending")
            immature = sorted(oid for oid, v in cond_views.items()
                              if v.damage_state == "unobserved")
            recheck["current_read"] = {
                "condition_orders": len(ids),
                "damaged": damaged, "pending": pending, "unobserved": immature,
            }
            if damaged:
                recheck["exit_conditions"].append(
                    f"{cand['id']} 的 {cand['carrier']}/{cand['station']}/"
                    f"{cand['workbench']} 条件出现已核认破损（{', '.join(damaged)}），"
                    "该操作条件应撤出生产"
                )
            if pending:
                recheck["exit_conditions"].append(
                    f"{cand['id']} 的 {cand['carrier']} 条件有待核认破损"
                    f"（{', '.join(pending)}），核认前破损保证不成立"
                )
        recheck["still_supported"] = not recheck["exit_conditions"]
        out.append(recheck)
    return out
