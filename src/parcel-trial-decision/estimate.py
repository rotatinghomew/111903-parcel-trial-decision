"""估计：分派口径（ITT）与执行口径分开，聚类到独立分派单位。

- 问题一（同一分派比例下合箱与分箱的差异）：订单按原分派比较，
  以作业区为层、班组为聚类单位；班组才是该层的独立分派单位。
- 问题二（分箱班组在不同共享压力下的处境）：只看原分箱班组的订单，
  先汇总到作业区，再按该区的合箱班组占比比较；作业区才是占比的
  独立分派单位。
- 订单数不是独立重复：同一班组/作业区贡献的订单先归并到分派单位。
- 执行口径（按实际装箱）只作对照读数，含自选偏差，不作分派证据。
"""

from __future__ import annotations

from statistics import NormalDist

from .ledger import Ledger
from .views import collect_views

_NORMAL = NormalDist()


def z_quantile(level: float) -> float:
    return _NORMAL.inv_cdf(level)


def _solve(matrix: list, vector: list) -> list | None:
    """高斯-约当解线性方程组；奇异时返回 None。"""
    n = len(matrix)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        piv = aug[col][col]
        aug[col] = [v / piv for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                factor = aug[r][col]
                aug[r] = [a - factor * b for a, b in zip(aug[r], aug[col])]
    return [aug[i][n] for i in range(n)]


def ols_cr1(rows: list, y_key: str, x_key: str,
            fe_key: str | None = None, cluster_key: str | None = None) -> dict | None:
    """一元回归 + 可选层固定效应，CR1 聚类稳健标准误。"""
    fe_levels = sorted({r[fe_key] for r in rows}) if fe_key else []
    k = 2 + max(0, len(fe_levels) - 1)
    n = len(rows)
    if n <= k:
        return None
    d = []
    for r in rows:
        row = [1.0, float(r[x_key])]
        row += [1.0 if r[fe_key] == v else 0.0 for v in fe_levels[1:]]
        d.append(row)
    y = [float(r[y_key]) for r in rows]

    xtx = [[sum(d[i][a] * d[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    xty = [sum(d[i][a] * y[i] for i in range(n)) for a in range(k)]
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    resid = [y[i] - sum(d[i][a] * beta[a] for a in range(k)) for i in range(n)]

    clusters = {}
    for i, r in enumerate(rows):
        key = r[cluster_key] if cluster_key else i
        clusters.setdefault(key, []).append(i)
    g = len(clusters)
    meat = [[0.0] * k for _ in range(k)]
    for idx in clusters.values():
        score = [sum(d[i][a] * resid[i] for i in idx) for a in range(k)]
        for a in range(k):
            for b in range(k):
                meat[a][b] += score[a] * score[b]
    # (X'X)^-1 M (X'X)^-1
    inv_cols = []
    for a in range(k):
        col = _solve(xtx, [1.0 if i == a else 0.0 for i in range(k)])
        if col is None:
            return None
        inv_cols.append(col)
    xtx_inv = [[inv_cols[b][a] for b in range(k)] for a in range(k)]
    var = 0.0
    for a in range(k):
        for b in range(k):
            var += xtx_inv[1][a] * meat[a][b] * xtx_inv[b][1]
    if g > 1:
        var *= (g / (g - 1)) * ((n - 1) / (n - k))
    return {
        "effect": beta[1],
        "se": var ** 0.5,
        "n": n,
        "n_clusters": g,
    }


def _outcome_value(view, kind: str):
    if kind == "damage":
        return None if view.damage is None else float(view.damage)
    if kind == "contract":
        return None if view.contract_minor is None else float(view.contract_minor)
    if kind == "invoiced":
        return None if view.invoiced_minor is None else float(view.invoiced_minor)
    raise ValueError(kind)


def _rows_for(ledger: Ledger, combo: tuple, blocks: set, cut: str, kind: str,
              carrier: str | None = None):
    """收集可观察订单及其余订单被排除的原因。"""
    ids = [oid for oid, o in ledger.orders.items()
           if o.block in blocks and o.combo == combo and o.eligible
           and (carrier is None or o.carrier == carrier)]
    views = collect_views(ledger, ids, cut)
    rows, excluded = [], {"immature_or_pending": 0, "missing_cost": 0, "no_packages": 0}
    for oid in ids:
        view = views[oid]
        value = _outcome_value(view, kind)
        if value is None:
            if kind == "damage":
                excluded["immature_or_pending"] += 1
            elif ledger.executed(oid) is None:
                excluded["no_packages"] += 1
            else:
                excluded["missing_cost"] += 1
            continue
        order = ledger.orders[oid]
        rows.append({
            "order": oid,
            "y": value,
            "assigned": 1.0 if view.assigned == "combined" else 0.0,
            "executed": None if view.executed is None else (1.0 if view.executed == "combined" else 0.0),
            "zone": order.zone or order.block,
            "team": order.team,
            "block": order.block,
            "carrier": order.carrier,
            "compliant": view.compliant,
        })
    return rows, views, excluded


def observed_rows(ledger: Ledger, combo: tuple, blocks: set, cut: str,
                  kind: str) -> tuple[list, dict]:
    """公开入口：某口径下可观察的订单行与被排除计数。"""
    rows, _views, excluded = _rows_for(ledger, combo, blocks, cut, kind)
    return rows, excluded


def q1_estimate(ledger: Ledger, combo: tuple, blocks: set, cut: str, kind: str,
                carrier: str | None = None) -> dict:
    """问题一：同一作业区内按原分派比较；聚类到班组。

    同时给出执行口径读数（按实际装箱），两者分列，不合并为一个效果。
    """
    rows, views, excluded = _rows_for(ledger, combo, blocks, cut, kind, carrier)

    # 可及性：某作业区在某口径下只剩一臂时，该区不进入对照，也不跨区借对照。
    by_zone = {}
    for r in rows:
        by_zone.setdefault(r["zone"], set()).add(r["assigned"])
    dropped = sorted(z for z, arms in by_zone.items() if len(arms) < 2)
    fit_rows = [r for r in rows if r["zone"] not in set(dropped)]

    itt = ols_cr1(fit_rows, "y", "assigned", fe_key="zone", cluster_key="team")

    exec_rows = [r for r in fit_rows if r["executed"] is not None]
    per_protocol = ols_cr1(exec_rows, "y", "executed", fe_key="zone", cluster_key="team")

    teams_t = sorted({r["team"] for r in fit_rows if r["assigned"] == 1.0})
    teams_c = sorted({r["team"] for r in fit_rows if r["assigned"] == 0.0})
    # 临时拆箱看执行，不依赖售后是否成熟，按范围内全部订单统计。
    non_compliant = sorted(
        oid for oid, o in ledger.orders.items()
        if o.block in blocks and o.combo == combo and o.eligible
        and (carrier is None or o.carrier == carrier)
        and ledger.executed(oid) is not None and ledger.executed(oid) != o.choice
    )
    return {
        "estimand": f"q1_{kind}",
        "cut": cut,
        "carrier": carrier,
        "itt": itt,
        "per_protocol": per_protocol,
        "cluster_level": "team",
        "n_orders": len(fit_rows),
        "teams_combined": teams_t,
        "teams_split": teams_c,
        "zones": sorted({r["zone"] for r in fit_rows}),
        "zones_dropped_positivity": dropped,
        "excluded": excluded,
        "non_compliant_orders": non_compliant,
        "carriers": sorted({r["carrier"] for r in fit_rows}),
    }


def q2_estimate(ledger: Ledger, combo: tuple, blocks: set, cut: str,
                kind: str = "damage") -> dict:
    """问题二：原分箱班组在不同合箱占比下的处境；聚类到作业区。"""
    rows, views, excluded = _rows_for(ledger, combo, blocks, cut, kind)
    split_rows = [r for r in rows if r["assigned"] == 0.0]

    zones = ledger.design
    by_zone = {}
    for r in split_rows:
        by_zone.setdefault(r["zone"], []).append(r)
    zone_rows, no_quota = [], []
    for zone, members in by_zone.items():
        design = zones.get(zone)
        if design is None:
            no_quota.append(zone)
            continue
        zone_rows.append({
            "zone": zone,
            "block": members[0]["block"],
            "y": sum(m["y"] for m in members) / len(members),
            "quota3": 1.0 if design.quota >= 3 else 0.0,
            "n_orders": len(members),
        })

    # 可及性：占比的两个水平都要出现在同一批次的独立抽签里；
    # 只剩一个水平的批次不进入对照，也不跨批借对照。
    levels_by_block = {}
    for r in zone_rows:
        levels_by_block.setdefault(r["block"], set()).add(r["quota3"])
    blocks_one_sided = sorted(b for b, lv in levels_by_block.items() if len(lv) < 2)
    fit_rows = [r for r in zone_rows if r["block"] not in set(blocks_one_sided)]

    fit = ols_cr1(fit_rows, "y", "quota3", fe_key="block", cluster_key="zone")
    return {
        "estimand": f"q2_{kind}",
        "cut": cut,
        "itt": fit,
        "cluster_level": "zone",
        "n_orders": len(split_rows),
        "zones": sorted(r["zone"] for r in fit_rows),
        "zones_by_quota": {
            "quota3": sorted(r["zone"] for r in fit_rows if r["quota3"] == 1.0),
            "quota1": sorted(r["zone"] for r in fit_rows if r["quota3"] == 0.0),
        },
        "blocks_missing_contrast": blocks_one_sided,
        "zones_without_design": sorted(no_quota),
        "excluded": excluded,
    }


def one_sided_bound(estimate: dict | None, alpha: float, side: str) -> float | None:
    """单侧 (1-alpha) 界限；side=upper 取上界，lower 取下界。"""
    if estimate is None:
        return None
    z = z_quantile(1.0 - alpha)
    if side == "upper":
        return estimate["effect"] + z * estimate["se"]
    return estimate["effect"] - z * estimate["se"]


def mean_cr1(rows: list, y_key: str, cluster_key: str) -> dict | None:
    """单样本均值，CR1 聚类稳健标准误（用于绝对风险上限判断）。"""
    n = len(rows)
    if n < 2:
        return None
    mean = sum(float(r[y_key]) for r in rows) / n
    clusters = {}
    for r in rows:
        clusters.setdefault(r[cluster_key], []).append(float(r[y_key]) - mean)
    g = len(clusters)
    meat = sum((sum(vals)) ** 2 for vals in clusters.values())
    var = meat / (n * n)
    if g > 1:
        var *= g / (g - 1)
    return {"effect": mean, "se": var ** 0.5, "n": n, "n_clusters": g}
