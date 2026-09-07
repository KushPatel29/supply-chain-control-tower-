"""
No published number may carry more precision than it was measured to.

CI regenerates every figure and asserts the files come back byte for byte
identical. A float written at full repr precision breaks that on a different
machine and nowhere else: `0.9500150944608787` came out of `math.erf`, which is
the platform's libm, so its sixteenth digit is a property of the runner rather
than of the data. The gate failed on Linux and passed on Windows, and the
number was correct to every digit anyone would read.

Full precision is also a claim. A safety-stock figure printed to fifteen
decimals says the analysis resolved it to fifteen decimals; it did not. So this
file asserts the same rule on both grounds: everything published is rounded to
a precision the analysis can defend.

Scoped to the files listed in PUBLISHED. The older outputs in these
repositories carry the other kind of long float - `1335.3700000000001` from
adding two-decimal money in binary - which is ugly but deterministic: IEEE 754
sums the same values in the same order to the same bits on every platform, so
they are not a reproducibility hazard. Rounding them would change committed
figures for cosmetic reasons, which is a separate decision from this one.
"""
import csv
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The most decimals any published figure needs. Rates are quoted to five
# (0.00001 = a thousandth of a percentage point), money to two, and nothing
# here resolves anything finer.
MAX_DECIMALS = 6

FLOATY = re.compile(r"^-?\d+\.(\d+)$")


# The files this round publishes. Every one of them passes through a solver, a
# quantile or a normal-distribution function - the places a platform's libm can
# put a different digit in the sixteenth place.
PUBLISHED = [
    # supply-chain-control-tower
    "supplier_scorecard.csv", "award_shift_candidates.csv", "reject_reasons.csv",
    "supplier_scorecard_summary.json", "supplier_scorecard_headline.csv",
    "service_exchange_curve.csv", "service_policy_options.csv",
    "service_policy_by_class.csv", "service_positions.csv",
    "service_economics_summary.json", "service_economics_headline.csv",
    # Customer-Recommendation-Engine
    "pocket_waterfall.csv", "pocket_by_customer.csv", "leakage_by_component.csv",
    "freight_by_order_band.csv", "pocket_margin_summary.json",
    "pocket_margin_headline.csv", "revenue_bridge.csv",
    "revenue_bridge_by_protein.csv", "customer_concentration.csv",
    "revenue_bridge_summary.json", "revenue_bridge_headline.csv",
    # gl-reconciliation-dashboard
    "journal_risk_entries.csv", "journal_risk_flags.csv",
    "journal_risk_by_user.csv", "benford_by_dimension.csv",
    "journal_risk_summary.json", "journal_risk_headline.csv",
    "exception_ageing.csv", "exception_sla_by_owner.csv",
    "exception_sla_by_type.csv", "exception_throughput.csv",
    "exception_clear_methods.csv", "exception_ageing_summary.json",
    "exception_ageing_headline.csv",
]


def published_files():
    for folder in ("output", "analytics/output"):
        d = ROOT / folder
        if not d.is_dir():
            continue
        for name in PUBLISHED:
            p = d / name
            if p.exists():
                yield p


FILES = sorted(set(published_files()))


def over_precise(value):
    m = FLOATY.match(str(value).strip())
    return bool(m) and len(m.group(1)) > MAX_DECIMALS


def test_there_are_published_files_to_check():
    """A list that matched nothing would make every case below vacuous."""
    assert len(FILES) >= 5, f"only found {len(FILES)} of the published files"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_published_number_carries_more_precision_than_it_has(path):
    offenders = []
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as fh:
            for row_no, row in enumerate(csv.DictReader(fh), start=2):
                for col, value in row.items():
                    if value and over_precise(value):
                        offenders.append(f"line {row_no} {col}={value}")
                if len(offenders) > 5:
                    break
    else:
        def walk(node, trail):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{trail}.{k}" if trail else k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{trail}[{i}]")
            elif isinstance(node, float) and over_precise(repr(node)):
                offenders.append(f"{trail}={node!r}")
        walk(json.loads(path.read_text(encoding="utf-8")), "")

    assert not offenders, (
        f"{path.name} publishes numbers to more than {MAX_DECIMALS} decimals, "
        f"which no analysis here resolves and which a different platform's "
        f"libm will not reproduce: {offenders[:5]}")
