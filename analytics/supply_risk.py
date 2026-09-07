"""
Global sourcing risk and service decomposition.

The order and inventory facts describe one country's distribution network.
They cannot answer the questions an MNC control tower exists to answer: how
concentrated is the supply base, which SKUs have nowhere else to go, and how
long a switch takes when a lane closes. This module answers those from the
approved vendor list, and decomposes the service number the network already
reports into the two failures that make it up.

What it computes
----------------
1. Perfect-order decomposition. OTIF is an AND of two independent failures;
   reporting only the product hides which one to fix. Splits on-time and
   in-full, and Paretos the failure reasons.

2. Award concentration. Herfindahl index over allocation share per SKU, and the
   count and COGS of SKUs with a single approved source.

3. Country and bloc exposure. COGS-weighted, because a supplier that holds 3%
   of the awards on a SKU nobody buys is not exposure.

4. Disruption scenario, per country. If that country's lanes close: how much
   COGS is exposed, how many SKUs are STRANDED (no qualified alternate
   anywhere else), and the mean contract lead time of the alternates the rest
   would have to switch to. A qualified alternate can absorb volume; an
   unqualified one needs a requalification programme first, so the two are
   counted separately rather than being averaged into a comforting number.

5. Lead-time exposure. Demand-weighted contract lead time, and the offshore
   trade-off: the unit-cost saving bought with those extra days.

What it does NOT compute, and why
---------------------------------
   * Landed cost and freight. No freight, duty or FX data exists in this model,
     and a landed cost built from a made-up freight rate would be a decoration.
   * Cash-to-cash cycle. Inventory days are here; DPO and DSO are not, and
     two thirds of a cycle is not a cycle.

Outputs (analytics/output/):
    perfect_order.csv          on-time / in-full / OTIF
    perfect_order_failures.csv which of the two failures actually happened
    sourcing_concentration.csv per-SKU award HHI and source count
    country_exposure.csv       COGS exposure and disruption scenario per country
    supply_risk_summary.json   the headline figures the report and tests read

Usage:
    python analytics/supply_risk.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BRONZE = ROOT / "data" / "bronze"
OUT = Path(__file__).resolve().parent / "output"

# The repo's canonical OTIF, matching pipeline/run_pipeline.py: a line is in
# full at 95% fill, not at 100%. Redefining it here would make this module
# disagree with the dashboard, which is the failure this project is about.
FILL_TOLERANCE = 0.95


def load():
    orders = pd.read_csv(BRONZE / "fact_orders.csv",
                         parse_dates=["order_date", "promised_date", "shipped_date"])
    return {
        "orders": orders,
        "sourcing": pd.read_csv(BRONZE / "fact_sourcing.csv"),
        "suppliers": pd.read_csv(BRONZE / "dim_supplier.csv"),
        "products": pd.read_csv(BRONZE / "dim_product.csv"),
    }


def perfect_order(orders: pd.DataFrame) -> pd.DataFrame:
    """Split OTIF into the two failures it hides."""
    fill = np.where(orders.qty_ordered > 0, orders.qty_shipped / orders.qty_ordered, 1.0)
    on_time = orders.shipped_date <= orders.promised_date
    in_full = fill >= FILL_TOLERANCE
    days_late = (orders.shipped_date - orders.promised_date).dt.days.clip(lower=0)

    n = len(orders)
    rows = [
        ("On time", int(on_time.sum()), on_time.mean()),
        ("In full", int(in_full.sum()), in_full.mean()),
        ("OTIF", int((on_time & in_full).sum()), (on_time & in_full).mean()),
    ]
    out = pd.DataFrame(rows, columns=["measure", "orders", "rate"])

    failures = pd.DataFrame([
        ("Late only", int((~on_time & in_full).sum())),
        ("Short only", int((on_time & ~in_full).sum())),
        ("Late and short", int((~on_time & ~in_full).sum())),
    ], columns=["measure", "orders"])
    failures["rate"] = failures.orders / n
    out = pd.concat([out, failures], ignore_index=True)
    out["mean_days_late"] = np.nan
    out.loc[out.measure == "OTIF", "mean_days_late"] = round(
        float(days_late[~on_time].mean()), 2)
    return out


def cogs_by_product(orders: pd.DataFrame) -> pd.Series:
    return (orders.qty_shipped * orders.unit_cost).groupby(orders.product_id).sum()


def concentration(sourcing: pd.DataFrame, cogs: pd.Series,
                  products: pd.DataFrame) -> pd.DataFrame:
    g = sourcing.groupby("product_id")
    out = pd.DataFrame({
        "sources": g.supplier_id.nunique(),
        "award_hhi": (g.allocation_share.apply(lambda s: (s ** 2).sum()) * 10000).round(0),
        "primary_share": g.allocation_share.max().round(4),
        "max_contract_lead_days": g.contract_lead_days.max(),
    })
    out["cogs"] = cogs.reindex(out.index).fillna(0).round(2)
    out["is_single_source"] = (out.sources == 1).astype(int)
    return out.join(products.set_index("product_id")[["sku", "category"]]).reset_index()


def country_exposure(sourcing, suppliers, cogs) -> pd.DataFrame:
    """COGS exposure per origin, and what a closed lane would actually cost."""
    s = sourcing.merge(suppliers, on="supplier_id")
    s["cogs"] = s.product_id.map(cogs).fillna(0)
    s["award_cogs"] = s.cogs * s.allocation_share
    total = s.award_cogs.sum()

    rows = []
    for country, hit in s.groupby("country"):
        skus = set(hit.product_id)
        # Only a QUALIFIED alternate outside the country can absorb the volume.
        alternates = s[(s.product_id.isin(skus))
                       & (s.country != country)
                       & (s.is_qualified_alternate == 1)]
        stranded = skus - set(alternates.product_id)
        switch = alternates.groupby("product_id").contract_lead_days.min()
        rows.append({
            "country": country,
            "bloc": hit.sourcing_bloc.iloc[0],
            "suppliers": hit.supplier_id.nunique(),
            "skus_supplied": len(skus),
            "exposed_cogs": round(float(hit.award_cogs.sum()), 2),
            "exposed_share": round(float(hit.award_cogs.sum() / total), 4),
            "stranded_skus": len(stranded),
            "stranded_cogs": round(float(cogs.reindex(list(stranded)).fillna(0).sum()), 2),
            "mean_switch_lead_days": round(float(switch.mean()), 1) if len(switch) else None,
        })
    return pd.DataFrame(rows).sort_values("exposed_cogs", ascending=False)


def lead_time_exposure(sourcing, suppliers, cogs) -> dict:
    s = sourcing.merge(suppliers, on="supplier_id")
    s["cogs"] = s.product_id.map(cogs).fillna(0)
    w = s.cogs * s.allocation_share
    if w.sum() == 0:
        return {}
    domestic = s.sourcing_bloc == "North America"
    return {
        "demand_weighted_lead_days": round(float((s.contract_lead_days * w).sum() / w.sum()), 1),
        "nearshore_lead_days": round(float((s.contract_lead_days[domestic] * w[domestic]).sum()
                                           / w[domestic].sum()), 1),
        "offshore_lead_days": round(float((s.contract_lead_days[~domestic] * w[~domestic]).sum()
                                          / w[~domestic].sum()), 1),
        "offshore_price_index": round(float((s.price_index[~domestic] * w[~domestic]).sum()
                                            / w[~domestic].sum()), 3),
        "nearshore_price_index": round(float((s.price_index[domestic] * w[domestic]).sum()
                                             / w[domestic].sum()), 3),
        "offshore_cogs_share": round(float(w[~domestic].sum() / w.sum()), 4),
    }


def build():
    d = load()
    cogs = cogs_by_product(d["orders"])

    po = perfect_order(d["orders"])
    conc = concentration(d["sourcing"], cogs, d["products"])
    ctry = country_exposure(d["sourcing"], d["suppliers"], cogs)
    lead = lead_time_exposure(d["sourcing"], d["suppliers"], cogs)

    single = conc[conc.is_single_source == 1]
    worst = ctry.iloc[0]
    shares = ctry.exposed_share
    summary = {
        "orders": int(len(d["orders"])),
        "on_time_rate": float(po.loc[po.measure == "On time", "rate"].iloc[0]),
        "in_full_rate": float(po.loc[po.measure == "In full", "rate"].iloc[0]),
        "otif_rate": float(po.loc[po.measure == "OTIF", "rate"].iloc[0]),
        "otif_orders": int(po.loc[po.measure == "OTIF", "orders"].iloc[0]),
        "late_only": int(po.loc[po.measure == "Late only", "orders"].iloc[0]),
        "short_only": int(po.loc[po.measure == "Short only", "orders"].iloc[0]),
        "late_and_short": int(po.loc[po.measure == "Late and short", "orders"].iloc[0]),
        "mean_days_late": float(po.loc[po.measure == "OTIF", "mean_days_late"].iloc[0]),
        "skus": int(len(conc)),
        "single_source_skus": int(len(single)),
        "single_source_cogs": round(float(single.cogs.sum()), 2),
        "single_source_cogs_share": round(float(single.cogs.sum() / cogs.sum()), 4),
        "median_award_hhi": float(conc.award_hhi.median()),
        "countries": int(len(ctry)),
        "country_hhi": round(float((shares ** 2).sum() * 10000), 0),
        "largest_origin": worst.country,
        "largest_origin_share": float(worst.exposed_share),
        "largest_origin_stranded_skus": int(worst.stranded_skus),
        "largest_origin_switch_days": float(worst.mean_switch_lead_days),
        **lead,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    rates = po[po.measure.isin(["On time", "In full", "OTIF"])]
    failures = po[~po.measure.isin(["On time", "In full", "OTIF"])]
    rates.round({"rate": 4}).to_csv(OUT / "perfect_order.csv", index=False)
    failures.round({"rate": 4}).drop(columns=["mean_days_late"]).to_csv(
        OUT / "perfect_order_failures.csv", index=False)
    conc.to_csv(OUT / "sourcing_concentration.csv", index=False)
    ctry.to_csv(OUT / "country_exposure.csv", index=False)
    (OUT / "supply_risk_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary, po, conc, ctry


def main():
    summary, po, conc, ctry = build()
    print("PERFECT ORDER")
    print("=" * 62)
    for _, r in po.iterrows():
        print(f"  {r.measure:16s} {r.orders:>6,}  {r.rate:>7.1%}")
    print(f"  mean days late on a late order: {summary['mean_days_late']}")
    print()
    print("SOURCING CONCENTRATION")
    print("=" * 62)
    print(f"  SKUs                          {summary['skus']}")
    print(f"  single-source SKUs            {summary['single_source_skus']}"
          f"  (${summary['single_source_cogs']:,.0f}, "
          f"{summary['single_source_cogs_share']:.1%} of COGS)")
    print(f"  median award HHI per SKU      {summary['median_award_hhi']:,.0f}")
    print(f"  country HHI                   {summary['country_hhi']:,.0f}"
          f"   ({summary['countries']} origins)")
    print()
    print("IF A LANE CLOSES")
    print("=" * 62)
    print(f"  {'origin':14s} {'exposed COGS':>14s} {'share':>7s} {'SKUs':>5s}"
          f" {'stranded':>9s} {'switch':>8s}")
    for _, r in ctry.iterrows():
        sw = "-" if pd.isna(r.mean_switch_lead_days) else f"{r.mean_switch_lead_days:.1f}d"
        print(f"  {r.country:14s} ${r.exposed_cogs:>13,.0f} {r.exposed_share:>7.1%}"
              f" {r.skus_supplied:>5d} {r.stranded_skus:>9d} {sw:>8s}")
    print()
    print("LEAD-TIME EXPOSURE")
    print("=" * 62)
    print(f"  demand-weighted contract lead {summary['demand_weighted_lead_days']} days")
    print(f"  nearshore {summary['nearshore_lead_days']}d at index "
          f"{summary['nearshore_price_index']} vs offshore "
          f"{summary['offshore_lead_days']}d at {summary['offshore_price_index']}")
    print(f"  offshore carries {summary['offshore_cogs_share']:.1%} of COGS")
    print()
    print(f"wrote 4 files to {OUT}")


if __name__ == "__main__":
    main()
