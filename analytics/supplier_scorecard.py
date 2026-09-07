"""
Supplier performance: who is actually delivering, and what is it costing?

The rest of this repo measures the outbound half of the chain - what customers
ordered, what shipped, what is on the shelf. This measures the inbound half,
from fact_purchase_orders: what was ordered from a supplier, when it was
promised, when it turned up, how much of it was fit to use, and what was paid
against the contract.

Four dimensions, because a supplier that is cheap and late is a different
problem from one that is dear and reliable, and a single blended number hides
which one you have:

  Delivery         on time, in full, and fit for use - measured on the ACCEPTED
                   quantity. A pallet that arrives complete and on the promised
                   day, of which a fifth is condemned at the gate, did not
                   fulfil the order; counting it as OTIF is how a scorecard ends
                   up disagreeing with the people receiving the goods.
  Quality          share of received units rejected, and the money spent on
                   them. Cost of poor quality is a cash number, not a ratio.
  Cost             purchase price variance: what was invoiced against the price
                   that was negotiated, weighted by the units actually bought.
  Responsiveness   how far the lead time wanders. A supplier whose lead is
                   long but steady can be planned around; one whose lead is
                   short on average and swings six days either side cannot, and
                   the safety stock its variability forces is a cost it is
                   never charged for.

Scoring against fixed anchors, not against each other
-----------------------------------------------------
Each dimension is scored 0-100 between a published target and a published floor
- not min-max scaled across the fifteen suppliers. Min-max is the usual choice
and it is wrong here for a specific reason: it guarantees somebody scores 100
and somebody scores 0, so a panel where every supplier is failing looks exactly
like one where every supplier is excellent. The anchors are in TARGETS below;
they are assumptions, so they are visible, quotable, and easy to argue with.

Tier is a claim, the score is a measurement
-------------------------------------------
dim_supplier carries a contractual supplier_tier. Nothing here uses it as an
input. It is compared to the measured band at the end, because "our tiering does
not match how these suppliers actually perform" is the finding a vendor review
exists to produce, and a scorecard that re-reads the tier it was handed tells
nobody anything they did not already believe.

Where the money is
------------------
Award shift. For every SKU with more than one approved source, the primary is
compared to the best qualified alternate. Where an alternate scores materially
higher, the annual spend sitting with the weaker primary is quantified - and so
is the price consequence of moving it, because an alternate that is better on
quality and 6% dearer is a decision, not an instruction, and presenting only
half of that would be advocacy dressed as analysis.

Outputs (analytics/output/):
    supplier_scorecard.csv          per supplier: raw metrics, component scores,
                                    composite, contractual tier, measured band
    award_shift_candidates.csv      SKUs whose primary is out-scored by an
                                    approved alternate, with the spend and the
                                    price delta attached
    reject_reasons.csv              Pareto of why receipts were rejected
    supplier_scorecard_summary.json the headline figures the report and the
                                    tests read

Usage:
    python analytics/supplier_scorecard.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BRONZE = ROOT / "data" / "bronze"
OUT = Path(__file__).resolve().parent / "output"

# A receipt at or above this share of the ordered quantity counts as in full.
IN_FULL_TOLERANCE = 0.98

# Score anchors: (target, floor). Target scores 100, floor scores 0, linear
# between, clipped outside. Every one of these is an assumption and is meant to
# be argued with - which is why they are here and not buried in the arithmetic.
TARGETS = {
    "otif":           (0.95, 0.70),    # higher is better
    "reject_rate":    (0.005, 0.05),   # lower is better
    "ppv_pct":        (-0.02, 0.05),   # lower is better; below contract is a win
    "lead_sigma":     (2.0, 12.0),     # lower is better, in days
}

# What the business says each dimension is worth. Delivery leads because a
# missed delivery stops a line; cost is real but recoverable in the next
# negotiation, and a late pallet is not.
WEIGHTS = {"delivery": 0.35, "quality": 0.30, "cost": 0.20, "responsiveness": 0.15}

# A gap this wide between an approved alternate and the incumbent primary is
# worth a buyer's time. Below it, the difference is inside the noise of a year
# of purchase orders.
AWARD_SHIFT_POINTS = 8.0

BANDS = ["Tier 1", "Tier 2", "Tier 3"]


def load() -> dict:
    return {
        "po": pd.read_csv(BRONZE / "fact_purchase_orders.csv",
                          parse_dates=["order_date", "promised_date",
                                       "received_date"]),
        "suppliers": pd.read_csv(BRONZE / "dim_supplier.csv"),
        "products": pd.read_csv(BRONZE / "dim_product.csv"),
        "sourcing": pd.read_csv(BRONZE / "fact_sourcing.csv"),
    }


def enrich(po: pd.DataFrame) -> pd.DataFrame:
    """Per-line facts every metric below is built from."""
    p = po.copy()
    p["slip_days"] = (p.received_date - p.promised_date).dt.days
    p["actual_lead_days"] = (p.received_date - p.order_date).dt.days
    p["on_time"] = (p.slip_days <= 0).astype(int)
    # Accepted, not received: rejected units never make it to the shelf, so
    # they cannot count towards filling the order.
    p["qty_accepted"] = p.qty_received - p.qty_rejected
    p["in_full"] = (p.qty_accepted >= p.qty_ordered * IN_FULL_TOLERANCE).astype(int)
    p["otif"] = (p.on_time & p.in_full).astype(int)
    p["spend"] = (p.qty_received * p.unit_price).round(2)
    p["contract_spend"] = (p.qty_received * p.contract_price).round(2)
    p["ppv_dollars"] = (p.spend - p.contract_spend).round(2)
    # The money that bought stock nobody could sell.
    p["cost_of_poor_quality"] = (p.qty_rejected * p.unit_price).round(2)
    return p


def score(value: float, target: float, floor: float) -> float:
    """0-100 between a published floor and a published target.

    Handles both directions: TARGETS holds (target, floor) and the target may
    be the larger number (OTIF) or the smaller one (reject rate)."""
    if pd.isna(value):
        return np.nan
    return float(np.clip((value - floor) / (target - floor), 0, 1) * 100)


def by_supplier(p: pd.DataFrame, suppliers: pd.DataFrame) -> pd.DataFrame:
    """One row per supplier: what it did, and what that is worth as a score."""
    g = p.groupby("supplier_id")
    s = pd.DataFrame({
        "po_lines": g.size(),
        "units_ordered": g.qty_ordered.sum(),
        "units_received": g.qty_received.sum(),
        "units_rejected": g.qty_rejected.sum(),
        "spend": g.spend.sum().round(2),
        "contract_spend": g.contract_spend.sum().round(2),
        "ppv_dollars": g.ppv_dollars.sum().round(2),
        "cost_of_poor_quality": g.cost_of_poor_quality.sum().round(2),
        "on_time_rate": g.on_time.mean().round(4),
        "in_full_rate": g.in_full.mean().round(4),
        "otif_rate": g.otif.mean().round(4),
        # Population sigma: this is the whole year of orders placed with that
        # supplier, not a sample drawn from it.
        "lead_sigma": g.actual_lead_days.std(ddof=0).round(2),
        "mean_lead_days": g.actual_lead_days.mean().round(1),
    })
    # Slip on the lines that were actually late - the average over every line
    # is dragged to nothing by the on-time majority and says a supplier is
    # "0.4 days late", which is not a thing that happens to anyone.
    late = p[p.slip_days > 0].groupby("supplier_id").slip_days
    s["late_lines"] = late.size().reindex(s.index).fillna(0).astype(int)
    s["late_p90_days"] = late.quantile(0.9).reindex(s.index).round(1)
    # Rate on units, not on lines: a 2% reject on a 5,000-unit receipt is worth
    # more than a 40% reject on a 200-unit one.
    s["reject_rate"] = (s.units_rejected / s.units_received).round(4)
    # Spend-weighted by construction - both sides are dollar totals.
    s["ppv_pct"] = (s.spend / s.contract_spend - 1).round(4)

    s["delivery_score"] = [score(v, *TARGETS["otif"]) for v in s.otif_rate]
    s["quality_score"] = [score(v, *TARGETS["reject_rate"]) for v in s.reject_rate]
    s["cost_score"] = [score(v, *TARGETS["ppv_pct"]) for v in s.ppv_pct]
    s["responsiveness_score"] = [score(v, *TARGETS["lead_sigma"])
                                 for v in s.lead_sigma]
    # Round the components BEFORE weighting them, so the published total is the
    # total of the published columns. Compositing at full precision and then
    # rounding everything separately leaves a table whose columns do not add up
    # to its own bottom line by as much as a tenth of a point, and the first
    # person to check it by hand stops trusting the rest of the page.
    for c in ["delivery_score", "quality_score", "cost_score",
              "responsiveness_score"]:
        s[c] = s[c].round(1)
    s["composite_score"] = sum(
        s[f"{k}_score"] * w for k, w in WEIGHTS.items()).round(1)

    s = s.merge(suppliers.set_index("supplier_id")[
        ["supplier_name", "country", "sourcing_bloc", "supplier_tier",
         "is_qualified_alternate"]], left_index=True, right_index=True)
    s = s.sort_values("composite_score", ascending=False)
    s["measured_rank"] = range(1, len(s) + 1)
    # Thirds, so the measured band is directly comparable to the three
    # contractual tiers it is being held against.
    s["measured_band"] = pd.qcut(s.composite_score, 3,
                                 labels=BANDS[::-1]).astype(str)
    s["tier_matches_measurement"] = (
        s.measured_band == s.supplier_tier).astype(int)
    return s.reset_index()


def award_shifts(p: pd.DataFrame, s: pd.DataFrame,
                 sourcing: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """SKUs where an approved alternate out-scores the incumbent primary.

    Only qualified alternates are considered: an unqualified supplier scoring
    well on the SKUs it does hold is not evidence it can take more, and
    recommending a shift to one would skip the qualification the business
    already decided it needs."""
    sc = s.set_index("supplier_id")
    src = sourcing.merge(products[["product_id", "sku", "category"]],
                         on="product_id", how="left")
    src["composite_score"] = src.supplier_id.map(sc.composite_score)
    src["supplier_name"] = src.supplier_id.map(sc.supplier_name)
    src["is_qualified_alternate"] = src.supplier_id.map(sc.is_qualified_alternate)
    # Annual spend actually placed on this product/supplier pair, and the price
    # it was bought at - the weighted average, so a price delta can be quoted
    # per unit and multiplied back up to a total.
    agg = p.groupby(["product_id", "supplier_id"]).agg(
        spend=("spend", "sum"), units=("qty_received", "sum"))
    pair = pd.MultiIndex.from_frame(src[["product_id", "supplier_id"]])
    src["annual_spend"] = pair.map(agg.spend).fillna(0.0)
    src["unit_price_paid"] = pair.map(agg.spend / agg.units)

    rows = []
    for pid, grp in src.groupby("product_id"):
        primary = grp[grp.is_primary == 1]
        alts = grp[(grp.is_primary == 0) & (grp.is_qualified_alternate == 1)]
        if primary.empty or alts.empty:
            continue
        inc = primary.iloc[0]
        best = alts.loc[alts.composite_score.idxmax()]
        gap = float(best.composite_score - inc.composite_score)
        if gap < AWARD_SHIFT_POINTS:
            continue
        # An approved alternate that has never actually been ordered from for
        # this SKU has no price on it. That is a finding in its own right - a
        # qualified second source nobody uses - but the price consequence of
        # moving to it is genuinely unknown, and a blank is the honest way to
        # say so. Quoting one anyway, or quietly summing a NaN into the total,
        # would put a number on the page that nothing supports.
        priced = pd.notna(best.unit_price_paid) and pd.notna(inc.unit_price_paid) \
            and inc.unit_price_paid > 0
        price_delta = (float(best.unit_price_paid - inc.unit_price_paid)
                       if priced else np.nan)
        units = float(inc.annual_spend / inc.unit_price_paid) if priced else np.nan
        rows.append({
            "product_id": int(pid),
            "sku": inc.sku,
            "category": inc.category,
            "incumbent": inc.supplier_name,
            "incumbent_score": round(float(inc.composite_score), 1),
            "alternate": best.supplier_name,
            "alternate_score": round(float(best.composite_score), 1),
            "score_gap": round(gap, 1),
            "spend_at_risk": round(float(inc.annual_spend), 2),
            "price_known": int(priced),
            "unit_price_delta": round(price_delta, 2) if priced else np.nan,
            # Positive means the shift costs money; negative means it saves it.
            "price_impact": round(price_delta * units, 2) if priced else np.nan,
        })
    return (pd.DataFrame(rows).sort_values("spend_at_risk", ascending=False)
            if rows else pd.DataFrame(columns=[
                "product_id", "sku", "category", "incumbent", "incumbent_score",
                "alternate", "alternate_score", "score_gap", "spend_at_risk",
                "price_known", "unit_price_delta", "price_impact"]))


def reject_pareto(p: pd.DataFrame) -> pd.DataFrame:
    """Why receipts were rejected, by units and by money."""
    r = p[p.qty_rejected > 0]
    out = r.groupby("reject_reason").agg(
        lines=("po_id", "size"),
        units=("qty_rejected", "sum"),
        value=("cost_of_poor_quality", "sum")).sort_values(
            "value", ascending=False)
    out["value"] = out.value.round(2)
    out["share_of_value"] = (out.value / out.value.sum()).round(4)
    out["cumulative_share"] = out.share_of_value.cumsum().round(4)
    return out.reset_index()


def build() -> tuple:
    d = load()
    p = enrich(d["po"])
    s = by_supplier(p, d["suppliers"])
    shifts = award_shifts(p, s, d["sourcing"], d["products"])
    reasons = reject_pareto(p)

    spend = float(p.spend.sum())
    mismatched = s[s.tier_matches_measurement == 0]
    # The pointed version of the mismatch: a supplier the contract calls Tier 1
    # that measures in the bottom third, or the reverse.
    inverted = s[((s.supplier_tier == "Tier 1") & (s.measured_band == "Tier 3"))
                 | ((s.supplier_tier == "Tier 3") & (s.measured_band == "Tier 1"))]
    worst = s.iloc[-1]
    best = s.iloc[0]

    summary = {
        "po_lines": int(len(p)),
        "suppliers": int(s.supplier_id.nunique()),
        "skus_purchased": int(p.product_id.nunique()),
        "window_start": str(p.order_date.min().date()),
        "window_end": str(p.order_date.max().date()),
        "total_spend": round(spend, 2),
        "otif_rate": round(float(p.otif.mean()), 4),
        "on_time_rate": round(float(p.on_time.mean()), 4),
        "in_full_rate": round(float(p.in_full.mean()), 4),
        "reject_rate": round(float(p.qty_rejected.sum() / p.qty_received.sum()), 4),
        "cost_of_poor_quality": round(float(p.cost_of_poor_quality.sum()), 2),
        "ppv_dollars": round(float(p.ppv_dollars.sum()), 2),
        "ppv_pct": round(float(p.spend.sum() / p.contract_spend.sum() - 1), 4),
        "overbilled_suppliers": int((s.ppv_pct > 0).sum()),
        "overbilled_dollars": round(float(s.loc[s.ppv_pct > 0,
                                               "ppv_dollars"].sum()), 2),
        "best_supplier": best.supplier_name,
        "best_score": float(best.composite_score),
        "worst_supplier": worst.supplier_name,
        "worst_score": float(worst.composite_score),
        "worst_otif": float(worst.otif_rate),
        "tier_mismatches": int(len(mismatched)),
        "tier_inversions": int(len(inverted)),
        "inverted_suppliers": inverted.supplier_name.tolist(),
        "spend_under_mismatched_tier": round(float(mismatched.spend.sum()), 2),
        "award_shift_skus": int(len(shifts)),
        "award_shift_spend": round(float(shifts.spend_at_risk.sum()), 2),
        "award_shift_price_impact": round(float(
            shifts.loc[shifts.price_known == 1, "price_impact"].sum()), 2),
        "award_shift_cheaper_skus": int((shifts.price_impact < 0).sum()),
        # Qualified alternates that have never been ordered from for the SKU
        # they are being proposed for: a live second source sitting unused.
        "award_shift_unpriced_skus": int((shifts.price_known == 0).sum()),
        "award_shift_unpriced_spend": round(float(
            shifts.loc[shifts.price_known == 0, "spend_at_risk"].sum()), 2),
        "top_reject_reason": reasons.iloc[0].reject_reason,
        "top_reject_share": float(reasons.iloc[0].share_of_value),
        "weights": WEIGHTS,
        "targets": {k: list(v) for k, v in TARGETS.items()},
    }
    return summary, s, shifts, reasons


def headline(summary: dict) -> pd.DataFrame:
    """The summary as a one-row table.

    Power BI needs somewhere to read a scalar from, and the alternative - a
    measure that aggregates a table which already holds a total - is how a
    report ends up double-counting its own roll-ups. One row cannot be summed
    twice."""
    scalars = {k: v for k, v in summary.items()
               if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    return pd.DataFrame([scalars])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    summary, s, shifts, reasons = build()

    s.to_csv(OUT / "supplier_scorecard.csv", index=False)
    shifts.to_csv(OUT / "award_shift_candidates.csv", index=False)
    reasons.to_csv(OUT / "reject_reasons.csv", index=False)
    (OUT / "supplier_scorecard_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    headline(summary).to_csv(OUT / "supplier_scorecard_headline.csv", index=False)

    print()
    print("=" * 70)
    print("SUPPLIER SCORECARD" + f"   ({summary['window_start']} to "
          f"{summary['window_end']})")
    print("=" * 70)
    print(f"  {summary['po_lines']:,} purchase order lines, "
          f"{summary['suppliers']} suppliers, "
          f"${summary['total_spend']:,.0f} of inbound spend")
    print(f"  OTIF                           {summary['otif_rate']:.1%}"
          f"   (on time {summary['on_time_rate']:.1%}, "
          f"in full {summary['in_full_rate']:.1%})")
    print(f"  rejected at receipt            {summary['reject_rate']:.2%} of units"
          f"   = ${summary['cost_of_poor_quality']:,.0f} of stock condemned")
    print(f"  purchase price variance        ${summary['ppv_dollars']:,.0f}"
          f"   ({summary['ppv_pct']:+.2%} against contract)")
    print(f"  {summary['overbilled_suppliers']} suppliers invoiced above their "
          f"contract, together ${summary['overbilled_dollars']:,.0f}")
    print()
    print("THE PANEL, SCORED")
    print("=" * 70)
    print(f"  {'supplier':22s} {'del':>5s} {'qual':>5s} {'cost':>5s} "
          f"{'resp':>5s} {'TOTAL':>6s}  {'tier':7s} {'measured':8s}")
    for _, r in s.iterrows():
        flag = "  <- mismatch" if not r.tier_matches_measurement else ""
        print(f"  {r.supplier_name[:22]:22s} {r.delivery_score:5.0f} "
              f"{r.quality_score:5.0f} {r.cost_score:5.0f} "
              f"{r.responsiveness_score:5.0f} {r.composite_score:6.1f}  "
              f"{r.supplier_tier:7s} {r.measured_band:8s}{flag}")
    print()
    print("TIER IS A CLAIM. THE SCORE IS A MEASUREMENT.")
    print("=" * 70)
    print(f"  {summary['tier_mismatches']} of {summary['suppliers']} suppliers "
          f"sit in a different band than their contract says,")
    print(f"  covering ${summary['spend_under_mismatched_tier']:,.0f} of spend.")
    if summary["inverted_suppliers"]:
        print(f"  {summary['tier_inversions']} are inverted outright: "
              + ", ".join(summary["inverted_suppliers"]))
    print()
    print("WHERE THE AWARD DOES NOT MATCH THE PERFORMANCE")
    print("=" * 70)
    print(f"  {summary['award_shift_skus']} SKUs have a qualified alternate "
          f"scoring {AWARD_SHIFT_POINTS:.0f}+ points above the incumbent,")
    print(f"  covering ${summary['award_shift_spend']:,.0f} of annual spend.")
    print(f"  Moving all of it would change the purchase price by "
          f"${summary['award_shift_price_impact']:+,.0f} - "
          f"{summary['award_shift_cheaper_skus']} of those SKUs")
    print("  are cheaper at the better supplier, so those are free.")
    if summary["award_shift_unpriced_skus"]:
        print(f"  {summary['award_shift_unpriced_skus']} more "
              f"(${summary['award_shift_unpriced_spend']:,.0f}) have a qualified "
              "alternate that has never")
        print("  been ordered from for that SKU - no price exists to compare, "
              "which is")
        print("  itself worth a buyer's attention.")
    print()
    for _, r in shifts.head(6).iterrows():
        if r.price_known:
            money = (f"{'costs' if r.price_impact > 0 else 'saves'} "
                     f"${abs(r.price_impact):,.0f}")
        else:
            money = "never ordered from"
        print(f"    {r.sku:10s} {r.incumbent[:18]:18s} {r.incumbent_score:5.1f}"
              f"  ->  {r.alternate[:18]:18s} {r.alternate_score:5.1f}"
              f"   ${r.spend_at_risk:>9,.0f}   {money}")
    print()
    print("WHY RECEIPTS WERE REJECTED")
    print("=" * 70)
    for _, r in reasons.iterrows():
        print(f"    {r.reject_reason:34s} ${r.value:>10,.0f}   "
              f"{r.share_of_value:5.1%}   (cum {r.cumulative_share:.0%})")
    print()
    print(f"wrote 5 files to {OUT}")


if __name__ == "__main__":
    main()
