"""A slicer must reach every visual on its page.

The failure this catches is silent, which is what makes it worth a test. A
running total has to clear the date filter the axis puts on it, and the
shortest way to write that is `ALL(fact_table)`. That clears the date - and
every slicer on the page with it. The chart keeps rendering, the numbers stay
plausible, and the only symptom is one line that does not move when the user
filters. On the migration command centre the Domain and Source Type slicers
moved four visuals out of five; the cutover burn-up held the whole-estate total.

The rule is DAX's own distinction between removing context and replacing it:

* A release inside a **CALCULATE filter argument** replaces context. That is a
  prior-period measure re-pointing a date dimension at last month, and it is
  correct - the aggregation still sees every other filter the user set.
* A release in the **expression** position removes context outright. Whatever
  is aggregated inside it no longer sees the user's slicers at all.

So: for each page, take the columns its slicers hold, walk every measure the
page's visuals reach (and the measures those call), and fail on a release in
expression position that drops a column a slicer on that page is holding.
Column-scoped releases (`ALL(t[some_other_column])`), `ALLSELECTED`, and an
`ALLEXCEPT` that names what it keeps are all left alone.

Nothing here is specific to one report; pages and model are read off disk, so
this file drops into any PBIP repo unchanged.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PBIP = ROOT / "powerbi" / "pbip"
SM = next(PBIP.glob("*.SemanticModel")) / "definition"
PAGES = next(PBIP.glob("*.Report")) / "definition" / "pages"

# `measure 'Name' =` or `measure Name =`, then the DAX that follows. TMDL
# indents a measure body one level deeper than the `measure` line and closes it
# with two-tab properties (formatString, lineageTag, displayFolder), so the body
# ends at the first of those - or at the next declaration, or at the `///`
# comment introducing the next measure. Stopping only at the next declaration
# swallows all three, which is how this checker first reported a DIVIDE() of two
# measures as releasing a filter: it was reading the sentence documenting the
# fix, two measures further down.
MEASURE_RE = re.compile(
    r"^\tmeasure\s+(?:'([^']+)'|([A-Za-z_][\w ]*?))\s*=\s*"
    r"(.*?)(?=^\t\t[A-Za-z]\w*:|^\t(?:measure|column|partition|annotation)\s|^\t///|\Z)",
    re.M | re.S,
)
RELEASE_RE = re.compile(
    r"\b(ALL|REMOVEFILTERS|ALLEXCEPT)\s*\(\s*('[^']+'|[A-Za-z_]\w*)\s*(\[[^\]]+\])?",
    re.I,
)
CALCULATE_RE = re.compile(r"\bCALCULATE(?:TABLE)?\s*\(", re.I)


def _unquote(name):
    return name[1:-1] if name.startswith("'") else name


def calculate_filter_spans(body):
    """Character ranges of the filter arguments of every CALCULATE in `body`.

    A release sitting in one of these is replacing filter context for the
    aggregation, which is what a prior-period or running-total measure is
    supposed to do. A release outside them is discarding context.
    """
    spans = []
    for m in CALCULATE_RE.finditer(body):
        depth, arg_start, args, i = 1, m.end(), [], m.end()
        while i < len(body) and depth:
            c = body[i]
            if c == '"':  # skip string literals so a comma inside one is safe
                i = body.find('"', i + 1)
                if i < 0:
                    break
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    args.append((arg_start, i))
            elif c == "," and depth == 1:
                args.append((arg_start, i))
                arg_start = i + 1
            i += 1
        spans.extend(args[1:])  # arg 0 is the expression; the rest are filters
    return spans


def measures():
    """measure name -> DAX body, across every table in the model."""
    out = {}
    for f in sorted((SM / "tables").glob("*.tmdl")):
        for quoted, bare, body in MEASURE_RE.findall(f.read_text(encoding="utf-8")):
            out[(quoted or bare).strip()] = body
    return out


MEASURES = measures()


def _referenced_measures(body, seen):
    """Measures called from a DAX body, transitively."""
    for name in re.findall(r"\[([^\]]+)\]", body):
        if name in MEASURES and name not in seen:
            seen.add(name)
            _referenced_measures(MEASURES[name], seen)
    return seen


def released_columns(body, held):
    """Slicer-held columns this body drops from its own aggregation."""
    protected = calculate_filter_spans(body)
    out = []
    for m in RELEASE_RE.finditer(body):
        if any(lo <= m.start() < hi for lo, hi in protected):
            continue
        func, table, column = m.group(1).upper(), _unquote(m.group(2)), m.group(3)
        if column:  # ALL(t[col]) releases exactly that column
            released = {f"{table}.{column[1:-1]}"}
        elif func == "ALLEXCEPT":
            kept = {f"{table}.{c}"
                    for c in re.findall(rf"{re.escape(table)}\[([^\]]+)\]", body)}
            released = {h for h in held if h.startswith(f"{table}.")} - kept
        else:  # ALL(t) / REMOVEFILTERS(t) releases the whole table
            released = {h for h in held if h.startswith(f"{table}.")}
        out += [(func, table, h) for h in sorted(released & held)]
    return out


def pages():
    for page_dir in sorted(p for p in PAGES.iterdir() if p.is_dir()):
        visuals = sorted((page_dir / "visuals").glob("*/visual.json"))
        if visuals:
            yield page_dir.name, visuals


def page_facts(visuals):
    """(columns the page's slicers hold, measures the page's visuals use)."""
    held, used = set(), set()
    for v in visuals:
        raw = v.read_text(encoding="utf-8")
        refs = set(re.findall(r'"queryRef"\s*:\s*"([^"]+)"', raw))
        if json.loads(raw).get("visual", {}).get("visualType") == "slicer":
            held |= {r for r in refs if "." in r}
        used |= {r.split(".", 1)[1] for r in refs if r.split(".", 1)[1] in MEASURES}
    return held, used


CASES = [pytest.param(name, vs, id=name) for name, vs in pages()]


def test_the_measure_bodies_parsed_out_of_the_model_are_dax():
    """The regex above is load-bearing: over-capture and this file reports the
    next measure's text as a defect, under-capture and it reports nothing.
    Both failure modes are quiet, so pin the shape."""
    assert len(MEASURES) > 5, "no measures parsed out of the model"
    stray = {n for n, b in MEASURES.items() if "lineageTag" in b or "formatString" in b}
    assert not stray, f"measure bodies ran past their DAX into properties: {sorted(stray)}"


def test_the_report_has_pages_with_slicers():
    """Guard against the traversal silently finding nothing."""
    assert any(page_facts(vs)[0] for _, vs in pages()), "no slicers found to check"


def test_the_check_can_tell_replacing_context_from_discarding_it():
    """The whole checker turns on that distinction, and both sides of it are
    real code this portfolio has shipped. Left: the migration burn-up before it
    was fixed - the count of artifacts itself was taken with the table's filters
    gone. Right: a prior-month measure re-pointing a date dimension inside a
    CALCULATE, which is correct and must not be reported."""
    held = {"t.domain", "d.month"}
    discards = "COUNTROWS(FILTER(ALL(t), NOT ISBLANK(t[cut]) && t[cut] <= vEnd))"
    replaces = "CALCULATE([Revenue], FILTER(ALL(d), d[month_start] = vPrev))"
    assert released_columns(discards, held) == [("ALL", "t", "t.domain")]
    assert released_columns(replaces, held) == []


@pytest.mark.parametrize("page,visuals", CASES)
def test_no_measure_on_a_page_discards_a_filter_its_slicer_set(page, visuals):
    held, used = page_facts(visuals)
    if not held:
        pytest.skip(f"{page} has no slicers")

    reachable = set(used)
    for m in used:
        _referenced_measures(MEASURES[m], reachable)

    problems = [
        f"[{name}] {func}({table}) discards {col}, which a slicer on {page} sets"
        for name in sorted(reachable)
        for func, table, col in released_columns(MEASURES[name], held)
    ]
    assert not problems, (
        f"{page}: {len(problems)} measure(s) ignore a slicer on their own page:\n  "
        + "\n  ".join(problems)
    )
