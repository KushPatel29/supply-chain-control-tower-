"""
Invariants for the global sourcing risk layer.

The dangerous failure here is not a crash, it is a plausible number. A
concentration index that quietly stops summing, or an OTIF that drifts away
from the one the pipeline publishes, would still render and still look
authoritative. These pin the arithmetic and the reconciliation.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analytics import supply_risk

ROOT = Path(__file__).resolve().parent.parent
BRONZE = ROOT / "data" / "bronze"

# Read the COMMITTED summary before anything rebuilds it. The build() fixture
# overwrites this file, so comparing afterwards could never fail - the test
# would have passed on a stale committed figure forever.
_COMMITTED_SUMMARY = json.loads(
    (ROOT / "analytics" / "output" / "supply_risk_summary.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built():
    summary, po, conc, ctry = supply_risk.build()
    return summary, po, conc, ctry


@pytest.fixture(scope="module")
def raw():
    return supply_risk.load()


# --- the reconciliation that matters ---------------------------------------

def test_otif_here_equals_the_otif_the_pipeline_publishes(built, raw):
    """This module and the medallion pipeline must not disagree about service.

    Both compute OTIF as on-time AND fill >= 95%. If either side ever redefines
    it, the dashboard and the sourcing analysis start telling a boardroom two
    different numbers, which is the exact failure this project is about.
    """
    summary = built[0]
    orders = raw["orders"]
    fill = np.where(orders.qty_ordered > 0, orders.qty_shipped / orders.qty_ordered, 1.0)
    pipeline_otif = ((orders.shipped_date <= orders.promised_date)
                     & (fill >= 0.95)).mean()
    assert summary["otif_rate"] == pytest.approx(pipeline_otif, abs=1e-9)


def test_the_failure_split_accounts_for_every_missed_order(built):
    """Late-only + short-only + both must equal every order that missed OTIF."""
    summary = built[0]
    missed = summary["orders"] - summary["otif_orders"]
    split = summary["late_only"] + summary["short_only"] + summary["late_and_short"]
    assert split == missed


def test_on_time_and_in_full_bound_otif_from_above(built):
    """OTIF is an AND, so it can never exceed either component."""
    s = built[0]
    assert s["otif_rate"] <= s["on_time_rate"]
    assert s["otif_rate"] <= s["in_full_rate"]
    # and it cannot be lower than the inclusion-exclusion floor either
    assert s["otif_rate"] >= s["on_time_rate"] + s["in_full_rate"] - 1


# --- concentration ----------------------------------------------------------

def test_award_shares_sum_to_one_for_every_sku(raw):
    """A share that stops summing makes every downstream exposure wrong."""
    s = raw["sourcing"].groupby("product_id").allocation_share.sum()
    assert s.round(4).between(0.9999, 1.0001).all(), \
        f"SKUs whose awards do not sum to 1: {s[~s.round(4).between(0.9999, 1.0001)].to_dict()}"


def test_a_single_source_sku_has_a_maximal_hhi(built):
    """HHI is on the 0-10,000 scale, so one source is exactly 10,000."""
    conc = built[2]
    single = conc[conc.is_single_source == 1]
    assert len(single) > 0, "no single-source SKU to check"
    assert (single.award_hhi == 10000).all()
    assert (conc.award_hhi <= 10000).all()
    # and a multi-source SKU must be strictly below it
    assert (conc[conc.is_single_source == 0].award_hhi < 10000).all()


def test_concentration_covers_every_sku_that_has_an_award(raw, built):
    assert set(built[2].product_id) == set(raw["sourcing"].product_id)


# --- country exposure and the disruption scenario ---------------------------

def test_country_exposure_shares_sum_to_one(built):
    assert built[3].exposed_share.sum() == pytest.approx(1.0, abs=1e-3)


def test_exposed_cogs_reconciles_to_total_cogs(built, raw):
    """Every dollar of COGS is awarded to exactly one origin, so the split of
    exposure across countries must add back up to the whole."""
    total = float((raw["orders"].qty_shipped * raw["orders"].unit_cost).sum())
    assert built[3].exposed_cogs.sum() == pytest.approx(total, rel=1e-6)


def test_a_stranded_sku_really_has_no_qualified_alternate(built, raw):
    """Re-derives the stranded count independently of the module under test."""
    ctry = built[3]
    s = raw["sourcing"].merge(raw["suppliers"], on="supplier_id")
    for _, row in ctry.iterrows():
        skus = set(s[s.country == row.country].product_id)
        alt = s[(s.product_id.isin(skus))
                & (s.country != row.country)
                & (s.is_qualified_alternate == 1)]
        assert row.stranded_skus == len(skus - set(alt.product_id)), row.country


def test_no_country_supplies_more_skus_than_exist(built, raw):
    assert (built[3].skus_supplied <= raw["products"].product_id.nunique()).all()


# --- published figures ------------------------------------------------------

def test_the_published_summary_matches_the_committed_json(built):
    """The report and the README quote this file; it must be what the code
    actually produced on this seed."""
    assert _COMMITTED_SUMMARY == built[0]


def test_headline_figures_are_what_the_readme_claims(built):
    """Hand-checked against the seeded data. If the generator changes, these
    fail loudly rather than the README quietly becoming fiction."""
    s = built[0]
    assert s["orders"] == 20000
    assert s["skus"] == 60
    assert s["countries"] == 9
    assert s["single_source_skus"] == 3
    assert s["otif_rate"] == pytest.approx(0.8042, abs=5e-4)
    assert s["on_time_rate"] == pytest.approx(0.8993, abs=5e-4)
    assert s["in_full_rate"] == pytest.approx(0.8932, abs=5e-4)
    assert s["largest_origin"] == "Mexico"
    assert s["largest_origin_share"] == pytest.approx(0.272, abs=2e-3)
    assert s["largest_origin_stranded_skus"] == 13
    assert s["country_hhi"] == pytest.approx(1620, abs=5)


def test_short_shipping_is_the_bigger_service_failure(built):
    """The finding the report leads with. Stated as a test so that if the data
    ever makes lateness the bigger driver, the claim fails instead of ageing
    into a lie."""
    s = built[0]
    assert s["short_only"] > s["late_only"]


def test_offshore_buys_a_lower_price_with_a_longer_lead(built):
    """The trade-off the lead-time section quantifies: if it ever inverts, the
    narrative around it is wrong."""
    s = built[0]
    assert s["offshore_price_index"] < s["nearshore_price_index"]
    assert s["offshore_lead_days"] > s["nearshore_lead_days"]
