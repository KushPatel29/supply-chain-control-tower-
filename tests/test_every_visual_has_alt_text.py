"""Every visual describes itself for a screen reader.

Power BI exposes a visual's alt text to assistive technology; without it a
reader hears a visual type and nothing else. Nothing in this portfolio had any,
across 382 visuals in six reports.

Alt text lives at `visual.visualContainerObjects.general[].properties.altText`
and is serialised like every other formatting property here: a single-quoted
string literal inside an `expr`. The PBIR schema sets `additionalProperties:
false` on that object, so a mistyped property name fails
`test_pbir_matches_its_schema` -- which is what makes this safe to hand-author
without opening Desktop. What the schema cannot check is whether the text
describes *this* visual, so that is checked here: the description has to name a
field the visual actually binds. Boilerplate pasted onto the wrong chart fails.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PBIP = ROOT / "powerbi" / "pbip"

VISUALS = sorted(PBIP.glob("*.Report/definition/pages/*/visuals/*/visual.json"))
CASES = [pytest.param(p, id=f"{p.parent.parent.parent.name}/{p.parent.name}") for p in VISUALS]


def _alt(visual: dict) -> str | None:
    for entry in visual.get("visualContainerObjects", {}).get("general", []) or []:
        node = (entry.get("properties") or {}).get("altText")
        if node:
            return str(node.get("expr", {}).get("Literal", {}).get("Value", ""))
    return None


def _bound_names(visual: dict) -> set[str]:
    """Every field the visual reads, in both raw and spoken form."""
    names: set[str] = set()
    state = visual.get("query", {}).get("queryState", {})
    for role in state.values():
        for proj in role.get("projections", []):
            raw = proj.get("nativeQueryRef") or ""
            field = proj.get("field", {})
            for kind in ("Measure", "Column", "Aggregation"):
                if kind in field:
                    raw = field[kind].get("Property", raw) or raw
                    break
            if raw:
                names.add(raw)
                names.add(raw.replace("_", " "))
    return names


def test_there_are_visuals_to_check():
    """Anti-vacuity: a glob that matched nothing would make every case below pass."""
    assert VISUALS, f"no visuals found under {PBIP}"
    assert len(VISUALS) > 10, f"only {len(VISUALS)} visuals found - glob looks wrong"


@pytest.mark.parametrize("path", CASES)
def test_every_visual_has_alt_text(path: Path):
    visual = json.loads(path.read_text(encoding="utf-8"))["visual"]
    alt = _alt(visual)
    assert alt, (
        f"{path.parent.name} ({visual.get('visualType')}) has no alt text, so a "
        "screen reader announces its type and nothing else"
    )
    assert alt.startswith("'") and alt.endswith("'"), (
        f"alt text {alt!r} is not a single-quoted string literal; Power BI drops "
        "formatting literals that are not quoted the way the title objects are"
    )
    assert len(alt.strip("'")) > 8, f"alt text {alt!r} is too short to describe anything"


@pytest.mark.parametrize("path", CASES)
def test_alt_text_describes_this_visual(path: Path):
    """Guards against a correct-looking description pasted onto the wrong chart."""
    visual = json.loads(path.read_text(encoding="utf-8"))["visual"]
    alt = (_alt(visual) or "").strip("'")
    bound = _bound_names(visual)
    if not bound:
        pytest.skip("visual binds no fields, so there is no name to match")
    assert any(name and name in alt for name in bound), (
        f"alt text {alt!r} names none of the fields {sorted(n for n in bound if n)} "
        "that this visual actually binds"
    )
