"""
Every number the README states in prose must still be the number the engines
produce, formatted the way a reader sees it.

The suite already pins the published FIGURES: change the analysis and
`test_inventory_health.py` and `test_supply_risk.py` fail. What nothing pinned
was the other direction - the prose. A figure can move, the JSON assertion can
be updated, and the README can go on quoting last month's run with a green
build the whole way. That is not hypothetical; it is what happened in the
healthcare repository, and the test that caught it there is the model for this
one.

So each assertion below reads the value from the engine output and then looks
for it in the README **as formatted**, thousands separators, bold markers, sign
and all. When a figure legitimately changes, this fails and names the document
that needs editing.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
OUT = ROOT / "analytics" / "output"

# The README writes negatives with a typographic minus, as prose should.
MINUS = "−"


@pytest.fixture(scope="module")
def prose():
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inventory():
    return json.loads((OUT / "inventory_health_summary.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sourcing():
    return json.loads((OUT / "supply_risk_summary.json").read_text(encoding="utf-8"))


def quoted(prose, needle):
    """Match across line breaks: prose wraps, and a figure that happens to land
    either side of a wrap is still quoted."""
    flat = " ".join(prose.split())
    return " ".join(needle.split()) in flat


def test_the_readme_is_long_enough_to_be_the_real_one(prose):
    """Guards against every assertion below passing on an empty or stub file."""
    assert len(prose) > 10_000
    assert "Plenty of stock, and still short" in prose


def test_the_shortfall_figures_are_quoted_as_published(inventory, prose):
    s = inventory
    assert quoted(prose, f"**{s['below_reorder_point']} of {s['positions']}**")
    assert quoted(prose, f"${s['replenishment_gap_value']:,.0f}")
    assert quoted(prose, f"${s['gap_value_a_class']:,.0f}")
    assert quoted(prose, f"**{s['positions_not_covering_lead']} positions**")
    assert quoted(prose, f"${s['excess_value']:,.0f}")


def test_the_transfer_opportunity_is_quoted_as_published(inventory, prose):
    s = inventory
    assert quoted(prose, f"**{s['transfer_skus']} SKUs**")
    assert quoted(prose, f"${s['transfer_value']:,.0f}")
    # The share is rounded to whole percent in the prose, as a share should be.
    assert quoted(prose, f"**{s['transfer_share_of_gap']:.0%}**")


def test_the_drift_figures_are_quoted_with_their_signs(inventory, prose):
    """The finding the page leads with is a PAIR of movements in opposite
    directions. Quoting one without the other, or dropping a sign, inverts it."""
    s = inventory
    assert quoted(prose, f"**+{s['gap_value_change']:.1%}**")
    assert quoted(prose, f"**{MINUS}{abs(s['on_hand_value_change']):.1%}**")
    assert quoted(prose, f"**+{s['offshore_gap_change']:.1%}**")
    assert quoted(prose, f"**{MINUS}{abs(s['nearshore_on_hand_change']):.1%}**"
                  ) or quoted(prose, f"**+{s['nearshore_on_hand_change']:.1%}**")
    assert quoted(prose, f"**{MINUS}{abs(s['nearshore_gap_change']):.1%}**")


def test_the_sourcing_figures_are_quoted_as_published(sourcing, prose):
    s = sourcing
    assert quoted(prose, f"**{s['single_source_skus']} of {s['skus']}**")
    assert quoted(prose, f"**{s['single_source_cogs_share']:.1%} of COGS**")
    assert quoted(prose, f"**{s['country_hhi']:,.0f}**")
    assert quoted(prose, f"**{s['largest_origin']}, {s['largest_origin_share']:.1%}")
    assert quoted(prose, f"**{s['largest_origin_stranded_skus']} SKUs**")
    assert quoted(prose, f"**{s['largest_origin_switch_days']} days**")
    assert quoted(prose, f"{s['offshore_cogs_share']:.1%} of COGS")
    assert quoted(prose, f"**{s['offshore_lead_days']} days**")
    assert quoted(prose, f"**{s['nearshore_lead_days']} days**")


def test_the_otif_decomposition_is_quoted_as_published(sourcing, prose):
    """The claim that short-shipping beats lateness is the repo's sharpest
    service finding; it is quoted with three counts that must add up."""
    s = sourcing
    assert quoted(prose, f"**{s['on_time_rate']:.1%}**")
    assert quoted(prose, f"**{s['in_full_rate']:.1%}**")
    assert quoted(prose, f"{s['late_only']:,} orders were late only")
    assert quoted(prose, f"**{s['short_only']:,} were short only**")
    assert quoted(prose, f"{s['late_and_short']} were both")
    assert quoted(prose, f"{s['otif_rate']:.1%} OTIF")
    assert s["short_only"] > s["late_only"], "the README's claim has inverted"


def test_the_reader_with_a_calculator_is_not_the_first_line_of_defence(inventory):
    """The two figures the README prints next to each other must divide into
    the third. A published share that does not follow from its own numerator
    and denominator is the cheapest error to catch and the worst to ship."""
    s = inventory
    assert s["transfer_value"] / s["replenishment_gap_value"] == pytest.approx(
        s["transfer_share_of_gap"], abs=0.0005)
    assert s["below_reorder_point"] / s["positions"] == pytest.approx(
        s["below_reorder_share"], abs=0.0005)


def test_the_page_and_test_counts_on_the_badge_are_real(prose):
    badge = re.search(r"tests-(\d+)%20passing", prose)
    assert badge, "README no longer carries a test-count badge"
    # A lower bound is not a guard. This asserted only that the badge was at
    # least the number of test *definitions*, so a badge could drift arbitrarily
    # high — or stay behind while parametrised cases were added — and still
    # pass. healthcare-claims-analytics had the strict version of this same
    # confusion, pinned to definitions rather than cases, and published 182 for
    # months while its suite collected 345. The badge means collected cases, so
    # that is what gets counted, in a subprocess so the answer does not depend
    # on how this run was invoked.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT,
    )
    # A module that fails to import is reported as an error and its tests are
    # simply absent from the count, so an environment problem would otherwise
    # surface as "the badge is wrong" -- a misleading failure that sends someone
    # to edit a correct README. Say what actually happened instead.
    errors = re.search(r"(\d+) errors?\b", completed.stdout)
    assert not errors, (
        "collection did not complete -- " + errors.group(0) + " during collection, "
        "so the count below would be short. Fix the import error, not the badge: "
        + completed.stdout[-2000:])
    found = re.search(r"(\d+) tests? collected", completed.stdout)
    assert found, "could not read a collected-test count: " + completed.stdout[-2000:]
    assert int(badge.group(1)) == int(found.group(1)), (
        f"badge claims {badge.group(1)} tests, the suite collects {found.group(1)}")

    pages = len(list((ROOT / "powerbi" / "pbip").glob(
        "*.Report/definition/pages/*/page.json")))
    # Written as a word in the prose and as a digit elsewhere; accept either,
    # and fail when the report grows a page and the sentence does not.
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
             7: "Seven", 8: "Eight", 9: "Nine"}
    assert (quoted(prose, f"{pages}-page") or quoted(prose, f"{pages} pages")
            or quoted(prose, f"{words.get(pages, pages)} report pages")), \
        f"the report has {pages} pages; the README does not say so"
