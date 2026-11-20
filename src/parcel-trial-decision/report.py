"""汇总报告：机器可读字典 + 文本渲染。

回答三个问题：
- 特定组合在何种线路、合同和成熟进度下可做决定；
- 哪些费用差异来自箱体变化；
- 哪批后续数据会改变用箱建议。
"""

from __future__ import annotations

from .ledger import Ledger
from .oldaccount import old_account_report
from .plan import plan_next_wave
from .review import recheck_signed_scopes, run_review_plan
from .views import collect_views


def order_trace(ledger: Ledger, cut: str) -> list:
    """每个订单的去向：分派、执行、运单与取账日的售后口径。"""
    views = collect_views(ledger, ledger.orders.keys(), cut)
    rows = []
    for oid, order in sorted(ledger.orders.items()):
        view = views[oid]
        rows.append({
            "order": oid,
            "block": order.block,
            "zone": order.zone,
            "team": order.team,
            "combo": list(order.combo),
            "carrier": order.carrier,
            "assigned": view.assigned,
            "executed": view.executed,
            "compliant": view.compliant,
            "bill": view.bill,
            "maturity": view.maturity,
            "damage_state": view.damage_state,
            "invoiced_minor": view.invoiced_minor,
            "contract_minor": view.contract_minor,
        })
    return rows


def _open_items(field: Ledger, route: Ledger, field_report: dict, route_report: dict) -> list:
    items = []
    if field is not None:
        latest = field_report["review"]["looks"][-1]
        cut = latest["as_of"]
        views = collect_views(field, field.orders.keys(), cut)
        immature = sorted(oid for oid, v in views.items() if v.maturity != "full"
                          and v.damage_state == "unobserved")
        pending = sorted(oid for oid, v in views.items() if v.damage_state == "pending")
        if immature:
            blocks = sorted({field.orders[o].block for o in immature})
            items.append(
                f"现场试验 {len(immature)} 个订单在 {cut} 未成熟（批次 {', '.join(blocks)}）；"
                f"其关窗登记后可进入以后各轮，但不写回已过轮次"
            )
        if pending:
            items.append(f"现场试验 {len(pending)} 个订单有待核认破损：{', '.join(pending)}")
        for entry in field_report["next_wave"]["unplaced"]:
            items.append(
                f"候选 {entry['candidate']} 本轮排不进抽签：{'; '.join(entry['reasons'])}"
            )
        items.append("审阅计划只登记到 "
                     + latest["look"] + "；之后的再读取须先登记新轮次与预算，"
                     "不能套用末轮单次标准")
    if route is not None:
        damage = route_report["old_account"]["damage"]
        pending = [c for c in damage["claims"]
                   if c["visible_at_cut"] and not c["adjudicated_by_cut"]]
        for claim in pending:
            items.append(
                f"旧账运单 {claim['bill']} 的破损 {claim['claim']} 仍未核认，"
                f"核认后重算相关候选的破损口径"
            )
        if damage["bills_not_fully_mature"]:
            items.append(
                f"旧账 {len(damage['bills_not_fully_mature'])} 张运单关窗未登记或未全覆盖，"
                f"成熟回流后候选范围与优先次序要重算"
            )
    return items


def _tables(field: Ledger) -> list:
    """现场计划表：每批怎么抽、班组名单、谁共用一张操作台。"""
    rows = []
    for zone in sorted(field.design.values(), key=lambda z: (z.block, z.zone)):
        rows.append({
            "block": zone.block,
            "zone": zone.zone,
            "station": zone.station,
            "workbench": zone.workbench,
            "quota": zone.quota,
            "teams": list(zone.teams),
            "treated": list(zone.treated),
            "shared_table": list(zone.teams),
        })
    return rows


def build_report(field: Ledger | None, route: Ledger | None, today: str) -> dict:
    report = {"generated_on": today, "field_trial": None, "route_trial": None,
              "open_items": []}
    field_report = route_report = None

    if field is not None:
        review = run_review_plan(field)
        latest_cut = review["looks"][-1]["as_of"] if review["looks"] else field.latest_day()
        field_report = {
            "notices": field.notices,
            "design_tables": _tables(field),
            "review": review,
            "signed_scopes": recheck_signed_scopes(field, today),
            "next_wave": plan_next_wave(field),
            "order_trace": order_trace(field, latest_cut),
        }
        report["field_trial"] = field_report

    if route is not None:
        route_report = {
            "notices": route.notices,
            "old_account": old_account_report(route, today),
            "signed_scopes": recheck_signed_scopes(route, today),
            "order_trace": order_trace(route, today),
        }
        report["route_trial"] = route_report

    report["open_items"] = _open_items(field, route, field_report, route_report)
    return report


# ---------- 文本渲染 ----------

def _fmt(value, digits=4, na="—"):
    if value is None:
        return na
    if isinstance(value, float):
        rounded = round(value, digits)
        if rounded == 0:
            rounded = 0.0  # 避免 -0.0
        return f"{rounded:.{digits}f}"
    return str(value)


def _decision_label(decision: str) -> str:
    return {"observe": "仅保留观察", "ready": "证据足以扩围",
            "pause": "暂停受影响生产范围"}[decision]


_STATE_CN = {"damaged": "已核认破损", "pending": "待核认",
             "mature_clean": "成熟无破损", "unobserved": "未成熟或未全覆盖"}


def _states_cn(states: dict) -> str:
    parts = [f"{_STATE_CN.get(k, k)} {v}" for k, v in sorted(states.items())]
    return "、".join(parts) if parts else "无"


def _render_group(lines, name: str, group: dict):
    q1, q2 = group["q1_damage"], group["q2_damage"]
    rate = group["combined_abs_rate"]
    lines.append(f"  组合 {name}（{'+'.join(group['combo'])}）")
    lines.append(
        f"    问题一·分派口径破损差(合-分) {_fmt(q1['itt']['effect'] if q1['itt'] else None)}"
        f"，上界 {_fmt(q1['ucb'])}（允许新增 {group['damage_increase_limit']}）；"
        f"独立分派单位 {q1['itt']['n_clusters'] if q1['itt'] else 0} 个班组"
    )
    pp = q1.get("per_protocol")
    if pp:
        lines.append(
            f"    问题一·执行口径对照读数 {_fmt(pp['effect'])}"
            f"（含自选偏差，不作分派证据）；临时拆箱 {len(q1['non_compliant_orders'])} 单"
        )
    lines.append(
        f"    问题二·分箱班组在高/低占比区破损差 {_fmt(q2['itt']['effect'] if q2['itt'] else None)}"
        f"，上界 {_fmt(q2['ucb'])}；独立分派单位 {q2['itt']['n_clusters'] if q2['itt'] else 0} 个作业区"
    )
    lines.append(
        f"    合箱绝对破损率上界 {_fmt(rate['ucb'])}"
        f"（生产上限 {_fmt(group['damage_rate_limit'], 2)}）"
    )
    for carrier, lcb in group["contract_savings_lcb"].items():
        lines.append(f"    {carrier} 合同口径箱体节省下界 {_fmt(lcb, 1)} 分/单")
    if q1["zones_dropped_positivity"]:
        lines.append(f"    可及性：作业区 {', '.join(q1['zones_dropped_positivity'])} "
                     "在该口径下只剩一臂，未纳入对照，也不跨区借对照")
    if q2["blocks_missing_contrast"]:
        lines.append(f"    可及性：批次 {', '.join(q2['blocks_missing_contrast'])} "
                     "的占比水平不齐，问题二不含这些批次")
    lines.append(f"    结论：{_decision_label(group['decision'])}"
                 + (f"；原因：{'；'.join(group['harm'])}" if group["harm"] else ""))
    for cand in group.get("candidates", []):
        lines.append(
            f"    候选 {cand['id']}（{cand['carrier']}/{cand['workbench']}/{cand['carton']}，"
            f"预计 {cand['projected_orders']} 单）："
            + ("可扩围" if cand["expandable"] else "证据不足，不可扩围")
        )
        for gap in cand.get("gaps", []):
            if not cand["expandable"]:
                lines.append(f"      缺口：{gap}")


def render_text(report: dict) -> str:
    lines = [f"合箱试验审阅与扩围核对（生成日 {report['generated_on']}）", ""]

    field = report.get("field_trial")
    if field:
        lines.append("一、现场试验（两阶段分派）")
        if field["notices"]:
            lines.append(f"  台账提示 {len(field['notices'])} 条，其中执行偏差"
                         f" {sum(1 for n in field['notices'] if '执行偏差' in n)} 条（明细见 JSON）")
        for look in field["review"]["looks"]:
            lines.append(
                f"  轮次 {look['look']}：取账 {look['as_of']}，"
                f"批次 {', '.join(look['eligible_blocks'])}；"
                f"本轮预算 {look['shared_alpha']}，{look['family_size']} 个审阅量均摊，"
                f"每个 {look['alpha_per_quantity']:.5f}；累计占用 {look['cumulative_alpha']}"
            )
            for name, group in look["groups"].items():
                _render_group(lines, name, group)
        lines.append(f"  预算纪律：{field['review']['budget_note']}")
        for scope in field["signed_scopes"]:
            lines.append(
                f"  签字复核 {scope['scope_id']}（{scope['issued']} 签发，候选 {scope['candidate']}）："
                f"当前{'仍支持' if scope['still_supported'] else '不再支持'}"
            )
            for fact in scope["new_facts_since_issued"]:
                lines.append(f"    签发后新事实：{fact}")
            for cond in scope["exit_conditions"]:
                lines.append(f"    应撤出生产：{cond}")
        wave = field["next_wave"]
        lines.append("  下一轮试用计划：")
        for p in wave["placements"]:
            tariff = p["tariff"]
            tariff_note = ("当日合同档位覆盖" if tariff["tariff_covers_day"]
                           and tariff["bracket_covers_carton"] else "当日合同档位需复核")
            lines.append(
                f"    {p['candidate']} → {p['zone']}（{p['workbench']} {p['day']}），"
                f"抽 {p['treated_draw']} 个合箱班组，余 {p['control_left']} 个分箱对照；"
                f"候选台面{'一致' if p['workbench_match'] else '不一致，需签核'}；{tariff_note}"
            )
        for u in wave["unplaced"]:
            lines.append(f"    {u['candidate']} 排不进：{'; '.join(u['reasons'])}")
        for note in wave["notes"]:
            lines.append(f"    注意：{note}")
        lines.append("")

    route = report.get("route_trial")
    if route:
        lines.append("二、旧账（无现场分派计划，仅执行与成本）")
        lines.append(f"  {route['old_account']['disclaimer']}")
        for row in route["old_account"]["execution_cost"]:
            line = (
                f"  {row['carrier']} 合同期 {row['contract_period']} 起 "
                f"{'合箱' if row['executed'] == 'combined' else '分箱'}："
                f"合同可比 {_fmt(row['contract_mean_minor'], 1)} 分，"
                f"实付 {_fmt(row['invoiced_mean_minor'], 1)} 分，n={row['n']}"
            )
            if row["executed"] == "combined" and row["box_driven_delta_minor"] is not None:
                line += f"，合箱相对分箱的箱体差 {_fmt(row['box_driven_delta_minor'], 1)} 分"
            lines.append(line)
        damage = route["old_account"]["damage"]
        lines.append(f"  售后口径（{damage['cut']}）：{_states_cn(damage['damage_states'])}；"
                     f"未完全成熟运单 {len(damage['bills_not_fully_mature'])} 张")
        lines.append(f"  临时拆箱（执行偏差）{len(route['old_account']['non_compliant_orders'])} 单："
                     + ", ".join(route["old_account"]["non_compliant_orders"]))
        for scope in route["signed_scopes"]:
            lines.append(
                f"  签字复核 {scope['scope_id']}（{scope['issued']} 签发，候选 {scope['candidate']}）"
            )
            for fact in scope["new_facts_since_issued"]:
                lines.append(f"    签发后新事实：{fact}")
            for cond in scope["exit_conditions"]:
                lines.append(f"    应撤出生产：{cond}")
        for cand in route["old_account"]["candidates"]:
            lines.append(
                f"  候选 {cand['candidate']}（{cand['carrier']}/{cand['workbench']}，"
                f"预计 {cand['projected_orders']} 单）："
                f"台位{'够' if cand['capacity_ok'] else '不够'}；"
                f"破损口径：{_states_cn(cand['damage_states'])}"
            )
            for row in cand["capacity_rows"]:
                if not row["combined_demand_ok"]:
                    lines.append(
                        f"    注意：{row['workbench']} {row['date']} 同台候选合计 "
                        f"{row['combined_demand']} 单，超过剩余台位 {row['remaining_slots']}"
                        f"/箱料 {row['remaining_carton']}，接不住全部候选"
                    )
        lines.append("")

    if report["open_items"]:
        lines.append("三、会改变建议的后续数据")
        for item in report["open_items"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)
