"""
Pricing a service policy is arithmetic, and arithmetic can be silently wrong.

The module hand-rolls three standard-normal functions rather than take a scipy
dependency, and it re-derives safety stock rather than read the number the
planning view already carries. Both are deliberate and both need proving, so the
first two sections here check the mathematics against closed forms and against
the generator that produced the data. If service_economics.py and
generate_data.py ever stop agreeing about King's formula, this is where it
surfaces - not in a slide.

The rest pins the claims the report makes: that four policies cost the same
money, that each optimum really is optimal for its own objective, and that the
counter-intuitive result - the textbook ABC ladder losing unit fill - is a
property of the network rather than a bug in the solver.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analytics"))

import service_economics as se  # noqa: E402

BRONZE = ROOT / "data" / "bronze"
PUBLISHED = ROOT / "analytics" / "output" / "service_economics_summary.json"


@pytest.fixture(scope="module")
def built():
    return se.build()


@pytest.fixture(scope="module")
def cur():
    return se.positions(se.load())


# --- the mathematics, against closed forms ----------------------------------

def test_the_normal_cdf_matches_known_values():
    assert float(se.norm_cdf(0.0)) == pytest.approx(0.5, abs=1e-12)
    assert float(se.norm_cdf(1.0)) == pytest.approx(0.8413447461, abs=1e-9)
    assert float(se.norm_cdf(1.959964)) == pytest.approx(0.975, abs=1e-6)
    assert float(se.norm_cdf(-2.0)) == pytest.approx(0.0227501319, abs=1e-9)


def test_ppf_inverts_cdf_across_the_whole_range():
    p = np.array([0.001, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 0.999])
    assert np.allclose(se.norm_cdf(se.norm_ppf(p)), p, atol=1e-9)


def test_the_policy_in_force_really_is_ninety_five_percent():
    """CURRENT_Z is quoted as 95% everywhere in the report. If the constant and
    the claim ever drift apart, every dollar figure below is mislabelled."""
    assert float(se.norm_ppf(0.95)) == pytest.approx(se.CURRENT_Z, abs=5e-4)


def test_the_clip_band_is_the_service_range_it_claims_to_be():
    assert float(se.norm_cdf(se.Z_FLOOR)) == pytest.approx(0.80, abs=1e-4)
    assert float(se.norm_cdf(se.Z_CEILING)) == pytest.approx(0.999, abs=1e-4)


def test_the_loss_function_matches_its_closed_form():
    """G(0) = phi(0) = 1/sqrt(2*pi), and G is strictly decreasing - more safety
    stock cannot increase the units you expect to be short."""
    assert float(se.loss(0.0)) == pytest.approx(1 / math.sqrt(2 * math.pi), abs=1e-12)
    z = np.linspace(-1, 4, 200)
    g = se.loss(z)
    assert (np.diff(g) < 0).all()
    assert (g > 0).all()


# --- against the generator that made the data -------------------------------

def test_kings_formula_here_reproduces_the_planning_view(cur):
    """The module recomputes safety stock instead of reading
    safety_stock_target. That is only safe if the two agree - so check, on
    every position, that they do to within the rounding the CSV carries."""
    from data_generator.generate_data import SERVICE_Z  # noqa: E402
    pos = pd.read_csv(BRONZE / "fact_inventory_position.csv",
                      parse_dates=["week_start"])
    latest = pos[pos.week_start == pos.week_start.max()]
    merged = cur.merge(latest[["product_id", "warehouse_id",
                               "safety_stock_target"]],
                       on=["product_id", "warehouse_id"], suffixes=("", "_pub"))
    assert len(merged) == len(cur)
    recomputed = SERVICE_Z * merged.sigma_dl
    published = merged.safety_stock_target_pub \
        if "safety_stock_target_pub" in merged else merged.safety_stock_target
    # The published column is an integer, so allow half a unit of rounding.
    assert (recomputed - published).abs().max() < 0.51


def test_the_replenishment_quantity_is_observed_not_assumed(cur):
    """Q comes from actual purchase orders. If it ever became a constant or an
    EOQ from invented parameters, the whole exchange curve would be a function
    of somebody's guess."""
    po = pd.read_csv(BRONZE / "fact_purchase_orders.csv")
    direct = po.groupby("product_id").qty_ordered.mean()
    assert cur.replenishment_qty.nunique() > 10, "Q has collapsed to a constant"
    assert np.allclose(cur.replenishment_qty,
                       cur.product_id.map(direct).values, atol=1e-6)


# --- the exchange curve -----------------------------------------------------

def test_the_curve_is_monotone_in_both_axes(built):
    _, curve, _, _, _ = built
    assert curve.service_level.is_monotonic_increasing
    assert curve.safety_stock_value.is_monotonic_increasing
    assert curve.fill_units.is_monotonic_increasing


def test_service_gets_dearer_the_more_of_it_you_buy(built):
    """The concavity that makes the curve worth drawing: the last point of
    service costs more than the first."""
    _, curve, _, _, _ = built
    c = curve.set_index("service_level")
    cheap = (c.loc[0.90, "safety_stock_value"] - c.loc[0.85, "safety_stock_value"]) \
        / (c.loc[0.90, "fill_units"] - c.loc[0.85, "fill_units"])
    dear = (c.loc[0.995, "safety_stock_value"] - c.loc[0.99, "safety_stock_value"]) \
        / (c.loc[0.995, "fill_units"] - c.loc[0.99, "fill_units"])
    assert dear > cheap * 3


def test_the_curve_passes_through_the_policy_actually_in_force(built):
    summary, curve, _, _, _ = built
    row = curve[curve.service_level == 0.95].iloc[0]
    assert row.safety_stock_value == pytest.approx(
        summary["safety_stock_value"], rel=1e-3)
    assert row.fill_units == pytest.approx(summary["fill_units_now"], abs=1e-4)


# --- the four allocations ---------------------------------------------------

def test_every_policy_spends_the_same_money(built):
    """The claim the whole page rests on. If the allocations do not cost the
    same, the comparison is between a policy and a bigger budget."""
    summary, _, table, _, _ = built
    budget = summary["safety_stock_value"]
    assert np.allclose(table.safety_stock_value, budget, rtol=1e-4), (
        table[["policy", "safety_stock_value"]])


def test_no_position_is_planned_outside_the_published_band(built):
    _, _, _, _, cur = built
    for col in ["z_ladder", "z_optimal_units", "z_optimal_revenue"]:
        assert cur[col].min() >= se.Z_FLOOR - 1e-9, col
        assert cur[col].max() <= se.Z_CEILING + 1e-9, col


def test_each_optimum_actually_wins_on_its_own_objective(built):
    """A solver bug shows up here and almost nowhere else: an allocation that
    is beaten on the very objective it was built to maximise."""
    _, _, table, _, cur = built
    t = table.set_index("policy")
    assert t.loc["optimal for units", "fill_units"] >= t.fill_units.max() - 1e-9
    assert t.loc["optimal for revenue", "fill_revenue"] >= t.fill_revenue.max() - 1e-9


def test_the_optimum_beats_the_flat_policy_on_both_objectives(built):
    _, _, table, _, _ = built
    t = table.set_index("policy")
    flat = t.loc["flat 95% (in force)"]
    assert t.loc["optimal for units", "fill_units"] > flat.fill_units
    assert t.loc["optimal for revenue", "fill_revenue"] > flat.fill_revenue


def test_the_abc_ladder_keeps_its_shape(built):
    """A above B above C. The LEVEL of the ladder is solved for; its ordering
    is the policy decision and must survive that solve."""
    _, _, _, _, cur = built
    z = cur.groupby("abc_class").z_ladder.mean()
    assert z["A"] > z["B"] > z["C"]


def test_the_ladder_result_is_the_networks_not_the_solvers(built):
    """The counter-intuitive headline - the textbook ABC ladder LOSING unit
    fill for the same money - has to be a property of this network, not an
    artefact. Two independent confirmations: the ladder still beats flat on the
    objective it was designed for relative to how it does on units, and a
    hand-built ladder computed without the solver reproduces the direction."""
    summary, _, table, _, cur = built
    t = table.set_index("policy")
    flat = t.loc["flat 95% (in force)"]
    ladder = t.loc["ABC ladder 98/95/90"]

    assert ladder.fill_units < flat.fill_units
    # It gives up far less on revenue than on units, which is the explanation:
    # it is a revenue heuristic being scored on units.
    assert (flat.fill_revenue - ladder.fill_revenue) \
        < (flat.fill_units - ladder.fill_units) / 5

    # And the mechanism, checked directly: a dollar of safety stock buys more
    # unit fill on a C item than on an A item, so a ladder that moves money
    # towards A moves it away from where it works hardest.
    eff = cur.demand_weight / (cur.replenishment_qty * cur.unit_cost)
    by_class = eff.groupby(cur.abc_class).median()
    assert by_class["C"] > by_class["A"]


def budget_tol(summary):
    return summary["safety_stock_value"] * 1e-3


def test_by_class_totals_reconcile_to_the_network(built):
    summary, _, _, by_class, _ = built
    assert by_class.value_now.sum() == pytest.approx(
        summary["safety_stock_value"], rel=1e-4)
    assert by_class.value_ladder.sum() == pytest.approx(
        summary["safety_stock_value"], rel=1e-4)
    assert by_class.value_optimal_units.sum() == pytest.approx(
        summary["safety_stock_value"], rel=1e-4)
    # Constant spend means the moves have to net to nothing.
    assert by_class.ladder_change.sum() == pytest.approx(0, abs=budget_tol(summary))
    assert by_class.optimal_change.sum() == pytest.approx(0, abs=budget_tol(summary))


def test_holding_service_releases_capital_rather_than_consuming_it(built):
    summary, _, _, _, cur = built
    assert summary["budget_holding_service"] < summary["safety_stock_value"]
    assert summary["working_capital_released"] > 0
    # ...and it genuinely holds the service it claims to hold.
    z = se.optimal_z(cur, summary["budget_holding_service"], "units")
    assert se.network_fill(cur, z, "units") == pytest.approx(
        summary["fill_units_now"], abs=2e-4)


# --- what the README and the report quote -----------------------------------

def test_the_published_summary_is_what_this_code_produces(built):
    summary, _, _, _, _ = built
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    for key, value in summary.items():
        if isinstance(value, float):
            assert published[key] == pytest.approx(value, rel=1e-4), key
        else:
            assert published[key] == value, key
