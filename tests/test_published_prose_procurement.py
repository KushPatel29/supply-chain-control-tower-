"""
The same gate as `test_published_prose.py`, over the two new sections.

A figure can move, its JSON assertion can be updated, and the README can go on
quoting last month's run with a green build the whole way. So every number the
procurement and service-economics prose states is read back out of the engine
output and looked for in the document **as formatted** — thousands separators,
signs and all.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
OUT = ROOT / "analytics" / "output"


@pytest.fixture(scope="module")
def prose():
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scorecard():
    return json.loads((OUT / "supplier_scorecard_summary.json").read_text(
        encoding="utf-8"))


@pytest.fixture(scope="module")
def service():
    return json.loads((OUT / "service_economics_summary.json").read_text(
        encoding="utf-8"))


def quoted(prose, needle):
    """Match across line breaks: prose wraps, and a figure that lands either
    side of a wrap is still quoted."""
    return " ".join(needle.split()) in " ".join(prose.split())


def test_both_sections_are_actually_in_the_document(prose):
    """Guards against every assertion below passing on a file that lost them."""
    assert "## The supplier panel, measured instead of asked" in prose
    assert "## Nobody has ever priced the service level" in prose


def test_the_purchase_order_ledger_is_quoted_as_published(scorecard, prose):
    s = scorecard
    assert quoted(prose, f"{s['po_lines']:,} PO lines")
    assert quoted(prose, f"{s['suppliers']} suppliers and {s['skus_purchased']} SKUs")
    assert quoted(prose, f"${s['total_spend']:,.0f} of inbound spend")


def test_the_four_scored_dimensions_are_quoted_as_published(scorecard, prose):
    s = scorecard
    assert quoted(prose, f"OTIF {s['otif_rate']:.1%} (on time {s['on_time_rate']:.1%}, "
                         f"in full {s['in_full_rate']:.1%})")
    assert quoted(prose, f"{s['reject_rate']:.2%} of units rejected")
    assert quoted(prose, f"${s['cost_of_poor_quality']:,.0f} of stock condemned")
    assert quoted(prose, f"${abs(s['ppv_dollars']):,.0f} **under** contract")
    assert quoted(prose, f"{s['overbilled_suppliers']} suppliers invoice above theirs, "
                         f"together ${s['overbilled_dollars']:,.0f}")


def test_the_anchored_scale_claim_is_quoted_as_published(scorecard, prose):
    """The README says nobody reaches the target and names the top composite.
    If a supplier ever does reach it, that sentence becomes false."""
    s = scorecard
    assert quoted(prose, f"the best composite is {s['best_score']:.1f}")
    assert s["best_score"] < 100


def test_the_tier_mismatch_is_quoted_as_published(scorecard, prose):
    s = scorecard
    assert quoted(prose, f"**{s['tier_mismatches']} of {s['suppliers']} suppliers "
                         "sit in a different band")
    assert quoted(prose, f"${s['spend_under_mismatched_tier']:,.0f} of spend")
    assert quoted(prose, f"{s['tier_inversions']} are inverted outright")
    for name in s["inverted_suppliers"]:
        assert quoted(prose, name)
    assert quoted(prose, f"{s['worst_supplier']} scores {s['worst_score']:.1f}")


def test_the_award_shift_is_quoted_as_published(scorecard, prose):
    s = scorecard
    assert quoted(prose, f"{s['award_shift_skus']} SKUs have a qualified alternate")
    assert quoted(prose, f"${s['award_shift_spend']:,.0f} of annual spend")
    assert quoted(prose, f"${s['award_shift_price_impact']:,.0f}")
    assert quoted(prose, f"{s['award_shift_cheaper_skus']} of those SKUs are *cheaper*")
    assert quoted(prose, f"A further {s['award_shift_unpriced_skus']}")
    assert quoted(prose, f"${s['award_shift_unpriced_spend']:,.0f}")


def test_the_reject_pareto_is_quoted_as_published(scorecard, prose):
    s = scorecard
    assert quoted(prose, f"{s['top_reject_share']:.0%} of the money on "
                         f'"{s["top_reject_reason"]}"')


def test_the_service_policy_in_force_is_quoted_as_published(service, prose):
    s = service
    assert quoted(prose, f"{s['positions']} SKU × warehouse positions")
    assert quoted(prose, f"flat {s['current_service_level']:.0%} cycle service level")
    assert quoted(prose, f"${s['safety_stock_value']:,.0f} in safety stock")


def test_the_exchange_curve_prices_are_quoted_as_published(service, prose):
    s = service
    assert quoted(prose, f"the next point costs ${s['cost_of_one_more_point']:,.0f}")
    assert quoted(prose, f"95% to 99% costs ${s['cost_of_99_vs_95']:,.0f}")


def test_the_four_policy_table_is_quoted_as_published(service, prose):
    s = service
    for value in (s["fill_units_now"], s["fill_revenue_now"],
                  s["fill_units_ladder"], s["fill_revenue_ladder"],
                  s["fill_units_optimal"], s["fill_revenue_optimal"]):
        assert quoted(prose, f"{value:.2%}"), value


def test_the_counter_intuitive_ladder_result_is_quoted_as_published(service, prose):
    """The headline finding. If the ladder ever stops losing, this sentence has
    to be rewritten rather than left standing."""
    s = service
    assert s["ladder_units_change_pts"] < 0
    assert quoted(prose, f"**loses** {abs(s['ladder_units_change_pts']):.2f} points "
                         "of unit fill")
    assert quoted(prose, f"only {s['ladder_revenue_change_pts']:+.2f} points")
    assert quoted(prose, f"reaches {s['fill_units_optimal']:.2%}")
    assert quoted(prose, f"({s['optimal_units_change_pts']:+.2f} pts)")


def test_the_two_objectives_agreement_is_quoted_as_published(service, prose):
    s = service
    gap = abs(s["fill_units_now"] - s["fill_revenue_now"]) * 100
    assert quoted(prose, f"agree to {gap:.3f} of a point")


def test_the_working_capital_release_is_quoted_as_published(service, prose):
    s = service
    assert quoted(prose, f"needs ${s['budget_holding_service']:,.0f}")
    assert quoted(prose, f"${s['working_capital_released']:,.0f} of working capital")
    assert quoted(prose, f"({s['working_capital_released_pct']:.1%})")
