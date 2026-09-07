"""
What does service cost, and is the current policy buying it efficiently?

inventory_health.py measures the network against its policy. This measures the
policy itself. Those are different questions, and the second one is almost never
asked: a planning system is handed a service level once, at implementation, and
it is still carrying it five years later because nobody ever priced it.

The policy here is a flat 95% cycle service level - z = 1.645 - applied to every
one of the 478 SKU x warehouse positions regardless of what the item costs, how
much of it moves, or how large a replenishment is. That is the default in every
planning package on the market, and it is the thing this module argues with.

Three questions
---------------
1. THE EXCHANGE CURVE. Safety stock and service are the two axes of the same
   trade, and the curve between them is steeply concave: the units that buy the
   move from 90% to 95% are cheap and the ones that buy 99% to 99.5% are not.
   Quoting a service target without the curve beside it is quoting a price with
   no idea what is being bought.

2. IS THE SAME MONEY BUYING THE MOST SERVICE IT COULD? A flat z spends the most
   on exactly the items where a point of service is dearest - high unit cost,
   large replenishment quantities - and the least where it is cheapest. Holding
   the safety-stock investment exactly constant and re-allocating it by ABC
   class raises the network fill rate for nothing. That is not a saving, it is
   the same money spent better, which is a far easier proposal to get approved.

3. HOW MUCH OF THE AVAILABLE GAIN DOES THAT CAPTURE? The class ladder is what a
   business can actually implement - three numbers a planner can key in. The
   theoretical optimum sets a service level per SKU by equalising the marginal
   fill rate per dollar across the whole network (the Lagrangian below), which
   is better and which nobody maintains by hand. Reporting the ladder without
   the ceiling above it would make a partial answer look like a complete one.

The arithmetic, stated
----------------------
Safety stock per position is King's formula, recomputed here from the same
demand and lead-time inputs the planning view was built from rather than read
off it - so if the two ever disagree, a test says so instead of an executive
finding out.

    sigma_DL = sqrt(L * sigma_d^2 + mu_d^2 * sigma_L^2)
    safety   = z * sigma_DL

Fill rate uses the standard normal loss function, which is the expected units
short per replenishment cycle divided by the size of that cycle:

    G(z) = phi(z) - z * (1 - Phi(z))
    fill = 1 - sigma_DL * G(z) / Q

Q is the mean quantity actually ordered from a supplier for that SKU, taken from
fact_purchase_orders. This matters: the usual move is to assume an EOQ from an
invented ordering cost and holding rate, which quietly makes the whole answer a
function of two numbers somebody guessed. The replenishment quantity is
observable here, so it is observed.

What this does NOT do
---------------------
   * Put a dollar value on a stockout. Lost margin, expedite freight and the
     cost of a customer not calling back are all real and none of them are in
     this data. Every figure below is stated in units of service and dollars of
     inventory, both of which are measured.
   * Recommend a service target. It prices them. Which one to buy is a
     commercial decision that depends on the number in the paragraph above.

Outputs (analytics/output/):
    service_exchange_curve.csv      service level -> safety stock $ -> fill rate
    service_policy_by_class.csv     current vs re-allocated policy, by ABC class
    service_positions.csv           per position: sigma_DL, Q, fill, both policies
    service_economics_summary.json  the headline figures the report and tests read

Usage:
    python analytics/service_economics.py
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BRONZE = ROOT / "data" / "bronze"
OUT = Path(__file__).resolve().parent / "output"

# The policy in force: one cycle service level for every position, everywhere.
CURRENT_Z = 1.645

# The ladder a planner can actually key in: three service levels by ABC class.
# Shape only - the level of the whole ladder is solved for below so that it
# costs exactly what the flat policy costs today.
CLASS_SERVICE = {"A": 0.98, "B": 0.95, "C": 0.90}

# Service levels the exchange curve is quoted at.
CURVE_LEVELS = [0.80, 0.85, 0.90, 0.925, 0.95, 0.96, 0.97, 0.98, 0.99, 0.995]

# z is clipped to a band a business would actually run: no position is planned
# below 80% cycle service however cheap its stock is, and none above 99.9%
# however dear its shortage. Without a floor the unconstrained optimum strips
# safety stock off slow C items entirely, which is arithmetically correct and
# commercially unserious - a SKU with no protection at all is a stockout with a
# spreadsheet in front of it.
Z_FLOOR, Z_CEILING = 0.8416, 3.0902      # 80% and 99.9% cycle service

# The two things a service policy can be trying to maximise. They are not the
# same objective and they do not have the same answer, which is the finding this
# module exists to produce.
OBJECTIVES = {
    "units": "share of demanded units filled from stock",
    "revenue": "share of demanded revenue filled from stock",
}

_erf = np.vectorize(math.erf)


def norm_cdf(z):
    return 0.5 * (1.0 + _erf(np.asarray(z, dtype=float) / math.sqrt(2.0)))


def norm_pdf(z):
    z = np.asarray(z, dtype=float)
    return np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def norm_ppf(p):
    """Inverse normal CDF by bisection.

    Deliberately not a rational approximation: this has one moving part, is
    exact to the width of a double after 80 halvings of [-8, 8], and cannot be
    subtly wrong in the tails the way a mis-transcribed coefficient can."""
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1 - 1e-12)
    lo = np.full_like(p, -8.0)
    hi = np.full_like(p, 8.0)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        too_low = norm_cdf(mid) < p
        lo = np.where(too_low, mid, lo)
        hi = np.where(too_low, hi, mid)
    return (lo + hi) / 2.0


def loss(z):
    """Standard normal loss function G(z): expected units short per cycle, in
    units of sigma."""
    z = np.asarray(z, dtype=float)
    return norm_pdf(z) - z * (1.0 - norm_cdf(z))


def load() -> dict:
    return {
        "position": pd.read_csv(BRONZE / "fact_inventory_position.csv",
                                parse_dates=["week_start"]),
        "products": pd.read_csv(BRONZE / "dim_product.csv"),
        "sourcing": pd.read_csv(BRONZE / "fact_sourcing.csv"),
        "orders": pd.read_csv(BRONZE / "fact_orders.csv"),
        "po": pd.read_csv(BRONZE / "fact_purchase_orders.csv"),
    }


def abc_classes(orders: pd.DataFrame) -> pd.Series:
    """Pareto on revenue - the same cut inventory_health.py makes, so the two
    pages cannot disagree about which SKUs are A."""
    rev = (orders.qty_shipped * orders.unit_price).groupby(orders.product_id).sum()
    rev = rev.sort_values(ascending=False)
    return pd.cut(rev.cumsum() / rev.sum(), [0, 0.8, 0.95, 1.0],
                  labels=["A", "B", "C"]).rename("abc_class")


def positions(d: dict) -> pd.DataFrame:
    """One row per SKU x warehouse in the latest week, carrying everything the
    policy arithmetic needs."""
    pos = d["position"]
    cur = pos[pos.week_start == pos.week_start.max()].copy()

    # The lane a position is planned against: the SLOWEST approved source, which
    # is the same choice the planning view itself made.
    lead = d["sourcing"].groupby("product_id").agg(
        lead_days=("contract_lead_days", "max"),
        lead_sigma=("lead_time_sigma_days", "max"))
    cur["lead_sigma"] = cur.product_id.map(lead.lead_sigma)

    # King's formula, recomputed rather than read off safety_stock_target.
    cur["sigma_dl"] = np.sqrt(cur.lead_days * cur.demand_sigma ** 2
                              + cur.avg_daily_demand ** 2 * cur.lead_sigma ** 2)

    prod = d["products"].set_index("product_id")
    cur["unit_cost"] = cur.product_id.map(prod.unit_cost)
    cur["abc_class"] = cur.product_id.map(abc_classes(d["orders"])).astype(str)

    # Replenishment quantity: what is actually ordered from a supplier for this
    # SKU, not an EOQ derived from assumptions nobody wrote down.
    q = d["po"].groupby("product_id").qty_ordered.mean()
    cur["replenishment_qty"] = cur.product_id.map(q)
    cur = cur[cur.replenishment_qty.notna() & (cur.sigma_dl > 0)
              & cur.unit_cost.notna()].copy()

    # Two weightings, because there are two defensible things to maximise. A
    # point of fill on a fast mover is not the same point as on a SKU that ships
    # twice a month (units), and neither is a point on a $4 item the same as on
    # a $40 one (revenue).
    cur["unit_price"] = cur.product_id.map(prod.unit_price)
    cur["demand_weight"] = cur.avg_daily_demand / cur.avg_daily_demand.sum()
    rev = cur.avg_daily_demand * cur.unit_price
    cur["revenue_weight"] = rev / rev.sum()
    return cur.reset_index(drop=True)


def spend(cur: pd.DataFrame, z) -> float:
    """Safety-stock investment in dollars at the given z (scalar or per row)."""
    return float((np.asarray(z) * cur.sigma_dl * cur.unit_cost).sum())


def network_fill(cur: pd.DataFrame, z, objective="units") -> float:
    """Weighted fill rate across the network, under one of the two objectives."""
    w = cur[f"{'demand' if objective == 'units' else 'revenue'}_weight"].values
    f = 1.0 - cur.sigma_dl.values * loss(z) / cur.replenishment_qty.values
    return float((np.clip(f, 0.0, 1.0) * w).sum())


def solve_offset(cur: pd.DataFrame, base_z: np.ndarray, budget: float) -> float:
    """Shift a whole ladder up or down until it costs exactly the budget.

    The ladder's SHAPE is the policy decision - A above B above C. Its level is
    just whatever makes the proposal cost-neutral, which is what makes it an
    easy proposal: nobody has to approve new working capital."""
    lo, hi = -3.0, 3.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if spend(cur, np.clip(base_z + mid, Z_FLOOR, Z_CEILING)) < budget:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def optimal_z(cur: pd.DataFrame, budget: float, objective="units") -> np.ndarray:
    """The best the money can do: a service level per SKU.

    At the optimum the marginal fill rate bought by the next dollar is equal
    everywhere - otherwise you would move a dollar from where it buys less to
    where it buys more. Differentiating the weighted fill rate and the spend
    with respect to z_i gives

        d(fill)/d$_i = w_i * (1 - Phi(z_i)) / (Q_i * c_i)

    where sigma_DL cancels out of the ratio entirely - the item's variability
    decides how MUCH stock a service level costs, not how efficiently that
    stock converts into service. Setting that equal to a common multiplier
    lambda and inverting gives z_i directly; bisecting lambda spends the budget
    exactly. Which w_i goes in the numerator is the objective, and it is the
    whole argument: units and revenue disagree about where the next dollar
    belongs."""
    w = cur[f"{'demand' if objective == 'units' else 'revenue'}_weight"].values
    qc = cur.replenishment_qty.values * cur.unit_cost.values

    def z_at(lam):
        return np.clip(norm_ppf(1.0 - np.clip(lam * qc / w, 1e-12, 1 - 1e-12)),
                       Z_FLOOR, Z_CEILING)

    lo, hi = 1e-12, 1.0
    for _ in range(120):
        mid = math.sqrt(lo * hi)
        # A bigger lambda demands a higher marginal return, so buys less stock.
        if spend(cur, z_at(mid)) > budget:
            lo = mid
        else:
            hi = mid
    return z_at(math.sqrt(lo * hi))


def policy_table(cur: pd.DataFrame, budget: float) -> tuple:
    """The four ways to spend today's safety-stock budget, scored on both
    objectives. Same dollars in every row - only the allocation changes."""
    base = cur.abc_class.map({k: float(norm_ppf(v))
                              for k, v in CLASS_SERVICE.items()}).values
    offset = solve_offset(cur, base, budget)
    policies = {
        "flat 95% (in force)": np.full(len(cur), CURRENT_Z),
        "ABC ladder 98/95/90": np.clip(base + offset, Z_FLOOR, Z_CEILING),
        "optimal for units": optimal_z(cur, budget, "units"),
        "optimal for revenue": optimal_z(cur, budget, "revenue"),
    }
    rows = []
    for name, z in policies.items():
        rows.append({
            "policy": name,
            "safety_stock_value": round(spend(cur, z), 2),
            "fill_units": round(network_fill(cur, z, "units"), 5),
            "fill_revenue": round(network_fill(cur, z, "revenue"), 5),
            "mean_service_level": round(float(np.average(
                norm_cdf(z), weights=cur.demand_weight)), 4),
        })
    return pd.DataFrame(rows), policies, float(offset)


def build() -> tuple:
    d = load()
    cur = positions(d)

    budget = spend(cur, CURRENT_Z)

    # 1. The exchange curve: what each service level costs, and what it buys
    #    under each objective.
    curve = []
    for sl in CURVE_LEVELS:
        z = float(norm_ppf(sl))
        curve.append({
            "service_level": sl,
            "z": round(z, 4),
            "safety_stock_value": round(spend(cur, z), 2),
            "vs_current_value": round(spend(cur, z) - budget, 2),
            "fill_units": round(network_fill(cur, z, "units"), 6),
            "fill_revenue": round(network_fill(cur, z, "revenue"), 6),
        })
    curve = pd.DataFrame(curve)

    # 2. Four allocations of the identical budget.
    table, policies, offset = policy_table(cur, budget)
    for name, key in [("ABC ladder 98/95/90", "z_ladder"),
                      ("optimal for units", "z_optimal_units"),
                      ("optimal for revenue", "z_optimal_revenue")]:
        cur[key] = policies[name]
    cur["z_current"] = CURRENT_Z

    t = table.set_index("policy")
    flat, ladder = t.loc["flat 95% (in force)"], t.loc["ABC ladder 98/95/90"]
    opt_u, opt_r = t.loc["optimal for units"], t.loc["optimal for revenue"]

    # 3. The same finding taken as cash instead of service: what the
    #    units-optimal allocation costs to hold today's unit fill exactly.
    lo, hi = 0.0, budget
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if network_fill(cur, optimal_z(cur, mid, "units"), "units") < flat.fill_units:
            lo = mid
        else:
            hi = mid
    budget_same_service = (lo + hi) / 2.0

    cls = cur.groupby("abc_class")
    by_class = pd.DataFrame({
        "positions": cls.size(),
        "skus": cls.product_id.nunique(),
        "value_now": (CURRENT_Z * cur.sigma_dl * cur.unit_cost)
                     .groupby(cur.abc_class).sum().round(2),
        "value_ladder": (cur.z_ladder * cur.sigma_dl * cur.unit_cost)
                        .groupby(cur.abc_class).sum().round(2),
        "value_optimal_units": (cur.z_optimal_units * cur.sigma_dl * cur.unit_cost)
                               .groupby(cur.abc_class).sum().round(2),
        # Rounded like every other float that reaches a file. norm_cdf is
        # built on math.erf, which is the platform's libm, so the sixteenth
        # digit of this number is a property of the machine that wrote it -
        # and CI asserts these files regenerate byte for byte. It failed on
        # the Linux runner and passed on Windows for exactly that digit.
        "service_now": round(float(norm_cdf(CURRENT_Z)), 4),
        "service_ladder": pd.Series(norm_cdf(cur.z_ladder), index=cur.index)
                          .groupby(cur.abc_class).mean().round(4),
        "service_optimal_units": pd.Series(norm_cdf(cur.z_optimal_units),
                                           index=cur.index)
                                 .groupby(cur.abc_class).mean().round(4),
    })
    by_class["ladder_change"] = (by_class.value_ladder - by_class.value_now).round(2)
    by_class["optimal_change"] = (by_class.value_optimal_units
                                  - by_class.value_now).round(2)
    by_class = by_class.reset_index()

    # What another point of cycle service costs from here - the slope of the
    # curve at the policy in force, which is the number a service target ought
    # to be argued with.
    z_up = float(norm_ppf(min(0.995, float(norm_cdf(CURRENT_Z)) + 0.01)))

    summary = {
        "positions": int(len(cur)),
        "skus": int(cur.product_id.nunique()),
        "as_of": str(cur.week_start.max().date()),
        "current_service_level": round(float(norm_cdf(CURRENT_Z)), 4),
        "current_z": CURRENT_Z,
        "safety_stock_value": round(budget, 2),
        "fill_units_now": float(flat.fill_units),
        "fill_revenue_now": float(flat.fill_revenue),
        "cost_of_one_more_point": round(spend(cur, z_up) - budget, 2),
        "cost_of_99_vs_95": round(float(
            curve.loc[curve.service_level == 0.99, "safety_stock_value"].iloc[0]
            - budget), 2),
        "ladder_service_levels": CLASS_SERVICE,
        "ladder_offset_z": round(offset, 4),
        "fill_units_ladder": float(ladder.fill_units),
        "fill_revenue_ladder": float(ladder.fill_revenue),
        "ladder_units_change_pts": round(
            (ladder.fill_units - flat.fill_units) * 100, 3),
        "ladder_revenue_change_pts": round(
            (ladder.fill_revenue - flat.fill_revenue) * 100, 3),
        "fill_units_optimal": float(opt_u.fill_units),
        "optimal_units_change_pts": round(
            (opt_u.fill_units - flat.fill_units) * 100, 3),
        "fill_revenue_optimal": float(opt_r.fill_revenue),
        "optimal_revenue_change_pts": round(
            (opt_r.fill_revenue - flat.fill_revenue) * 100, 3),
        "budget_holding_service": round(budget_same_service, 2),
        "working_capital_released": round(budget - budget_same_service, 2),
        "working_capital_released_pct": round(
            1 - budget_same_service / budget, 4),
        "a_class_ladder_change": round(float(
            by_class.loc[by_class.abc_class == "A", "ladder_change"].iloc[0]), 2),
        "c_class_ladder_change": round(float(
            by_class.loc[by_class.abc_class == "C", "ladder_change"].iloc[0]), 2),
        "a_class_optimal_change": round(float(
            by_class.loc[by_class.abc_class == "A", "optimal_change"].iloc[0]), 2),
        "c_class_optimal_change": round(float(
            by_class.loc[by_class.abc_class == "C", "optimal_change"].iloc[0]), 2),
    }
    return summary, curve, table, by_class, cur


def headline(summary: dict) -> pd.DataFrame:
    """The summary as a one-row table, for the report to read scalars from.
    A single row cannot be double-counted by a careless roll-up."""
    scalars = {k: v for k, v in summary.items()
               if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    return pd.DataFrame([scalars])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    summary, curve, table, by_class, cur = build()

    curve.to_csv(OUT / "service_exchange_curve.csv", index=False)
    table.to_csv(OUT / "service_policy_options.csv", index=False)
    by_class.to_csv(OUT / "service_policy_by_class.csv", index=False)
    cols = ["product_id", "warehouse_id", "abc_class", "avg_daily_demand",
            "demand_sigma", "lead_days", "lead_sigma", "sigma_dl", "unit_cost",
            "unit_price", "replenishment_qty", "demand_weight", "revenue_weight",
            "z_current", "z_ladder", "z_optimal_units", "z_optimal_revenue"]
    cur[cols].round(5).to_csv(OUT / "service_positions.csv", index=False)
    (OUT / "service_economics_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    headline(summary).to_csv(OUT / "service_economics_headline.csv", index=False)

    print()
    print("=" * 74)
    print(f"SERVICE LEVEL ECONOMICS   (week of {summary['as_of']})")
    print("=" * 74)
    print(f"  {summary['positions']} positions, {summary['skus']} SKUs, every one "
          f"planned at a flat {summary['current_service_level']:.0%}")
    print(f"  safety stock investment        ${summary['safety_stock_value']:,.0f}")
    print(f"  fill rate, weighted by units   {summary['fill_units_now']:.2%}")
    print(f"  fill rate, weighted by revenue {summary['fill_revenue_now']:.2%}")
    print()
    print("WHAT SERVICE COSTS")
    print("=" * 74)
    print(f"  {'service':>8s} {'z':>6s} {'safety stock':>14s} {'vs today':>13s} "
          f"{'fill rate':>11s}")
    for _, r in curve.iterrows():
        here = "  <- in force" if abs(r.z - CURRENT_Z) < 0.01 else ""
        print(f"  {r.service_level:>7.1%} {r.z:>6.2f} "
              f"${r.safety_stock_value:>13,.0f} ${r.vs_current_value:>+12,.0f} "
              f"{r.fill_units:>11.2%}{here}")
    print()
    print("  One fill rate, because at a UNIFORM service level the two objectives")
    print(f"  agree to {abs(summary['fill_units_now'] - summary['fill_revenue_now'])*100:.3f} "
          "of a point - which is exactly why a flat policy")
    print("  survives so long. It is never obviously wrong under either. The two")
    print("  only come apart once the money starts being allocated.")
    print()
    print(f"  One more point of cycle service costs "
          f"${summary['cost_of_one_more_point']:,.0f} from here; 95% to 99% costs "
          f"${summary['cost_of_99_vs_95']:,.0f}.")
    print()
    print("THE SAME MONEY, FOUR WAYS")
    print("=" * 74)
    print(f"  {'policy':24s} {'safety stock':>14s} {'fill (units)':>13s} "
          f"{'fill ($)':>10s}")
    for _, r in table.iterrows():
        print(f"  {r.policy:24s} ${r.safety_stock_value:>13,.0f} "
              f"{r.fill_units:>13.2%} {r.fill_revenue:>10.2%}")
    print()
    verb = "loses" if summary["ladder_units_change_pts"] < 0 else "gains"
    print(f"  The ABC ladder is the textbook move, and on this network it {verb} "
          f"{abs(summary['ladder_units_change_pts']):.2f} points")
    print("  of unit fill for the same money. That is not a defect in the ladder:")
    print("  it protects revenue, and revenue is not what a fill rate on units")
    print(f"  measures - the same ladder moves revenue fill "
          f"{summary['ladder_revenue_change_pts']:+.2f} points.")
    print("  A flat service level is wrong; so is reaching for the standard fix")
    print("  without first saying which of the two things you are buying.")
    print()
    print(f"  Allocating for units reaches {summary['fill_units_optimal']:.2%} "
          f"({summary['optimal_units_change_pts']:+.2f} pts).")
    print(f"  Allocating for revenue reaches {summary['fill_revenue_optimal']:.2%} "
          f"({summary['optimal_revenue_change_pts']:+.2f} pts).")
    print("  Both cost exactly what is being spent today.")
    print()
    print("WHERE THE MONEY MOVES")
    print("=" * 74)
    print(f"  {'class':>6s} {'positions':>10s} {'held now':>13s} "
          f"{'ABC ladder':>14s} {'optimal (units)':>17s}")
    for _, r in by_class.iterrows():
        print(f"  {r.abc_class:>6s} {r.positions:>10.0f} ${r.value_now:>12,.0f} "
              f"${r.ladder_change:>+13,.0f} ${r.optimal_change:>+16,.0f}")
    print()
    print("  ...or take the finding as cash instead of service:")
    print(f"  holding the unit fill rate exactly where it is needs "
          f"${summary['budget_holding_service']:,.0f},")
    print(f"  releasing ${summary['working_capital_released']:,.0f} of working "
          f"capital ({summary['working_capital_released_pct']:.1%}) at "
          "identical service.")
    print()
    print(f"wrote 6 files to {OUT}")


if __name__ == "__main__":
    main()
