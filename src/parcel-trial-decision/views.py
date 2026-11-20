"""按取账日还原每个订单当时可见的事实。

原则：
- 未成熟的售后窗口不能算成没有破损。
- 只覆盖部分商品的关窗，只让被覆盖的物品成熟。
- 已报告未核认的破损是“待定”，既不是破损也不是无破损。
- 合在同一张运单的破损只记到订单一次，不复制给每箱物品；
  无法定位到具体物品的破损停留在运单（订单）层级。
"""

from __future__ import annotations

from dataclasses import dataclass

from .billing import effective_invoice, order_contract_cost
from .ledger import Ledger


# 破损观察状态
DAMAGED = "damaged"            # 已有核认破损
PENDING = "pending"            # 已报告未核认
MATURE_CLEAN = "mature_clean"  # 全部物品窗口已关且无核认破损
UNOBSERVED = "unobserved"      # 窗口未关或只覆盖部分物品


@dataclass(frozen=True)
class OrderView:
    order_id: str
    assigned: str
    executed: str | None
    compliant: bool | None
    bill: str | None
    carrier: str
    contract_period: str | None
    maturity: str              # full / partial / open
    covered_items: frozenset
    damage_state: str
    damage: bool | None        # True 核认破损；False 成熟且无破损；None 不可判
    pending_claims: tuple
    adjudicated_claims: tuple
    invoiced_minor: int | None
    contract_minor: int | None


def order_view(ledger: Ledger, order_id: str, cut: str) -> OrderView:
    order = ledger.orders[order_id]
    pkgs = ledger.packages_of(order_id)
    executed = ledger.executed(order_id)
    compliant = None if executed is None else executed == order.choice
    bill = ledger.bill_of(order_id)

    closure = ledger.closures_by_bill.get(bill) if bill else None
    if closure is not None and closure.closed_on <= cut:
        covered = frozenset(closure.covered) & frozenset(order.items)
    else:
        covered = frozenset()
    if covered >= frozenset(order.items):
        maturity = "full"
    elif covered:
        maturity = "partial"
    else:
        maturity = "open"

    adjudicated, pending = [], []
    for claim in ledger.claims_by_bill.get(bill, []):
        if claim.reported > cut:
            continue
        if claim.adjudicated and claim.adjudicated <= cut:
            adjudicated.append(claim.id)
        else:
            pending.append(claim.id)

    if adjudicated:
        damage_state, damage = DAMAGED, True
    elif pending:
        damage_state, damage = PENDING, None
    elif maturity == "full":
        damage_state, damage = MATURE_CLEAN, False
    else:
        damage_state, damage = UNOBSERVED, None

    invoiced = effective_invoice(ledger, bill, cut) if bill else None
    contract, period = order_contract_cost(ledger, order_id)

    return OrderView(
        order_id=order_id,
        assigned=order.choice,
        executed=executed,
        compliant=compliant,
        bill=bill,
        carrier=order.carrier,
        contract_period=period,
        maturity=maturity,
        covered_items=covered,
        damage_state=damage_state,
        damage=damage,
        pending_claims=tuple(pending),
        adjudicated_claims=tuple(adjudicated),
        invoiced_minor=invoiced[0] if invoiced else None,
        contract_minor=contract,
    )


def collect_views(ledger: Ledger, order_ids, cut: str) -> dict:
    return {oid: order_view(ledger, oid, cut) for oid in order_ids}
