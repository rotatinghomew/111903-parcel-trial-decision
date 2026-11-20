"""承运计费：计费重、落档与账单口径。

规则（与 README 一致）：
- 包裹计费重取实重与体积重较大者，体积重 = 立方厘米 / 合同除数。
- 按包裹计费时逐件落档；按运单计费时用同一运单内计费重合计落档一次。
- 没有覆盖该重量的档位时返回 None（无适用合同报价），不推定超重费。
- 合同期起止日均含当日。
- 账单订正取代被订正条目，不是额外收费。
"""

from __future__ import annotations

from .ledger import Ledger, Package, Tariff


def volumetric_kg(size_cm: tuple, divisor_cm3_kg: float) -> float:
    volume = size_cm[0] * size_cm[1] * size_cm[2]
    return volume / divisor_cm3_kg


def billable_kg(pkg: Package, divisor_cm3_kg: float) -> float:
    return max(pkg.weight_kg, volumetric_kg(pkg.size_cm, divisor_cm3_kg))


def tariff_on(tariffs: list, carrier: str, day: str) -> Tariff | None:
    """发运当日适用的合同期；起止日均含当日。"""
    for tariff in tariffs:
        if tariff.carrier == carrier and tariff.from_day <= day <= tariff.to_day:
            return tariff
    return None


def bracket_fee(tariff: Tariff, weight_kg: float) -> int | None:
    """按含端点的上限落档；无覆盖档位时不推定超重费。"""
    for cap, fee in sorted(tariff.brackets):
        if weight_kg <= cap:
            return fee
    return None


def contract_cost(packages: list, tariff: Tariff) -> int | None:
    """同一合同下、按实际装箱口径计算的合同可比成本（分）。"""
    if tariff.billing_unit == "waybill":
        total = sum(billable_kg(p, tariff.divisor_cm3_kg) for p in packages)
        return bracket_fee(tariff, total)
    if tariff.billing_unit == "parcel":
        amount = 0
        for pkg in packages:
            fee = bracket_fee(tariff, billable_kg(pkg, tariff.divisor_cm3_kg))
            if fee is None:
                return None
            amount += fee
        return amount
    raise ValueError(f"未知计费单位 {tariff.billing_unit}")


def order_contract_cost(ledger: Ledger, order_id: str) -> tuple[int | None, str | None]:
    """订单的合同可比成本与所用合同期起点；无适用报价时成本为 None。"""
    pkgs = ledger.packages_of(order_id)
    if not pkgs:
        return None, None
    tariff = tariff_on(ledger.tariffs, pkgs[0].carrier, pkgs[0].shipped)
    if tariff is None:
        return None, None
    return contract_cost(pkgs, tariff), tariff.from_day


def effective_invoice(ledger: Ledger, bill: str, cut: str) -> tuple[int, str] | None:
    """取账日 cut 可见的实付：订正已到时以订正为准（取代而非累加）。"""
    base = ledger.invoice_by_bill.get(bill)
    if base is None or base.charged_on > cut:
        return None
    amount, source = base.amount_minor, base.id
    for amendment in ledger.amendments:
        if amendment.supersedes == base.id and amendment.charged_on <= cut:
            amount, source = amendment.amount_minor, amendment.id
    return amount, source
