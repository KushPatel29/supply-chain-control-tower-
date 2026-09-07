"""
The supplier scorecard has to measure suppliers, not restate what it was told.

Two of these tests exist because the failure they catch would be invisible in
the output. A scorecard that quietly re-reads supplier_tier produces a
beautifully consistent report in which every Tier 1 supplier scores well, and
nobody ever finds out it learned nothing - so test_the_contractual_tier_is_not
_an_input shuffles the tier column and demands that not one score moves. And a
min-max scorecard always crowns somebody, so a panel that is failing across the
board looks identical to one that is excellent - so the anchors are pinned to
fixed, published numbers instead.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analytics"))

import supplier_scorecard as sc  # noqa: E402

BRONZE = ROOT / "data" / "bronze"
PUBLISHED = ROOT / "analytics" / "output" / "supplier_scorecard_summary.json"


@pytest.fixture(scope="module")
def built():
    return sc.build()


@pytest.fixture(scope="module")
def lines():
    return sc.enrich(pd.read_csv(
        BRONZE / "fact_purchase_orders.csv",
        parse_dates=["order_date", "promised_date", "received_date"]))


# --- the receipts themselves ------------------------------------------------

def test_no_receipt_exceeds_what_was_ordered(lines):
    assert (lines.qty_received <= lines.qty_ordered).all()


def test_nothing_is_rejected_that_was_not_received(lines):
    assert (lines.qty_rejected <= lines.qty_received).all()
    assert (lines.qty_rejected >= 0).all()


def test_a_rejection_always_carries_a_reason(lines):
    """A condemned pallet with no reason on it cannot be actioned, and it turns
    the Pareto into a chart with an 'unknown' bar taller than the rest."""
    rejected = lines[lines.qty_rejected > 0]
    assert len(rejected) > 0
    assert rejected.reject_reason.notna().all()
    assert (rejected.reject_reason.str.len() > 0).all()
    # ...and the converse: no reason on a receipt that was accepted whole.
    clean = lines[lines.qty_rejected == 0]
    assert clean.reject_reason.fillna("").eq("").all()


def test_every_po_is_promised_after_it_is_ordered(lines):
    assert (lines.promised_date > lines.order_date).all()


def test_in_full_is_measured_on_the_accepted_quantity(lines):
    """The distinction the module was built around: a receipt that arrives
    complete and is then half condemned did not fill the order."""
    complete_but_condemned = lines[(lines.qty_received == lines.qty_ordered)
                                   & (lines.qty_rejected
                                      > lines.qty_ordered * 0.02)]
    assert len(complete_but_condemned) > 0, (
        "no receipt arrives complete and is then rejected - this test proves "
        "nothing on this dataset")
    assert (complete_but_condemned.in_full == 0).all()


def test_otif_cannot_exceed_either_half_of_itself(lines):
    assert lines.otif.sum() <= lines.on_time.sum()
    assert lines.otif.sum() <= lines.in_full.sum()


# --- the scoring ------------------------------------------------------------

def test_the_score_is_anchored_not_relative():
    """A value at the published target scores 100, at the floor scores 0, and
    beyond either is clipped. Nothing about the rest of the panel enters."""
    for target, floor in sc.TARGETS.values():
        assert sc.score(target, target, floor) == pytest.approx(100)
        assert sc.score(floor, target, floor) == pytest.approx(0)
        beyond = target + (target - floor)
        assert sc.score(beyond, target, floor) == pytest.approx(100)
        below = floor - (target - floor)
        assert sc.score(below, target, floor) == pytest.approx(0)
        midpoint = (target + floor) / 2
        assert sc.score(midpoint, target, floor) == pytest.approx(50)


def test_nobody_is_crowned_by_construction(built):
    """The tell of a min-max scorecard: someone always scores 100 and someone
    always scores 0. Here the whole panel can be mediocre, and is."""
    _, s, _, _ = built
    assert s.composite_score.max() < 100
    assert s.composite_score.min() > 0
    # Delivery is this panel's weak axis and the scale says so out loud.
    assert s.delivery_score.max() < 100


def test_the_published_columns_add_up_to_the_published_total(built):
    """Somebody will check this by hand with a calculator. When they do, the
    four columns on the page must produce the total on the page - not a number
    0.06 away from it that came from unrounded intermediates."""
    _, s, _, _ = built
    assert sum(sc.WEIGHTS.values()) == pytest.approx(1.0)
    rebuilt = sum(s[f"{k}_score"] * w for k, w in sc.WEIGHTS.items())
    assert np.allclose(rebuilt.round(1), s.composite_score, atol=1e-9)


def test_the_contractual_tier_is_not_an_input():
    """Shuffle the tier column and every score must be untouched.

    If this ever fails, the scorecard is grading suppliers on the label the
    contract gave them, and the tier-vs-measurement finding is circular."""
    d = sc.load()
    p = sc.enrich(d["po"])
    honest = sc.by_supplier(p, d["suppliers"]).set_index("supplier_id")

    scrambled = d["suppliers"].copy()
    rng = np.random.default_rng(7)
    scrambled["supplier_tier"] = rng.permutation(scrambled.supplier_tier.values)
    lied_to = sc.by_supplier(p, scrambled).set_index("supplier_id")

    for col in ["delivery_score", "quality_score", "cost_score",
                "responsiveness_score", "composite_score", "measured_band"]:
        assert (honest[col] == lied_to[col]).all(), (
            f"{col} moved when the contractual tier was shuffled")


def test_the_reject_rate_is_on_units_not_on_lines(built):
    """A 40% rejection on a 200-unit receipt must not outweigh a 2% rejection
    on a 5,000-unit one."""
    _, s, _, _ = built
    d = sc.load()
    p = sc.enrich(d["po"])
    direct = (p.groupby("supplier_id").qty_rejected.sum()
              / p.groupby("supplier_id").qty_received.sum())
    assert np.allclose(s.set_index("supplier_id").reject_rate,
                       direct.reindex(s.supplier_id).values, atol=1e-4)


def test_purchase_price_variance_reconciles_to_the_line_level(built, lines):
    _, s, _, _ = built
    assert s.ppv_dollars.sum() == pytest.approx(lines.ppv_dollars.sum(), abs=1.0)
    assert np.allclose(s.spend - s.contract_spend, s.ppv_dollars, atol=1.0)


def test_price_variance_runs_in_both_directions(built):
    """A panel where every supplier over-bills is a data artefact, not a
    finding - and a scorecard that can only report leakage is an accusation
    with a chart attached."""
    _, s, _, _ = built
    assert (s.ppv_pct > 0).any(), "nobody invoices above contract"
    assert (s.ppv_pct < 0).any(), "nobody invoices below contract"


# --- the recommendations ----------------------------------------------------

def test_award_shifts_only_name_qualified_alternates(built):
    _, s, shifts, _ = built
    qualified = set(s.loc[s.is_qualified_alternate == 1, "supplier_name"])
    assert set(shifts.alternate) <= qualified


def test_award_shifts_clear_the_published_threshold(built):
    _, _, shifts, _ = built
    assert (shifts.score_gap >= sc.AWARD_SHIFT_POINTS).all()
    assert (shifts.alternate_score > shifts.incumbent_score).all()


def test_the_price_consequence_is_reported_in_both_directions(built):
    """A shift recommendation that only ever showed savings would be advocacy.
    Some of these cost money, and the page has to say so."""
    _, _, shifts, _ = built
    priced = shifts[shifts.price_known == 1]
    assert (priced.price_impact > 0).any(), "no shift costs anything"
    assert (priced.price_impact < 0).any(), "no shift saves anything"
    # Sign of the total must follow the sign of the per-unit delta.
    assert (np.sign(priced.price_impact)
            == np.sign(priced.unit_price_delta)).all()


def test_an_alternate_that_has_never_shipped_gets_no_invented_price(built):
    """An approved alternate with no purchase history for the SKU it is being
    proposed for has no price to compare against. The row stays - a qualified
    second source nobody uses is worth knowing about - but the price columns
    are blank and the blank is excluded from the headline, rather than summed
    in as a NaN or filled with a plausible-looking guess."""
    summary, _, shifts, _ = built
    unpriced = shifts[shifts.price_known == 0]
    assert len(unpriced) > 0, (
        "no unpriced alternate in this data - this test proves nothing")
    assert unpriced.unit_price_delta.isna().all()
    assert unpriced.price_impact.isna().all()
    assert summary["award_shift_price_impact"] == pytest.approx(
        float(shifts.loc[shifts.price_known == 1, "price_impact"].sum()), abs=1.0)
    assert not np.isnan(summary["award_shift_price_impact"])
    assert summary["award_shift_unpriced_skus"] == len(unpriced)

    po = pd.read_csv(BRONZE / "fact_purchase_orders.csv")
    sup = pd.read_csv(BRONZE / "dim_supplier.csv")
    ordered = set(zip(po.product_id, po.supplier_id))
    name_to_id = dict(zip(sup.supplier_name, sup.supplier_id))
    for _, r in unpriced.iterrows():
        assert (r.product_id, name_to_id[r.alternate]) not in ordered


def test_the_incumbent_is_always_the_primary_source(built):
    _, _, shifts, _ = built
    src = pd.read_csv(BRONZE / "fact_sourcing.csv")
    sup = pd.read_csv(BRONZE / "dim_supplier.csv").set_index("supplier_id")
    primaries = (src[src.is_primary == 1].set_index("product_id")
                 .supplier_id.map(sup.supplier_name))
    for _, r in shifts.iterrows():
        assert primaries[r.product_id] == r.incumbent


def test_the_reject_pareto_accounts_for_every_rejected_dollar(built, lines):
    _, _, _, reasons = built
    assert reasons.value.sum() == pytest.approx(
        lines.cost_of_poor_quality.sum(), abs=1.0)
    assert reasons.share_of_value.sum() == pytest.approx(1.0, abs=1e-3)
    assert reasons.cumulative_share.iloc[-1] == pytest.approx(1.0, abs=1e-3)
    assert reasons.value.is_monotonic_decreasing


# --- what the README and the report quote -----------------------------------

def test_the_published_summary_is_what_this_code_produces(built):
    """The figures in the README come from the committed JSON. If the code and
    that file ever disagree, the document is quoting a run nobody can
    reproduce."""
    summary, _, _, _ = built
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    for key, value in summary.items():
        if isinstance(value, float):
            assert published[key] == pytest.approx(value, rel=1e-6), key
        else:
            assert published[key] == value, key
