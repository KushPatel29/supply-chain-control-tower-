"""The file that greets someone opening the PBIP has to describe what is in it.

`powerbi/pbip/OPEN_ME_FIRST.md` was written when the model existed and the
report did not. It said "9 tables, 10 relationships, 18 DAX measures", told the
reader they would find "three empty named pages", and sent them to the build
guide to make the visuals themselves. By the time anyone read it there were 26
tables, 135 measures, 8 pages and 66 visuals already built. Every number in it
was wrong, and the one instruction it gave was to build something that was
finished - which is a worse first impression than no file at all.

Prose goes stale quietly. Counts do not have to: they are all readable off the
project. This asserts each number the file states against the thing it states it
about, so the next person to add a page finds out here rather than leaving the
welcome note lying about the work.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PBIP = ROOT / "powerbi" / "pbip"
DOC = PBIP / "OPEN_ME_FIRST.md"
SM = next(PBIP.glob("*.SemanticModel")) / "definition"
REPORT = next(PBIP.glob("*.Report")) / "definition"


@pytest.fixture(scope="module")
def doc():
    assert DOC.exists(), f"{DOC.name} is what a reader opens first; it is missing"
    return DOC.read_text(encoding="utf-8")


def stated(doc, label):
    """The bold number the table states against `label`, e.g. Tables | **26**."""
    m = re.search(rf"\|\s*{label}\s*\|\s*\*\*([\d,]+)\*\*", doc, re.I)
    assert m, f"{DOC.name} no longer states a count for {label}"
    return int(m.group(1).replace(",", ""))


def test_the_table_count_is_real(doc):
    actual = len(list((SM / "tables").glob("*.tmdl")))
    assert stated(doc, "Tables") == actual


def test_the_relationship_count_is_real(doc):
    text = (SM / "relationships.tmdl").read_text(encoding="utf-8")
    assert stated(doc, "Relationships") == len(re.findall(r"^relationship ", text, re.M))


def test_the_measure_count_is_real(doc):
    text = (SM / "tables" / "_Measures.tmdl").read_text(encoding="utf-8")
    assert stated(doc, "Measures") == len(re.findall(r"^\tmeasure ", text, re.M))


def test_the_page_and_visual_counts_are_real(doc):
    """One row states both: `| Pages | **8**, **66** visuals |`."""
    row = re.search(r"\|\s*Pages\s*\|([^|]+)\|", doc, re.I)
    assert row, f"{DOC.name} no longer states a page count"
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\*\*([\d,]+)\*\*", row.group(1))]
    assert len(numbers) == 2, f"expected a page count and a visual count, got {numbers}"
    pages, visuals = numbers
    order = json.loads((REPORT / "pages" / "pages.json").read_text(encoding="utf-8"))
    assert pages == len(order["pageOrder"])
    assert visuals == len(list((REPORT / "pages").rglob("visual.json")))


def test_every_security_role_it_names_exists(doc):
    roles = {p.stem for p in (SM / "roles").glob("*.tmdl")}
    assert stated(doc, "Security roles") == len(roles)
    missing = sorted(r for r in roles if r not in doc)
    assert not missing, f"roles in the model but not named in {DOC.name}: {missing}"


def test_every_page_it_lists_exists(doc):
    """The page table is the other half: names, not just a count."""
    order = json.loads((REPORT / "pages" / "pages.json").read_text(encoding="utf-8"))
    titles = []
    for name in order["pageOrder"]:
        page = json.loads((REPORT / "pages" / name / "page.json").read_text(encoding="utf-8"))
        titles.append(page.get("displayName", name))
    missing = [t for t in titles if t not in doc]
    assert not missing, f"pages in the report but not in {DOC.name}: {missing}"


def test_it_does_not_still_say_the_report_is_unbuilt(doc):
    """The specific claim that made the stale file misleading rather than merely
    out of date. Worth its own assertion so the sentence cannot come back."""
    for phrase in ("empty named pages", "You only build visuals",
                   "Then build visuals per the build guide"):
        assert phrase not in doc, (
            f"{DOC.name} still says {phrase!r}, but the report ships built"
        )


def test_every_relative_link_resolves(doc):
    broken = [
        target for target in re.findall(r"\]\((\.\.?[^)]+)\)", doc)
        if not (DOC.parent / target.split("#")[0]).exists()
    ]
    assert not broken, f"{DOC.name} links to files that do not exist: {broken}"
