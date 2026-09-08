"""Inventory adds across lots and warehouses, never across dates.

fact_inventory holds one row per lot per weekly snapshot. `Total Inventory
Value` was `SUM(fact_inventory[inventory_value])`, which adds 26 separate
photographs of the same warehouse together — $34,564,500 against a real
position of $202,393, overstated 171x, on the cards a planner reads first.

The model already knew: `Avg Inventory Value` carries a comment saying a plain
SUM "would double-count across multiple weekly snapshots instead of averaging
them". The denominator was fixed and the headline measure was not.

DAX cannot be executed here, so this pins the two halves that can be checked:
the data really is a repeating snapshot (which is *why* summing is wrong, with
the numbers), and the measure really does restrict to one date.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
FACT = ROOT / "data" / "lake" / "gold" / "fact_inventory.parquet"
TMDL = ROOT / "powerbi/pbip/SupplyChainControlTower.SemanticModel/definition/tables/_Measures.tmdl"
DAX = ROOT / "powerbi" / "dax_measures.dax"


@pytest.fixture(scope="module")
def fact() -> pd.DataFrame:
    if not FACT.is_file():
        pytest.skip("gold fact_inventory not built")
    return pd.read_parquet(FACT)


def _measure(text: str, name: str) -> str:
    """The body of one measure, up to the next measure or property line."""
    m = re.search(rf"measure '{re.escape(name)}' =(.*?)(?=\n\t*(?:measure |formatString:))",
                  text, re.S)
    assert m, f"{name} is no longer defined in the TMDL"
    return m.group(1)


def test_the_fact_really_is_a_repeating_snapshot(fact):
    """If this ever stops being true, the measure below can go back to a SUM."""
    dates = fact["date_key"].nunique()
    assert dates > 1, "one snapshot only — semi-additivity would be moot"
    lots_per_date = fact.groupby("date_key").size()
    assert lots_per_date.min() > 1, "each snapshot should carry many lots"


def test_summing_every_snapshot_is_wildly_wrong(fact):
    """The size of the error, kept as a number so nobody has to take it on trust."""
    summed = fact["inventory_value"].sum()
    as_of = fact.loc[fact["date_key"] == fact["date_key"].max(), "inventory_value"].sum()
    assert as_of > 0
    ratio = summed / as_of
    assert ratio > 10, (
        f"summing all snapshots gives ${summed:,.0f} against an as-of position of "
        f"${as_of:,.0f} ({ratio:.0f}x) — a plain SUM cannot be right here"
    )


@pytest.mark.parametrize("source", ["tmdl", "dax"])
def test_total_inventory_value_restricts_to_one_snapshot(source):
    body = (_measure(TMDL.read_text(encoding="utf-8"), "Total Inventory Value")
            if source == "tmdl" else
            re.search(r"Total Inventory Value =(.*?)(?=\n// |\nInventory As Of)",
                      DAX.read_text(encoding="utf-8"), re.S).group(1))
    flat = " ".join(body.split())
    assert "SUM(fact_inventory[inventory_value])" in flat, "the measure no longer sums values at all"
    assert "date_key" in flat, (
        "Total Inventory Value does not mention date_key, so it is adding every "
        "weekly snapshot together again"
    )
    assert re.search(r"MAX\(\s*fact_inventory\[date_key\]\s*\)", flat), (
        "the as-of date is no longer taken from the fact's latest snapshot"
    )


def test_the_as_of_date_ignores_the_risk_flag():
    """Critical and Warning must be shares of the SAME snapshot.

    Without this, slicing to Critical would find the last date that happens to
    have a Critical lot, and '% Inventory at Risk' would divide two different
    days by each other.
    """
    body = " ".join(_measure(TMDL.read_text(encoding="utf-8"), "Total Inventory Value").split())
    assert "REMOVEFILTERS(fact_inventory[expiry_risk_flag])" in body, (
        "the as-of date responds to the expiry-risk flag, so the risk split and "
        "its denominator can land on different snapshots"
    )
