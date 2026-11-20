"""台位与下一轮试用分派计划。

规则：
- 已确认的试用预订先扣除台位与箱料；已确认班组锁定，
  工具不会静默挪用，明确取消才可释放。
- 名单交叠的班组在同一批抽签中只能出现在一个作业区。
- 每个作业区必须保住有意义的分派对照：抽中 quota 个合箱班组后，
  至少要剩一个分箱班组；抽不出对照的排法不成立。
- 候选的站点条件不跨站安排；不把候选全塞进一张共享台面或一种占比。
"""

from __future__ import annotations

from datetime import date

from .billing import billable_kg, bracket_fee, tariff_on
from .ledger import Ledger

_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}


def weekday_match(cand: dict, day: str) -> bool | None:
    """候选登记的星期条件与计划日是否一致。"""
    want = cand.get("weekday")
    if not want:
        return None
    return _WEEKDAYS.get(want) == date.fromisoformat(day).isoweekday()


def _tariff_check(ledger: Ledger, cand: dict, day: str) -> dict:
    """当日承运合同是否覆盖候选箱型（大促档位核对）。"""
    tariff = tariff_on(ledger.tariffs, cand["carrier"], day)
    if tariff is None:
        return {"tariff_covers_day": False, "bracket_covers_carton": None}
    # 用该组合历史上最重的合箱包裹估计计费重是否落得下档位
    worst = 0.0
    for oid, order in ledger.orders.items():
        if tuple(order.combo) != tuple(cand["combo"]):
            continue
        pkgs = ledger.packages_of(oid)
        if len(pkgs) == 1:
            worst = max(worst, billable_kg(pkgs[0], tariff.divisor_cm3_kg))
    covers = bracket_fee(tariff, worst) is not None if worst else None
    return {"tariff_covers_day": True, "bracket_covers_carton": covers}


def capacity_snapshot(ledger: Ledger) -> list:
    """每个 (station, workbench, date) 的总量、已确认占用与剩余。"""
    rows = []
    for avail in ledger.available:
        key = (avail["station"], avail["workbench"], avail["date"])
        used_slots = used_carton = 0
        bookings = []
        for booking in ledger.pilot_bookings:
            if not booking.get("confirmed"):
                continue
            if (booking["workbench"], booking["date"]) == (key[1], key[2]):
                used_slots += booking["slots"]
                used_carton += booking["slots"]
                bookings.append(booking["id"])
        rows.append({
            "station": key[0], "workbench": key[1], "date": key[2],
            "carton": avail.get("carton"),
            "slots": avail["slots"], "carton_qty": avail["carton_qty"],
            "booked_slots": used_slots, "booked_carton": used_carton,
            "remaining_slots": avail["slots"] - used_slots,
            "remaining_carton": avail["carton_qty"] - used_carton,
            "confirmed_bookings": bookings,
        })
    return rows


def locked_teams(ledger: Ledger) -> dict:
    """已确认预订锁定的班组及其去向；挪用必须先明确取消。"""
    locked = {}
    for booking in ledger.pilot_bookings:
        if booking.get("confirmed"):
            locked[booking["team"]] = {
                "booking": booking["id"],
                "workbench": booking["workbench"],
                "date": booking["date"],
                "slots": booking["slots"],
            }
    return locked


def plan_next_wave(ledger: Ledger, candidate_ids: list | None = None) -> dict:
    """把候选排进 future_zones 的抽签；排不进的说清原因，不硬凑对照。"""
    locked = locked_teams(ledger)
    capacity = {(c["workbench"], c["date"]): c for c in capacity_snapshot(ledger)}

    candidates = [c for c in ledger.candidates
                  if candidate_ids is None or c["id"] in candidate_ids]
    # 大单先排，减少碎片
    candidates = sorted(candidates, key=lambda c: -c["projected_orders"])

    zones = []
    for z in ledger.future_zones:
        drawable = [t for t in z["teams"] if t not in locked]
        zones.append({
            "zone": z["zone"], "workbench": z["workbench"], "station": z["station"],
            "day": z["day"], "teams": list(z["teams"]),
            "available_quotas": list(z["available_quotas"]),
            "drawable": drawable,
            "locked_out": [t for t in z["teams"] if t in locked],
            "used": False,
        })

    placements, unplaced = [], []
    used_teams = set()
    for cand in candidates:
        quota = cand.get("quota")
        need = cand["projected_orders"]
        reasons = []
        chosen = None
        for zone in zones:
            if zone["used"]:
                continue
            if zone["station"] != cand["station"]:
                reasons.append(f"{zone['zone']} 站点 {zone['station']} 与候选条件 {cand['station']} 不符")
                continue
            if quota is not None and quota not in zone["available_quotas"]:
                reasons.append(f"{zone['zone']} 不接受合箱班组数 {quota}")
                continue
            free = [t for t in zone["drawable"] if t not in used_teams]
            if quota is not None and len(free) < quota + 1:
                reasons.append(
                    f"{zone['zone']} 可抽班组 {len(free)} 个，抽 {quota} 个合箱后"
                    f"剩 {len(free) - quota} 个，保不住分箱对照"
                )
                continue
            cap = capacity.get((zone["workbench"], zone["day"]))
            if cap is None:
                reasons.append(f"{zone['zone']} 当日无台位登记")
                continue
            if cap["remaining_slots"] < need or cap["remaining_carton"] < need:
                reasons.append(
                    f"{zone['zone']} 剩余台位 {cap['remaining_slots']}/箱料 "
                    f"{cap['remaining_carton']} 接不住 {need} 单"
                )
                continue
            chosen = (zone, free, cap)
            break
        if chosen is None:
            unplaced.append({"candidate": cand["id"], "quota": quota,
                             "projected_orders": need, "reasons": reasons})
            continue
        zone, free, cap = chosen
        zone["used"] = True
        used_teams.update(free)
        cap["remaining_slots"] -= need
        cap["remaining_carton"] -= need
        tariff = _tariff_check(ledger, cand, zone["day"])
        placements.append({
            "candidate": cand["id"],
            "zone": zone["zone"],
            "workbench": zone["workbench"],
            "station": zone["station"],
            "day": zone["day"],
            "quota": quota,
            "draw_pool": free,
            "treated_draw": quota,
            "control_left": len(free) - (quota or 0),
            "workbench_match": zone["workbench"] == cand.get("workbench"),
            "weekday_match": weekday_match(cand, zone["day"]),
            "tariff": tariff,
            "capacity_after": dict(cap),
        })

    quotas_placed = {p["quota"] for p in placements if p["quota"] is not None}
    notes = []
    if locked:
        for team, info in locked.items():
            notes.append(
                f"班组 {team} 已被确认预订 {info['booking']} 锁定"
                f"（{info['workbench']} {info['date']} {info['slots']} 台位），"
                f"明确取消前不可挪用"
            )
    if len(quotas_placed) < 2 and len({c.get("quota") for c in candidates}) >= 2:
        notes.append(
            "本轮只能排到一种合箱占比，单靠本轮回答不了不同共享压力下的对照问题"
        )
    benches = {p["workbench"] for p in placements}
    if len(placements) > 1 and len(benches) == 1:
        notes.append("候选落在同一张共享台面，排队相互污染，应拆开")

    return {
        "placements": placements,
        "unplaced": unplaced,
        "locked_teams": locked,
        "capacity": list(capacity.values()),
        "notes": notes,
    }
