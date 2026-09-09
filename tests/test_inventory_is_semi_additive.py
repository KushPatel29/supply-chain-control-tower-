"""Inventory adds across lots and warehouses, never across dates.

fact_inventory holds one row per lot per weekly snapshot, so a plain SUM adds
26 separate photographs of the same warehouse together. Both value columns were
wrong at different times:

    Total Inventory Value   $34,564,500 against a real position of $202,393
    Qty On Hand              1,519,002 units against a real position of 8,735

The first was fixed and the second was missed for a month, because the test
written to prevent this named `Total Inventory Value` instead of asking which
measures aggregate the fact. This file enumerates instead: every measure that
adds a snapshot-sensitive column has to restrict to one date, and if a new
numeric column lands on the fact the tests fail until someone classifies it.

DAX cannot be executed here, so what is pinned is the pair of halves that can
be checked: the data really is a repeating snapshot (which is *why* summing is
wrong, with the numbers), and each measure really does restrict to one date --
in the TMDL the model loads and in the .dax file the README publishes, which
nothing syncs to each other.
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

#: Columns that are a stock level at a point in time. Adding these across
#: snapshots is meaningless; adding them across lots and warehouses is correct.
SNAPSHOT_COLUMNS = ("qty_on_hand", "inventory_value", "days_until_expiry")

#: A measure name in the .dax file starts at column zero and ends at ` =`.
NAME_AT_COLUMN_ZERO = r"^([A-Za-z%][^=\n]*?) ="
COMMENT_LINE = r"^\s*//.*$"
#: Column-zero words that begin a clause of a measure, not a new measure.
DAX_KEYWORDS = ("VAR", "RETURN", "EVALUATE", "DEFINE")


@pytest.fixture(scope="module")
def fact() -> pd.DataFrame:
    if not FACT.is_file():
        pytest.skip("gold fact_inventory not built")
    return pd.read_parquet(FACT)


def _tmdl_measures(text: str) -> dict[str, str]:
    """Every measure in the TMDL, name -> body, flattened to one line."""
    out = {}
    for m in re.finditer(
        r"measure '([^']+)' =(.*?)(?=\n\t*(?:measure |formatString:|lineageTag:))",
        text, re.S,
    ):
        out[m.group(1)] = " ".join(m.group(2).split())
    return out


def _dax_measures(text: str) -> dict[str, str]:
    """Same for the published .dax file, which uses `Name =` at column zero.

    Two other things sit at column zero and contain " =": the VAR and RETURN
    lines of the measure above, and body lines like
    `CALCULATE([Total Orders], fact_orders[otif_flag] = TRUE())`. Both have to
    be skipped or the parser reads a measure's own body as the next measure --
    which is how this file first reported that the two sources disagreed.
    """
    out = {}
    starts = []
    for m in re.finditer(NAME_AT_COLUMN_ZERO, text, re.M):
        name = m.group(1).strip()
        head = name.split()[0] if name.split() else ""
        if head in DAX_KEYWORDS:
            continue
        if any(ch in name for ch in "()[],"):
            continue
        starts.append((name, m.start(), m.end()))
    for i, (name, _, pos) in enumerate(starts):
        stop = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = re.sub(COMMENT_LINE, "", text[pos:stop], flags=re.M)
        out[name] = " ".join(body.split())
    return out


def _adds_the_snapshot(body: str) -> list[str]:
    """Which snapshot columns this measure aggregates additively, if any."""
    if not re.search(r"\bSUMX?\s*\(", body):
        return []
    return [c for c in SNAPSHOT_COLUMNS if f"fact_inventory[{c}]" in body]


def _restricting_measures(source: str) -> list[str]:
    text = (TMDL if source == "tmdl" else DAX).read_text(encoding="utf-8")
    parsed = _tmdl_measures(text) if source == "tmdl" else _dax_measures(text)
    assert parsed, f"no measures parsed out of the {source} file"
    return sorted(n for n, b in parsed.items() if _adds_the_snapshot(b))


def test_the_fact_really_is_a_repeating_snapshot(fact):
    """If this ever stops being true, the measures below can go back to a SUM."""
    dates = fact["date_key"].nunique()
    assert dates > 1, "one snapshot only - semi-additivity would be moot"
    lots_per_date = fact.groupby("date_key").size()
    assert lots_per_date.min() > 1, "each snapshot should carry many lots"


def test_no_unclassified_numeric_column_joined_the_fact(fact):
    """The guard that keeps this file from going stale the way the last one did.

    A new numeric column is either snapshot-sensitive, in which case it belongs
    in SNAPSHOT_COLUMNS, or it is not and belongs in this assertion's message.
    Either way somebody decides, rather than it arriving unexamined.
    """
    numeric = {
        c for c in fact.columns
        if pd.api.types.is_numeric_dtype(fact[c]) and not c.endswith("_key")
    }
    assert numeric == set(SNAPSHOT_COLUMNS), (
        f"fact_inventory's numeric columns are {sorted(numeric)}, but this file "
        f"classifies {sorted(SNAPSHOT_COLUMNS)}. Add the new column to "
        "SNAPSHOT_COLUMNS if summing it across snapshots would be wrong."
    )


@pytest.mark.parametrize("column", ["inventory_value", "qty_on_hand"])
def test_summing_every_snapshot_is_wildly_wrong(fact, column):
    """The size of the error, kept as a number so nobody has to take it on trust."""
    summed = fact[column].sum()
    as_of = fact.loc[fact["date_key"] == fact["date_key"].max(), column].sum()
    assert as_of > 0
    ratio = summed / as_of
    assert ratio > 10, (
        f"summing all snapshots of {column} gives {summed:,.0f} against an as-of "
        f"position of {as_of:,.0f} ({ratio:.0f}x) - a plain SUM cannot be right here"
    )


def test_both_value_measures_are_found_to_aggregate_the_fact():
    """Proves the enumeration below is actually looking at something.

    A regex that silently matched nothing would make every test after this one
    vacuous - which is the failure mode that let Qty On Hand through.
    """
    found = _restricting_measures("tmdl")
    assert "Total Inventory Value" in found and "Qty On Hand" in found, (
        f"expected both value measures to be detected as aggregating the fact, got {found}"
    )


@pytest.mark.parametrize("source", ["tmdl", "dax"])
def test_every_measure_that_adds_the_snapshot_restricts_to_one_date(source):
    for name in _restricting_measures(source):
        text = (TMDL if source == "tmdl" else DAX).read_text(encoding="utf-8")
        body = (_tmdl_measures(text) if source == "tmdl" else _dax_measures(text))[name]
        cols = ", ".join(_adds_the_snapshot(body))
        assert re.search(r"MAX\(\s*fact_inventory\[date_key\]\s*\)", body), (
            f"[{name}] in the {source} adds {cols} without taking an as-of date "
            "from the fact's latest snapshot, so it is adding every weekly "
            "snapshot together"
        )
        assert re.search(r"fact_inventory\[date_key\]\s*=", body), (
            f"[{name}] in the {source} computes an as-of date but never filters "
            "to it"
        )


@pytest.mark.parametrize("source", ["tmdl", "dax"])
def test_the_as_of_date_ignores_the_risk_flag(source):
    """Critical and Warning must be shares of the SAME snapshot.

    Without this, slicing to Critical would find the last date that happens to
    have a Critical lot, and '% Inventory at Risk' would divide two different
    days by each other.
    """
    text = (TMDL if source == "tmdl" else DAX).read_text(encoding="utf-8")
    parsed = _tmdl_measures(text) if source == "tmdl" else _dax_measures(text)
    for name in _restricting_measures(source):
        assert "REMOVEFILTERS(fact_inventory[expiry_risk_flag])" in parsed[name], (
            f"[{name}]'s as-of date responds to the expiry-risk flag, so the risk "
            "split and its denominator can land on different snapshots"
        )


def test_the_two_sources_agree_on_which_measures_are_semi_additive():
    """Nothing syncs dax_measures.dax to the TMDL; this notices when they drift."""
    assert _restricting_measures("tmdl") == _restricting_measures("dax")
