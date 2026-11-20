"""台账载入、索引与完整性核验。

只登记事实，不做估计。关键约束：
- 二次拆分不是新订单：同一订单的包裹物品合集必须等于订单物品。
- 同一订单只挂在一张运单上。
- 账单订正引用旧条目，不是额外收费。
- 合在同一张运单的破损只记到运单，不复制给每箱物品。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import json
from pathlib import Path


@dataclass(frozen=True)
class Order:
    id: str
    block: str
    team: str
    choice: str            # 分派：split / combined
    eligible: bool
    combo: tuple
    items: tuple
    station: str
    assigned_at: str
    carrier: str
    zone: str | None = None


@dataclass(frozen=True)
class Package:
    id: str
    order: str
    items: tuple
    bill: str
    carrier: str
    shipped: str
    delivered: str | None
    weight_kg: float
    size_cm: tuple


@dataclass(frozen=True)
class Invoice:
    id: str
    bill: str
    amount_minor: int
    charged_on: str


@dataclass(frozen=True)
class Amendment:
    id: str
    supersedes: str
    bill: str
    amount_minor: int
    charged_on: str


@dataclass(frozen=True)
class Closure:
    id: str
    bill: str
    covered: tuple
    closed_on: str
    status: str


@dataclass(frozen=True)
class Claim:
    id: str
    bill: str
    items: tuple          # 空元组表示已报告但无法定位到具体物品
    reported: str
    adjudicated: str | None


@dataclass(frozen=True)
class Tariff:
    carrier: str
    from_day: str
    to_day: str
    divisor_cm3_kg: float
    billing_unit: str     # waybill / parcel
    brackets: tuple       # ((上限kg, 收费分), ...)，含端点


@dataclass(frozen=True)
class ZoneDesign:
    block: str
    zone: str
    station: str
    workbench: str
    assigned_day: str
    teams: tuple
    treated: tuple
    quota: int


@dataclass(frozen=True)
class Look:
    look: str
    as_of: str
    eligible_blocks: tuple
    scope_groups: tuple
    shared_alpha: float
    damage_increase_limit: float


@dataclass
class Ledger:
    """一份试验台账（订单、包裹、账单、售后、合同与现场计划）。"""

    name: str
    orders: dict
    packages_by_order: dict
    invoices_by_id: dict
    invoice_by_bill: dict
    amendments: list
    closures_by_bill: dict
    claims_by_bill: dict
    tariffs: list
    design: dict                 # zone -> ZoneDesign，无现场计划时为空
    review_plan: list
    candidates: list
    available: list
    pilot_bookings: list
    signed_scopes: list
    combo_groups: dict
    future_zones: list
    as_of: str | None
    notices: list = field(default_factory=list)

    def packages_of(self, order_id: str) -> list:
        return self.packages_by_order.get(order_id, [])

    def executed(self, order_id: str) -> str | None:
        """实际装箱口径：一件包裹为合箱，多件为分箱。不生成新订单。"""
        pkgs = self.packages_of(order_id)
        if not pkgs:
            return None
        return "combined" if len(pkgs) == 1 else "split"

    def bill_of(self, order_id: str) -> str | None:
        pkgs = self.packages_of(order_id)
        return pkgs[0].bill if pkgs else None

    def latest_day(self) -> str:
        """台账中已可见的最后事实日期（用于“当前证据”口径）。"""
        days = []
        for inv in self.invoices_by_id.values():
            days.append(inv.charged_on)
        days += [a.charged_on for a in self.amendments]
        for clo in self.closures_by_bill.values():
            days.append(clo.closed_on)
        for claims in self.claims_by_bill.values():
            for c in claims:
                days.append(c.reported)
                if c.adjudicated:
                    days.append(c.adjudicated)
        if self.as_of:
            days.append(self.as_of)
        return max(days)


def _as_tuple(value):
    return tuple(value) if isinstance(value, list) else value


def load_ledger(path: str | Path, name: str | None = None) -> Ledger:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    orders = {}
    for row in raw.get("orders", []):
        orders[row["id"]] = Order(
            id=row["id"], block=row["block"], team=row["team"],
            choice=row["choice"], eligible=bool(row.get("eligible", True)),
            combo=tuple(row["combo"]), items=tuple(row["items"]),
            station=row["station"], assigned_at=row["assigned_at"],
            carrier=row["carrier"], zone=row.get("zone"),
        )

    packages_by_order = {}
    for row in raw.get("packages", []):
        pkg = Package(
            id=row["id"], order=row["order"], items=tuple(row["items"]),
            bill=row["bill"], carrier=row["carrier"], shipped=row["shipped"],
            delivered=row.get("delivered"), weight_kg=float(row["weight_kg"]),
            size_cm=tuple(row["size_cm"]),
        )
        packages_by_order.setdefault(pkg.order, []).append(pkg)

    invoices_by_id = {}
    invoice_by_bill = {}
    for row in raw.get("invoices", []):
        inv = Invoice(id=row["id"], bill=row["bill"],
                      amount_minor=int(row["amount_minor"]),
                      charged_on=row["charged_on"])
        invoices_by_id[inv.id] = inv
        invoice_by_bill[inv.bill] = inv

    amendments = [
        Amendment(id=a["id"], supersedes=a["supersedes"], bill=a["bill"],
                  amount_minor=int(a["amount_minor"]), charged_on=a["charged_on"])
        for a in raw.get("invoice_amendments", [])
    ]

    closures_by_bill = {}
    for row in raw.get("claim_closures", []):
        closures_by_bill[row["bill"]] = Closure(
            id=row["id"], bill=row["bill"], covered=tuple(row["covered"]),
            closed_on=row["closed_on"], status=row.get("status", ""),
        )

    claims_by_bill = {}
    for row in raw.get("claim_events", []):
        claim = Claim(
            id=row["id"], bill=row["bill"],
            items=tuple(row["items"]) if row.get("items") else tuple(),
            reported=row["reported"], adjudicated=row.get("adjudicated"),
        )
        claims_by_bill.setdefault(claim.bill, []).append(claim)

    tariffs = [
        Tariff(carrier=t["carrier"], from_day=t["from_day"], to_day=t["to_day"],
               divisor_cm3_kg=float(t["divisor_cm3_kg"]),
               billing_unit=t["billing_unit"],
               brackets=tuple((float(cap), int(fee)) for cap, fee in t["brackets_minor"]))
        for t in raw.get("tariffs", [])
    ]

    design = {}
    for row in raw.get("design", []):
        design[row["zone"]] = ZoneDesign(
            block=row["block"], zone=row["zone"], station=row["station"],
            workbench=row["workbench"], assigned_day=row["assigned_day"],
            teams=tuple(row["teams"]), treated=tuple(row["treated"]),
            quota=int(row["quota"]),
        )

    review_plan = [
        Look(look=r["look"], as_of=r["as_of"],
             eligible_blocks=tuple(r["eligible_blocks"]),
             scope_groups=tuple(r["scope_groups"]),
             shared_alpha=float(r["shared_alpha"]),
             damage_increase_limit=float(r["damage_increase_limit"]))
        for r in raw.get("review_plan", [])
    ]

    ledger = Ledger(
        name=name or Path(path).stem,
        orders=orders,
        packages_by_order=packages_by_order,
        invoices_by_id=invoices_by_id,
        invoice_by_bill=invoice_by_bill,
        amendments=amendments,
        closures_by_bill=closures_by_bill,
        claims_by_bill=claims_by_bill,
        tariffs=tariffs,
        design=design,
        review_plan=review_plan,
        candidates=list(raw.get("candidates", [])),
        available=list(raw.get("available", [])),
        pilot_bookings=list(raw.get("pilot_bookings", [])),
        signed_scopes=list(raw.get("signed_scopes", [])),
        combo_groups={k: list(v) for k, v in raw.get("combo_groups", {}).items()},
        future_zones=list(raw.get("future_zones", [])),
        as_of=raw.get("as_of"),
    )
    ledger.notices.extend(_integrity_notices(ledger))
    return ledger


def _integrity_notices(ledger: Ledger) -> list:
    """核验台账内部一致性；问题只登记，不静默改写。"""
    notes = []
    for oid, order in ledger.orders.items():
        pkgs = ledger.packages_of(oid)
        if not pkgs:
            notes.append(f"订单 {oid} 没有包裹记录")
            continue
        shipped_items = sorted(it for p in pkgs for it in p.items)
        if sorted(order.items) != shipped_items:
            notes.append(f"订单 {oid} 的包裹物品与分派物品不一致")
        if len({p.bill for p in pkgs}) != 1:
            notes.append(f"订单 {oid} 挂在多张运单上")
        if len(pkgs) > 1 and order.choice == "combined":
            notes.append(
                f"订单 {oid} 分派合箱但实际拆成 {len(pkgs)} 件（执行偏差，仍属原订单）"
            )
    for a in ledger.amendments:
        base = ledger.invoices_by_id.get(a.supersedes)
        if base is None:
            notes.append(f"订正 {a.id} 引用的旧账单 {a.supersedes} 不存在")
        elif base.bill != a.bill:
            notes.append(f"订正 {a.id} 与被订正账单的运单不一致")
    for oid, pkgs in ledger.packages_by_order.items():
        bill = pkgs[0].bill
        clo = ledger.closures_by_bill.get(bill)
        if clo is None:
            continue
        expect = (date.fromisoformat(pkgs[0].shipped) + timedelta(days=10)).isoformat()
        if clo.closed_on != expect:
            notes.append(f"运单 {bill} 关窗日 {clo.closed_on} 与发运后第十天 {expect} 不符")
    for zone in ledger.design.values():
        if len(zone.treated) != zone.quota:
            notes.append(f"作业区 {zone.zone} 抽中班组数与 quota 不符")
        if not set(zone.treated) <= set(zone.teams):
            notes.append(f"作业区 {zone.zone} 抽中班组不在名单内")
    for oid, order in ledger.orders.items():
        if order.zone and order.zone in ledger.design:
            zone = ledger.design[order.zone]
            if order.team not in zone.teams:
                notes.append(f"订单 {oid} 的班组不在作业区 {order.zone} 名单内")
            expect_choice = "combined" if order.team in zone.treated else "split"
            if order.choice != expect_choice:
                notes.append(f"订单 {oid} 的分派与抽签结果不一致")
    return notes
