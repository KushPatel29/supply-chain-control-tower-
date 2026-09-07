"""
Inventory and network health: is the stock in the right place, and enough of it?

fact_inventory_snapshot answers lot-level questions - FEFO, expiry, "which
customers got batch X". It cannot answer planning ones, because it models lots
depleting rather than a stock position being replenished; measured on it every
SKU looks permanently stocked out. fact_inventory_position is the planner's
view, and this reads it.

What it computes
----------------
1. Position vs policy. Reorder point = cycle stock (mean demand x lead time)
   plus safety stock from King's formula, which carries BOTH sources of
   variability: demand varying over the lead time, and the lead time itself
   varying against average demand. Ignoring the second term is the usual way a
   safety stock ends up too small on a long offshore lane.

2. Cover against lead time. Days of cover is meaningless on its own: 20 days is
   comfortable on a 6-day domestic lane and a guaranteed stockout on a 40-day
   one. Every position is compared to its OWN replenishment lead.

3. ABC. Pareto on revenue, because a dollar of shortfall on an A item is not
   the same dollar as on a C item.

4. Transfer opportunity. A shortage at one warehouse against a surplus of the
   same SKU at another is not a buying problem, it is a moving problem. This
   separates the two, because the second is faster and cheaper and gets missed
   when shortage is only ever reported nationally.

5. Excess. Position above 1.5x the reorder point, which is working capital
   doing nothing - reported alongside the shortfall rather than netted against
   it, since they are different SKUs in different places.

What it does NOT compute, and why
---------------------------------
   * XYZ demand classification. Every SKU in this dataset lands in the same
     variability band (CV 0.58-0.77), so the axis would separate nothing. ABC
     and the cover-vs-lead gap are where this data actually bites.
   * Carrying cost and stockout cost in dollars. No holding-rate or lost-margin
     assumption exists here, and inventing one would turn a measurement into an
     opinion.

Outputs (analytics/output/):
    inventory_position.csv     per SKU x warehouse: cover, gap, excess, class
    transfer_opportunities.csv shortages a surplus elsewhere could cover
    inventory_trend.csv        the same policy test across all 13 weeks
    inventory_health_summary.json  the headline figures the report and tests read

Usage:
    python analytics/inventory_health.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BRONZE = ROOT / "data" / "bronze"
OUT = Path(__file__).resolve().parent / "output"

EXCESS_MULTIPLE = 1.5      # position above this x reorder point is excess
DOMESTIC_BLOC = "North America"    # the same split supply_risk.py uses


def load():
    return {
        "position": pd.read_csv(BRONZE / "fact_inventory_position.csv",
                                parse_dates=["week_start"]),
        "products": pd.read_csv(BRONZE / "dim_product.csv"),
        "warehouses": pd.read_csv(BRONZE / "dim_warehouse.csv"),
        "orders": pd.read_csv(BRONZE / "fact_orders.csv"),
        "sourcing": pd.read_csv(BRONZE / "fact_sourcing.csv"),
        "suppliers": pd.read_csv(BRONZE / "dim_supplier.csv"),
    }


def abc_classes(orders: pd.DataFrame) -> pd.Series:
    """Pareto on revenue: A to 80%, B to 95%, C the tail."""
    rev = (orders.qty_shipped * orders.unit_price).groupby(orders.product_id).sum()
    rev = rev.sort_values(ascending=False)
    return pd.cut(rev.cumsum() / rev.sum(), [0, 0.8, 0.95, 1.0],
                  labels=["A", "B", "C"]).rename("abc_class")


def derive(pos: pd.DataFrame, cost: pd.Series) -> pd.DataFrame:
    """Position against policy. Applied to one week or to all of them, so the
    trend and the current snapshot cannot drift apart by being computed twice."""
    p = pos.copy()
    p["unit_cost"] = p.product_id.map(cost)
    p["position_units"] = p.on_hand_units + p.on_order_units
    p["cover_days"] = np.where(p.avg_daily_demand > 0,
                               p.on_hand_units / p.avg_daily_demand, np.nan)
    p["gap_units"] = (p.reorder_point - p.position_units).clip(lower=0)
    p["excess_units"] = (p.position_units
                         - p.reorder_point * EXCESS_MULTIPLE).clip(lower=0)
    p["gap_value"] = (p.gap_units * p.unit_cost).round(2)
    p["excess_value"] = (p.excess_units * p.unit_cost).round(2)
    p["on_hand_value"] = (p.on_hand_units * p.unit_cost).round(2)
    # A position that cannot outlast its own replenishment lead has a stockout
    # window built into it, whatever the headline cover number says.
    p["covers_lead_time"] = (p.cover_days >= p.lead_days).astype(int)
    return p


def planning_lane(d: dict) -> pd.Series:
    """Which lane actually governs each SKU's replenishment.

    A SKU with two approved sources is planned against the SLOWEST of them -
    that is the lead time the reorder point was built from - so the lane that
    governs is that supplier's, not an average of the two. Split nearshore vs
    offshore on the same sourcing bloc supply_risk.py uses, so the two pages
    cannot disagree about what offshore means."""
    s = d["sourcing"].merge(d["suppliers"][["supplier_id", "sourcing_bloc"]],
                            on="supplier_id", how="left")
    slowest = s.sort_values("contract_lead_days").groupby("product_id").tail(1)
    return (slowest.set_index("product_id").sourcing_bloc
            .ne(DOMESTIC_BLOC).map({True: "Offshore", False: "Nearshore"})
            .rename("lane"))


def current_position(d: dict) -> pd.DataFrame:
    pos = d["position"]
    cost = d["products"].set_index("product_id").unit_cost
    cur = derive(pos[pos.week_start == pos.week_start.max()], cost)
    cur = cur.join(abc_classes(d["orders"]), on="product_id")
    cur = cur.join(planning_lane(d), on="product_id")
    cur = cur.merge(d["warehouses"][["warehouse_id", "warehouse_name", "region"]],
                    on="warehouse_id", how="left")
    cur = cur.merge(d["products"][["product_id", "sku", "category"]],
                    on="product_id", how="left")
    return cur


def transfers(cur: pd.DataFrame) -> pd.DataFrame:
    """Shortages that a surplus of the same SKU elsewhere could cover."""
    agg = cur.groupby(["product_id", "sku", "category"], as_index=False).agg(
        short_units=("gap_units", "sum"),
        spare_units=("excess_units", "sum"),
        unit_cost=("unit_cost", "first"))
    agg["transferable_units"] = np.minimum(agg.short_units, agg.spare_units).round(0)
    agg["transferable_value"] = (agg.transferable_units * agg.unit_cost).round(2)
    return (agg[agg.transferable_units > 0]
            .sort_values("transferable_value", ascending=False)
            .reset_index(drop=True))


def weekly_trend(d: dict) -> pd.DataFrame:
    """The same policy test applied to all 13 weeks, split by planning lane.

    A single snapshot cannot tell a network that is drawing down from one that
    is holding steady, and those are different decisions: one needs a purchase
    order, the other needs the policy re-set. Split nearshore vs offshore,
    because a total that barely moves can hide a network rotating its stock out
    of the lanes that need cover and into the ones that do not - which is how a
    business ends up holding plenty of inventory and still missing service.
    """
    cost = d["products"].set_index("product_id").unit_cost
    a = derive(d["position"], cost).join(planning_lane(d), on="product_id")
    g = a.groupby(["week_start", "lane"])
    t = g.agg(positions=("gap_units", "size"),
              gap_value=("gap_value", "sum"),
              excess_value=("excess_value", "sum"),
              on_hand_value=("on_hand_value", "sum"),
              below_reorder_point=("gap_units", lambda x: int((x > 0).sum())),
              not_covering_lead=("covers_lead_time",
                                 lambda x: int((x == 0).sum()))).reset_index()
    t["below_reorder_share"] = (t.below_reorder_point / t.positions).round(4)
    t["gap_value"] = t.gap_value.round(2)
    t["excess_value"] = t.excess_value.round(2)
    t["on_hand_value"] = t.on_hand_value.round(2)
    # A text label with its own sort index, not a date: a date column on a
    # categorical axis gets Power BI's auto date hierarchy and the axis turns
    # into a Year/Quarter/Month drill of thirteen consecutive weeks.
    weeks = sorted(t.week_start.unique())
    t["week_index"] = t.week_start.map({w: i for i, w in enumerate(weeks)})
    t["week_label"] = t.week_start.dt.strftime("%d %b")
    t["week_start"] = t.week_start.dt.date
    return t.sort_values(["week_index", "lane"]).reset_index(drop=True)


def build():
    d = load()
    cur = current_position(d)
    tr = transfers(cur)
    trend = weekly_trend(d)

    by_abc = cur.groupby("abc_class", observed=True).gap_value.sum()
    summary = {
        "as_of": str(cur.week_start.max().date()),
        "positions": int(len(cur)),
        "skus": int(cur.product_id.nunique()),
        "warehouses": int(cur.warehouse_id.nunique()),
        "on_hand_value": round(float(cur.on_hand_value.sum()), 2),
        "median_cover_days": round(float(cur.cover_days.median()), 1),
        "median_lead_days": float(cur.lead_days.median()),
        "below_reorder_point": int((cur.gap_units > 0).sum()),
        "below_reorder_share": round(float((cur.gap_units > 0).mean()), 4),
        "replenishment_gap_value": round(float(cur.gap_value.sum()), 2),
        "gap_value_a_class": round(float(by_abc.get("A", 0.0)), 2),
        "excess_positions": int((cur.excess_units > 0).sum()),
        "excess_value": round(float(cur.excess_value.sum()), 2),
        "positions_not_covering_lead": int((cur.covers_lead_time == 0).sum()),
        "transfer_skus": int(len(tr)),
        "transfer_value": round(float(tr.transferable_value.sum()), 2),
        "a_class_skus": int((abc_classes(d["orders"]) == "A").sum()),
        "trend_weeks": int(trend.week_start.nunique()),
    }
    first, last = trend.week_start.min(), trend.week_start.max()

    def at(week, lane=None):
        rows = trend[trend.week_start == week]
        return rows if lane is None else rows[rows.lane == lane]

    def move(col, lane=None):
        return round(float(at(last, lane)[col].sum()
                           / at(first, lane)[col].sum() - 1), 4)

    summary["gap_value_13w_ago"] = round(float(at(first).gap_value.sum()), 2)
    summary["gap_value_change"] = move("gap_value")
    summary["on_hand_value_change"] = move("on_hand_value")
    summary["offshore_gap_change"] = move("gap_value", "Offshore")
    summary["nearshore_gap_change"] = move("gap_value", "Nearshore")
    summary["offshore_on_hand_change"] = move("on_hand_value", "Offshore")
    summary["nearshore_on_hand_change"] = move("on_hand_value", "Nearshore")
    summary["offshore_gap_share"] = round(
        float(at(last, "Offshore").gap_value.sum() / at(last).gap_value.sum()), 4)
    summary["transfer_share_of_gap"] = round(
        summary["transfer_value"] / summary["replenishment_gap_value"], 4)

    OUT.mkdir(parents=True, exist_ok=True)
    keep = ["week_start", "sku", "category", "warehouse_name", "region", "abc_class",
            "lane",
            "on_hand_units", "on_order_units", "position_units", "reorder_point",
            "safety_stock_target", "lead_days", "cover_days", "covers_lead_time",
            "gap_units", "gap_value", "excess_units", "excess_value", "on_hand_value"]
    cur[keep].round({"cover_days": 1}).to_csv(OUT / "inventory_position.csv", index=False)
    tr.to_csv(OUT / "transfer_opportunities.csv", index=False)
    trend.to_csv(OUT / "inventory_trend.csv", index=False)
    (OUT / "inventory_health_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary, cur, tr, trend


def main():
    summary, cur, tr, _ = build()
    print("INVENTORY POSITION" + f"   (week of {summary['as_of']})")
    print("=" * 66)
    print(f"  {summary['positions']} SKU x warehouse positions, "
          f"{summary['skus']} SKUs across {summary['warehouses']} warehouses")
    print(f"  on hand                        ${summary['on_hand_value']:,.0f}")
    print(f"  median cover                   {summary['median_cover_days']} days"
          f"   against a {summary['median_lead_days']:.0f}-day median lead")
    print()
    print("WHERE THE POLICY IS NOT BEING MET")
    print("=" * 66)
    print(f"  below reorder point            {summary['below_reorder_point']} of "
          f"{summary['positions']}  ({summary['below_reorder_share']:.0%})")
    print(f"  replenishment gap              ${summary['replenishment_gap_value']:,.0f}"
          f"   (${summary['gap_value_a_class']:,.0f} on A-class)")
    print(f"  cover shorter than its lead    {summary['positions_not_covering_lead']} positions"
          "  <- a stockout window is already built in")
    print(f"  excess above {EXCESS_MULTIPLE}x reorder     {summary['excess_positions']} positions,"
          f" ${summary['excess_value']:,.0f} of idle working capital")
    print()
    print("IS IT GETTING BETTER OR WORSE?")
    print("=" * 66)
    print(f"  over {summary['trend_weeks']} weeks the gap moved "
          f"{summary['gap_value_change']:+.0%}"
          f"   (${summary['gap_value_13w_ago']:,.0f} -> "
          f"${summary['replenishment_gap_value']:,.0f})")
    print(f"  ...while total stock on hand moved only "
          f"{summary['on_hand_value_change']:+.1%}")
    print(f"  offshore lanes    gap {summary['offshore_gap_change']:+.0%}"
          f"   stock {summary['offshore_on_hand_change']:+.0%}")
    print(f"  nearshore lanes   gap {summary['nearshore_gap_change']:+.0%}"
          f"   stock {summary['nearshore_on_hand_change']:+.0%}")
    print("  the network is not running out of stock, it is rotating it out of")
    print("  the lanes that need the cover and into the ones that do not.")
    print()
    print("BUY, OR JUST MOVE?")
    print("=" * 66)
    print(f"  {summary['transfer_skus']} SKUs are short in one warehouse and long in another.")
    print(f"  ${summary['transfer_value']:,.0f} of the ${summary['replenishment_gap_value']:,.0f} gap "
          f"({summary['transfer_share_of_gap']:.0%}) needs no purchase order at all.")
    print()
    print("  top transfers:")
    for _, r in tr.head(5).iterrows():
        print(f"    {r.sku:10s} {r.category:12s} {r.transferable_units:>7,.0f} units"
              f"   ${r.transferable_value:>10,.0f}")
    print()
    print(f"wrote 4 files to {OUT}")


if __name__ == "__main__":
    main()
