"""The seed stops making lots a month before it stops shipping orders.

`gen_dim_lot` draws `production_date` from `SNAPSHOT_START + uniform(-30, 150)`,
but the snapshot window runs to `SNAPSHOT_END`, which is `SNAPSHOT_START + 180`.
The 150 is a literal that does not track the window. So for the final month
orders keep consuming stock that nothing replenishes, and the warehouse drains
to a stub: SKU coverage falls from 53 to 16 and the closing position lands at
about 14% of a median week.

This matters because inventory is measured **as of the latest snapshot**. Both
semi-additive measures resolve to that thin final week, so the number a reader
sees is real for its date and unrepresentative of the business -- the kind of
defect that survives every correctness test because nothing here is incorrect.

Not fixed in place, deliberately. CI regenerates `analytics/output/` and
compares it byte for byte, and the eight committed report screenshots show the
current figures. Changing the generator shifts every downstream random draw, so
the honest sequence is: extend the lot window, regenerate, then re-shoot the
screenshots from Power BI Desktop -- and Desktop is not available in this
environment. Until then the gap is measured here rather than left to be
discovered on the report.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "data" / "lake" / "gold"

#: How thin the closing week may be, as a share of the trailing median, before
#: a reader would be misled by the as-of figure.
MIN_CLOSING_COVERAGE = 0.60


def _gold(name: str) -> pd.DataFrame:
    path = GOLD / f"{name}.parquet"
    if not path.is_file():
        pytest.skip(f"gold {name} not built")
    return pd.read_parquet(path)


def test_lots_stop_being_produced_before_orders_stop_shipping():
    """The mechanism, pinned so the diagnosis outlives this docstring."""
    lots = _gold("dim_lot")
    orders = _gold("fact_orders")

    last_lot = pd.to_datetime(lots["production_date"]).max()
    last_order = pd.to_datetime(orders["date_key"], format="%Y%m%d").max()

    assert last_lot < last_order, (
        "lots now run at least as late as orders - if the generator was fixed, "
        "delete this test and the xfail below"
    )
    gap_days = (last_order - last_lot).days
    assert gap_days > 20, (
        f"only {gap_days} days between the last lot and the last order; the "
        "shortfall this file describes may no longer be the cause"
    )


def test_the_drain_is_visible_in_the_snapshot_coverage():
    """The consequence: the closing week carries a fraction of the SKUs."""
    fact = _gold("fact_inventory")
    by_date = fact.groupby("date_key")["product_key"].nunique()
    assert by_date.iloc[-1] < by_date.median() / 2, (
        "the closing snapshot no longer looks truncated - the generator may "
        "have been fixed, in which case this file should go"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known: the lot generator's 150-day window does not reach SNAPSHOT_END, "
        "so the closing snapshot holds ~14% of a median week. Fixing it means "
        "regenerating the byte-for-byte outputs and re-shooting the report "
        "screenshots in Power BI Desktop. When that happens this test starts "
        "passing, XPASSes strictly, and should be un-marked."
    ),
)
def test_the_closing_snapshot_represents_the_business():
    """The invariant the report actually needs, asserted against reality.

    Both `Total Inventory Value` and `Qty On Hand` read the latest snapshot.
    For that to be a fair headline the closing week has to look like a normal
    week. Today it does not, and this records by how much.
    """
    fact = _gold("fact_inventory")
    weekly = fact.groupby("date_key")["inventory_value"].sum()
    closing, typical = weekly.iloc[-1], weekly.median()
    assert closing >= MIN_CLOSING_COVERAGE * typical, (
        f"the closing snapshot holds ${closing:,.0f} against a median week of "
        f"${typical:,.0f} ({closing / typical:.0%}), so every as-of card on the "
        "inventory page under-reports the business"
    )
