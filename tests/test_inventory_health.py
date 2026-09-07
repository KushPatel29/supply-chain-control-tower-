"""
Invariants for the inventory and network health layer.

The failure to guard against is an inventory number that is merely plausible.
A reorder point that quietly drops the lead-time term, a shortfall that nets
against a surplus in a different warehouse, a transfer that moves more units
than exist - all of them render, and all of them would send someone to buy the
wrong thing.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analytics import inventory_health

ROOT = Path(__file__).resolve().parent.parent

# Read the committed summary before the fixture rebuilds it, or the check that
# the published figures are current can never fail.
_COMMITTED = json.loads(
    (ROOT / "analytics" / "output" / "inventory_health_summary.json")
    .read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built():
    return inventory_health.build()


@pytest.fixture(scope="module")
def raw():
    return inventory_health.load()


# --- the arithmetic -------------------------------------------------------

def test_reorder_point_carries_both_sources_of_variability(raw):
    """King's formula, re-derived independently of the generator.

    Safety stock must include the lead time's OWN variability, not just
    demand's. Dropping that term is the standard way a safety stock ends up too
    small on a long offshore lane - exactly where it matters most.

    Checked against BOTH formulas: it must match the full one and must NOT
    match the demand-only one. A "greater than" assertion alone passes either
    way, because rounding to whole units nudges half the rows up regardless.
    """
    pos = raw["position"]
    cur = pos[pos.week_start == pos.week_start.max()]
    sourcing = pd.read_csv(ROOT / "data" / "bronze" / "fact_sourcing.csv")
    sigma_l = sourcing.groupby("product_id").lead_time_sigma_days.max()
    z = 1.645

    demand_only = z * np.sqrt(cur.lead_days * cur.demand_sigma ** 2)
    full = z * np.sqrt(cur.lead_days * cur.demand_sigma ** 2
                       + (cur.avg_daily_demand ** 2)
                       * cur.product_id.map(sigma_l) ** 2)

    assert np.allclose(cur.safety_stock_target, np.round(full), atol=1.5)
    assert (np.abs(cur.safety_stock_target - np.round(demand_only)) > 1).mean() > 0.9, \
        "safety stock is indistinguishable from the demand-only formula"

def test_reorder_point_is_cycle_stock_plus_safety_stock(raw):
    pos = raw["position"]
    cur = pos[pos.week_start == pos.week_start.max()]
    cycle = cur.avg_daily_demand * cur.lead_days
    assert np.allclose(cur.reorder_point, np.round(cycle + cur.safety_stock_target),
                       atol=1.5)


def test_gap_and_excess_never_apply_to_the_same_position(built):
    """A position is short or long, never both: the thresholds must not overlap."""
    cur = built[1]
    assert not ((cur.gap_units > 0) & (cur.excess_units > 0)).any()


def test_gap_value_reconciles_to_units_times_cost(built):
    cur = built[1]
    assert np.allclose(cur.gap_value, (cur.gap_units * cur.unit_cost).round(2), atol=0.02)


def test_abc_classes_cover_every_sku_and_a_is_the_biggest_slice(built, raw):
    cur = built[1]
    assert cur.abc_class.notna().all()
    counts = cur.drop_duplicates("product_id").abc_class.value_counts()
    assert counts.get("A", 0) >= counts.get("C", 0), "Pareto is upside down"


# --- transfers ------------------------------------------------------------

def test_a_transfer_never_moves_more_than_exists(built):
    """Transferable is the MINIMUM of the shortage and the surplus. If it ever
    exceeded either, the plan would move units that are not there."""
    cur, tr = built[1], built[2]
    per_sku = cur.groupby("product_id").agg(short=("gap_units", "sum"),
                                            spare=("excess_units", "sum"))
    for _, r in tr.iterrows():
        pid = cur.loc[cur.sku == r.sku, "product_id"].iloc[0]
        assert r.transferable_units <= per_sku.loc[pid, "short"] + 1
        assert r.transferable_units <= per_sku.loc[pid, "spare"] + 1


def test_transfer_value_cannot_exceed_the_replenishment_gap(built):
    """Moving stock can only ever cover part of the shortfall; if it covered
    more, the shortage and the surplus are being counted twice."""
    s = built[0]
    assert 0 < s["transfer_value"] <= s["replenishment_gap_value"]
    assert 0 < s["transfer_share_of_gap"] <= 1.0


def test_every_transfer_sku_is_short_somewhere_and_long_somewhere(built):
    cur, tr = built[1], built[2]
    for sku in tr.sku:
        rows = cur[cur.sku == sku]
        assert (rows.gap_units > 0).any(), f"{sku} is not short anywhere"
        assert (rows.excess_units > 0).any(), f"{sku} is not long anywhere"


# --- cover vs lead --------------------------------------------------------

def test_cover_is_compared_to_each_position_own_lead_time(built):
    """20 days of cover is comfortable on a 6-day lane and a stockout on a
    40-day one, so the flag must use the position's own lead, not an average."""
    cur = built[1]
    flagged = cur[cur.covers_lead_time == 0]
    assert (flagged.cover_days < flagged.lead_days).all()
    ok = cur[cur.covers_lead_time == 1]
    assert (ok.cover_days >= ok.lead_days).all()
    assert cur.lead_days.nunique() > 1, "one lead time for everything proves nothing"


# --- published figures ----------------------------------------------------

def test_the_published_summary_matches_the_committed_json(built):
    assert _COMMITTED == built[0]


def test_headline_figures_are_what_the_readme_claims(built):
    s = built[0]
    assert s["positions"] == 478
    assert s["skus"] == 60
    assert s["warehouses"] == 8
    assert s["below_reorder_point"] == 267
    assert s["transfer_skus"] == 33
    assert s["median_cover_days"] == pytest.approx(47.3, abs=0.2)
    assert s["median_lead_days"] == pytest.approx(40, abs=0.5)
    assert s["replenishment_gap_value"] == pytest.approx(2_271_670, rel=1e-4)
    assert s["transfer_value"] == pytest.approx(438_318, rel=1e-4)


def test_a_quarter_of_the_gap_is_a_move_not_a_purchase(built):
    """The finding the page leads with. If the network ever stops holding a
    coverable surplus, this fails rather than the claim quietly ageing."""
    s = built[0]
    assert s["transfer_share_of_gap"] >= 0.15


# --- the trend ------------------------------------------------------------

def test_the_trend_reconciles_to_the_snapshot(built):
    """Last week of the trend must be the snapshot, or the page shows two
    different numbers for the same week and neither can be trusted."""
    s, _, _, trend = built
    last = trend[trend.week_start == trend.week_start.max()]
    assert last.positions.sum() == s["positions"]
    assert last.gap_value.sum() == pytest.approx(s["replenishment_gap_value"], abs=0.5)
    assert last.on_hand_value.sum() == pytest.approx(s["on_hand_value"], abs=0.5)
    assert last.below_reorder_point.sum() == s["below_reorder_point"]


def test_every_week_carries_both_lanes(built):
    trend = built[3]
    per_week = trend.groupby("week_start").lane.nunique()
    assert (per_week == 2).all(), "a missing lane silently zeroes a series"
    assert trend.week_start.nunique() == 13


def test_the_lane_split_is_the_finding_not_the_headline(built):
    """The page's claim: the total barely moves while the composition rotates.

    Three things have to hold together or the claim is wrong - total stock about
    flat, the offshore gap growing, the nearshore gap shrinking. Asserted
    separately so a failure says which half of the story broke.
    """
    s = built[0]
    assert abs(s["on_hand_value_change"]) < 0.05, "total stock moved after all"
    assert s["offshore_gap_change"] > 0.10
    assert s["nearshore_gap_change"] < -0.10
    assert s["nearshore_on_hand_change"] > 0.10


def test_the_governing_lane_is_the_slowest_approved_source(raw):
    """A dual-sourced SKU is planned against its slowest source, so that is the
    lane it belongs to. Averaging the two would move SKUs to the wrong band and
    quietly flatter the offshore number."""
    d = inventory_health.load()
    lane = inventory_health.planning_lane(d)
    s = d["sourcing"].merge(d["suppliers"][["supplier_id", "sourcing_bloc"]],
                            on="supplier_id", how="left")
    dual = s.groupby("product_id").supplier_id.nunique()
    mixed = [p for p in dual[dual > 1].index
             if s[s.product_id == p].sourcing_bloc.nunique() > 1]
    assert mixed, "no mixed-bloc SKU exists, so this proves nothing"
    for pid in mixed:
        rows = s[s.product_id == pid]
        slowest = rows.loc[rows.contract_lead_days.idxmax()]
        expected = ("Offshore" if slowest.sourcing_bloc != inventory_health.DOMESTIC_BLOC
                    else "Nearshore")
        assert lane[pid] == expected


def test_the_shortfall_is_concentrated_on_a_class(built):
    """A dollar short on an A item is not the same dollar as on a C item, and
    the report says the shortfall is concentrated. Hold it to that."""
    s = built[0]
    assert s["gap_value_a_class"] / s["replenishment_gap_value"] > 0.5
