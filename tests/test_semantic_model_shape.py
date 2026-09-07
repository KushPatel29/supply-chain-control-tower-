"""
The semantic model has to be shaped the way TMDL is parsed, not just spelled
right.

`test_semantic_model.py` checks what the model BINDS to: every table reads a
file that exists, every bound column is typed, every measure reference
resolves. All of that passed on a model Power BI Desktop could not open at all.

The defect was a multi-line measure written with the first line of its DAX on
the header line and the rest at the property indent:

    measure 'Score Highlight' = VAR vTop = CALCULATE(...)
            VAR vHere = [Composite Score]
            RETURN IF(...)
            lineageTag: 24fba888-...

TMDL has exactly one shape for a multi-line expression - nothing after the `=`,
then the body indented deeper than the properties. Written the other way the
parser reads the continuation lines, and then `lineageTag:` itself, as part of
the expression; the model fails to load and Desktop opens an *untitled window*
with no error dialog to read. A capture run reported "Desktop is showing
'Untitled - Power BI Desktop'" twice before anybody looked at the file.

So this file checks structure. It is deliberately dumb: it does not parse DAX,
it asserts the two shapes a measure is allowed to have.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TABLES = next(ROOT.glob("powerbi/pbip/*.SemanticModel/definition/tables"))

# The properties Desktop writes under a measure. Anything else at this indent
# directly under a single-line measure is a continuation that should not be
# there.
PROPERTY = re.compile(
    r"^\t\t(lineageTag|formatString|displayFolder|description|isHidden"
    r"|formatStringDefinition|annotation|changedProperty|dataType"
    r"|isDataTypeInferred|detailRowsDefinition|kpi)\b")
HEADER = re.compile(r"^\tmeasure ('[^']+'|\S+)\s*=\s*(.*)$")

FILES = sorted(TABLES.glob("*.tmdl"))


def measures(text):
    """(name, first_line, following_lines) for every measure in a file."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = HEADER.match(line)
        if not m:
            continue
        following = []
        for nxt in lines[i + 1:]:
            if nxt.strip() == "" or not nxt.startswith("\t\t"):
                break
            following.append(nxt)
        out.append((m.group(1), m.group(2), following))
    return out


def test_there_are_measures_to_check():
    """A glob that matches nothing would make every test below pass."""
    found = sum(len(measures(f.read_text(encoding="utf-8"))) for f in FILES)
    assert found > 20, f"only {found} measures found - is the model there?"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_single_line_measure_is_followed_only_by_its_properties(path):
    """The exact defect: DAX continuing at the property indent. The parser
    swallows the properties into the expression and the whole model stops
    loading, with no error anywhere a human would look."""
    text = path.read_text(encoding="utf-8")
    broken = []
    for name, first, following in measures(text):
        if not first.strip():
            continue                      # a properly-formed multi-line measure
        for line in following:
            if not PROPERTY.match(line):
                broken.append(f"{name}: {line.strip()[:60]}")
                break
    assert not broken, (
        "measures whose DAX continues at the property indent - TMDL will read "
        f"`lineageTag:` as part of the expression: {broken}")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_multi_line_measure_indents_its_body_past_its_properties(path):
    """The other half of the same rule. A body at the property indent is
    ambiguous to the parser even when nothing follows it."""
    text = path.read_text(encoding="utf-8")
    broken = []
    for name, first, following in measures(text):
        if first.strip():
            continue                      # single-line; the test above covers it
        body = [line for line in following if not PROPERTY.match(line)]
        assert body or True
        for line in body:
            if not line.startswith("\t\t\t"):
                broken.append(f"{name}: {line.strip()[:60]}")
                break
    assert not broken, (
        f"multi-line measures whose body sits at the property indent: {broken}")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_measure_carries_exactly_one_lineage_tag(path):
    """A measure that lost its tag lost it to the expression above it, which is
    the same defect seen from the other end."""
    text = path.read_text(encoding="utf-8")
    missing = []
    for name, _first, following in measures(text):
        tags = [line for line in following if line.startswith("\t\tlineageTag:")]
        if len(tags) != 1:
            missing.append(f"{name}: {len(tags)} lineage tags")
    assert not missing, missing


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_measure_body_contains_a_property_keyword(path):
    """Belt and braces: if a property line ever ends up inside an expression,
    say so in the words a reader will search for."""
    text = path.read_text(encoding="utf-8")
    offenders = []
    for name, first, following in measures(text):
        body = [first] + [line for line in following if not PROPERTY.match(line)]
        for line in body:
            if re.search(r"\b(lineageTag|formatString|displayFolder)\s*:", line):
                offenders.append(f"{name}: {line.strip()[:60]}")
                break
    assert not offenders, offenders
