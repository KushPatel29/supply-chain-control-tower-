"""A visual a slicer cannot reach has to say so.

Half of a report's aggregate tables are standalone by design - a monthly KPI
series, a cohort triangle, a hiring funnel, a model's own AUC. Filter context
does not propagate to them, which is correct: they are computed at a grain the
dimension does not exist in. What is not correct is leaving that invisible. On
the HR retention page a Region slicer moved three of seven visuals, and the four
that did not move were the four the page is actually about. Nothing errors,
nothing blanks; the reader gets national cohort curves under a slicer reading
"Canada" and no reason to doubt them.

The rule: for each slicer, every other visual on its page either sits in that
slicer's filter path, or carries a subtitle naming that slicer as one it does
not answer to. Filter context travels from the one side of a relationship to the
many side, inactive relationships carry nothing, and bothDirections travels both
ways - so the reachable set is computed rather than assumed.

Per slicer, not per page, because a page can carry two and the weaker one hides
behind the stronger. The acute activity page has a Facility slicer and a Program
slicer; the facility-grain cards answer the first and ignore the second, and a
page-level union of what "some slicer" reaches calls that fine.

The marker is "not filtered by <slicer>", in a literal subtitle or in the DAX of
a measure-bound one, matched against the slicer's own title or column name. That
wording was chosen because it is the sentence a reader needs, so it cannot be
satisfied by a subtitle that says something else, and naming the slicer means a
note about Region cannot stand in for one about Program. A visual with no data
bindings - a header, a decoration - has nothing to be wrong about and is
skipped.

Nothing here is specific to one report; pages and model are read off disk, so
this file drops into any PBIP repo unchanged.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PBIP = ROOT / "powerbi" / "pbip"
SM = next(PBIP.glob("*.SemanticModel")) / "definition"
PAGES = next(PBIP.glob("*.Report")) / "definition" / "pages"

MARKER = "not filtered by"

MEASURE_RE = re.compile(
    r"^\tmeasure\s+(?:'([^']+)'|([A-Za-z_][\w ]*?))\s*=\s*"
    r"(.*?)(?=^\t\t[A-Za-z]\w*:|^\t(?:measure|column|partition|annotation)\s|^\t///|\Z)",
    re.M | re.S,
)

TABLES, MEASURES = set(), {}
for f in sorted((SM / "tables").glob("*.tmdl")):
    TABLES.add(f.stem)
    for quoted, bare, body in MEASURE_RE.findall(f.read_text(encoding="utf-8")):
        MEASURES[(quoted or bare).strip()] = body


def measure_tables(name, seen=None):
    """Tables a measure reads, following the measures it calls."""
    seen = seen if seen is not None else set()
    if name in seen:
        return set()
    seen.add(name)
    body = MEASURES.get(name, "")
    out = {t for t in re.findall(r"\b([A-Za-z_]\w*)\s*\[", body) if t in TABLES}
    out |= {t for t in re.findall(r"'([^']+)'\s*\[", body) if t in TABLES}
    for ref in re.findall(r"\[([^\]]+)\]", body):
        if ref in MEASURES:
            out |= measure_tables(ref, seen)
    return out


def _edges():
    """table -> tables it can filter."""
    out = defaultdict(set)
    text = (SM / "relationships.tmdl").read_text(encoding="utf-8")
    for block in re.split(r"\n(?=relationship )", text):
        if "fromColumn" not in block or re.search(r"isActive:\s*false", block):
            continue
        many = re.search(r"fromColumn:\s*([^.\s]+)\.", block).group(1)
        one = re.search(r"toColumn:\s*([^.\s]+)\.", block).group(1)
        out[one].add(many)
        if re.search(r"crossFilteringBehavior:\s*bothDirections", block):
            out[many].add(one)
    return out


EDGES = _edges()


def reachable_from(tables):
    seen, stack = set(tables), list(tables)
    while stack:
        for nxt in EDGES[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def read_visual(path):
    raw = path.read_text(encoding="utf-8")
    j = json.loads(raw)
    vis = j.get("visual", {})
    tables = set()
    for ref in set(re.findall(r'"queryRef"\s*:\s*"([^"]+)"', raw)):
        entity, _, field = ref.partition(".")
        if field in MEASURES:
            tables |= measure_tables(field)
        elif entity in TABLES:
            tables.add(entity)
    title = ""
    for block in vis.get("visualContainerObjects", {}).get("title") or []:
        expr = block.get("properties", {}).get("text", {}).get("expr", {})
        if "Literal" in expr:
            title = expr["Literal"].get("Value", "").strip("'")
    columns = sorted({
        ref.split(".", 1)[1] for ref in set(re.findall(r'"queryRef"\s*:\s*"([^"]+)"', raw))
        if "." in ref
    })
    sub = vis.get("visualContainerObjects", {}).get("subTitle") or []
    note = ""
    for block in sub:
        expr = block.get("properties", {}).get("text", {}).get("expr", {})
        if "Literal" in expr:
            note += expr["Literal"].get("Value", "")
        elif "Measure" in expr:
            note += MEASURES.get(expr["Measure"].get("Property", ""), "")
    return {
        "id": path.parent.name,
        "type": vis.get("visualType", "?"),
        "tables": tables,
        "note": note,
        "title": title,
        "columns": columns,
    }


def _names(slicer):
    """What a subtitle may call this slicer: its title, or its column name."""
    out = {slicer["title"]} | {c.replace("_", " ") for c in slicer["columns"]}
    return {n.strip().lower() for n in out if n and n.strip()}


def pages():
    for page in sorted(p for p in PAGES.iterdir() if p.is_dir()):
        visuals = [read_visual(v) for v in sorted((page / "visuals").glob("*/visual.json"))]
        if visuals:
            yield page.name, visuals


CASES = [pytest.param(name, vs, id=name) for name, vs in pages()]


def test_the_model_and_pages_were_actually_read():
    """Every assertion below is vacuous if the traversal finds nothing."""
    assert TABLES and MEASURES and CASES
    assert any(v["tables"] for _, vs in pages() for v in vs)


@pytest.mark.parametrize("page,visuals", CASES)
def test_every_visual_a_slicer_cannot_reach_says_so(page, visuals):
    slicers = [v for v in visuals if v["type"] == "slicer"]
    if not slicers:
        pytest.skip(f"{page} has no slicers")

    silent = []
    for slicer in slicers:
        reach = reachable_from(slicer["tables"])
        names = _names(slicer)
        label = slicer["title"] or ", ".join(slicer["columns"])
        for v in visuals:
            if v["type"] == "slicer" or not v["tables"] or v["tables"] & reach:
                continue
            note = v["note"].lower()
            if MARKER in note and any(n in note for n in names):
                continue
            silent.append(
                f"{v['id']} ({v['type']}) reads {sorted(v['tables'])}, which the "
                f"{label} slicer ({slicer['id']}) cannot reach, and its subtitle "
                f"does not name it"
            )
    assert not silent, (
        f"{page}: {len(silent)} visual(s) ignore a slicer on their own page "
        f"without saying so. Either relate the table, or add a subtitle "
        f'containing "{MARKER} <that slicer>":\n  ' + "\n  ".join(silent)
    )
