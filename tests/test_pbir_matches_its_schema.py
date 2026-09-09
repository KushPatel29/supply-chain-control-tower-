"""The report says which contract it is written to. Hold it to that.

Every file in a PBIR report definition opens with a `$schema` naming a version
Microsoft publishes. Power BI treats it as advisory and opens the file anyway,
which is why nobody notices when it is wrong - and across this portfolio 121 of
382 visuals named `visualContainer/2.10.0`, `2.11.0` or `2.12.0`. Microsoft has
published up to 2.9.0. Those URLs are 404s. One report carried three different
invented versions at once, which is the tell: the number was being typed, not
copied.

Nothing broke, and that is the problem. A `$schema` that resolves is the one
cheap way to check a hand-authored report without opening Desktop: the schema
sets `additionalProperties: false`, so a mistyped property name - the classic
silent PBIR failure, a visual that renders empty rather than erroring - fails
validation. Pointing at a version that does not exist gives that up while
looking like you have it.

Two layers here. The offline ones always run:

  * every definition file declares a `$schema` (themes are exempt - they use
    Power BI's theme format, which has no such contract),
  * all visual containers in the report declare the *same* version.

The networked one fetches each declared schema and validates every file against
it. Unreachable schemas are a loud skip, not a failure - Microsoft's schema host
being down is not a defect in this repo - but a 404 on a single version while
the others answer is a failure, because that is a version that does not exist.
Validation itself needs `jsonschema`; without it the layer skips and says so.
"""
import json
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PBIP = ROOT / "powerbi" / "pbip"
TIMEOUT = 30

# Two kinds of JSON under a PBIP that this does not govern. Themes use Power
# BI's theme format and carry no $schema. `.pbi/localSettings.json` is Desktop's
# own machine-local state - it is gitignored in every one of these repos and
# never reaches CI, so asserting anything about it would pass on a laptop and
# find nothing anywhere else. (It also names a semanticModel/localSettings
# version that 404s, which is Desktop's business, not this repo's.)
EXEMPT = ("StaticResources", ".pbi")


def definition_files():
    return sorted(
        p for p in PBIP.rglob("*.json")
        if not any(part in EXEMPT for part in p.parts)
    )


FILES = definition_files()
CASES = [pytest.param(p, id=str(p.relative_to(PBIP)).replace("\\", "/")) for p in FILES]

_cache = {}


def schema(uri):
    """Fetch and cache a schema. None when the host cannot be reached at all."""
    if uri not in _cache:
        try:
            with urllib.request.urlopen(uri, timeout=TIMEOUT) as r:
                _cache[uri] = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _cache[uri] = e.code          # 404 means the version is invented
        except Exception:
            _cache[uri] = None            # offline, DNS, timeout
    return _cache[uri]


def declared(path):
    return json.loads(path.read_text(encoding="utf-8")).get("$schema")


def test_there_are_report_files_to_check():
    """Every assertion below is vacuous if the traversal finds nothing."""
    assert FILES, f"no report definition files under {PBIP}"
    assert any("visualContainer" in (declared(p) or "") for p in FILES)


def test_every_definition_file_names_a_schema():
    """One test rather than one per file: the useful output here is the list of
    offenders, not which of them pytest reached first. Validation below is
    parametrised, because there knowing the single failing visual is the point."""
    silent = [str(p.relative_to(PBIP)).replace("\\", "/") for p in FILES if not declared(p)]
    assert not silent, (
        "no $schema, so Power BI will open these and nothing can check them:\n  "
        + "\n  ".join(silent)
    )


def test_the_report_speaks_one_visual_container_version():
    """Three versions in one report means the number was typed, not copied."""
    versions = defaultdict(list)
    for p in FILES:
        uri = declared(p) or ""
        if "visualContainer/" in uri:
            versions[uri.split("visualContainer/")[1].split("/")[0]].append(p.parent.name)
    assert versions, "no visual containers found"
    assert len(versions) == 1, (
        "visuals in this report declare "
        + ", ".join(f"{v} ({len(n)} visuals: {', '.join(sorted(n)[:3])}…)"
                    for v, n in sorted(versions.items()))
    )


def test_every_schema_the_report_names_exists():
    """The defect this file was written for. A 404 here is a version Microsoft
    never published, and it costs the report every check below."""
    uris = sorted({declared(p) for p in FILES if declared(p)})
    codes = {u: schema(u) for u in uris}
    if all(v is None for v in codes.values()):
        pytest.skip("schema host unreachable - not a claim about this repo")
    missing = sorted(u for u, v in codes.items() if isinstance(v, int))
    assert not missing, (
        "the report names schema versions that do not exist:\n  "
        + "\n  ".join(f"{u.rsplit('/definition/', 1)[-1]} -> HTTP {codes[u]}" for u in missing)
    )


@pytest.mark.parametrize("path", CASES)
def test_every_file_validates_against_the_schema_it_names(path):
    """`additionalProperties: false` throughout, so this catches the mistyped
    property that would otherwise render as a silently empty visual."""
    jsonschema = pytest.importorskip(
        "jsonschema", reason="validation needs jsonschema; the checks above still ran")
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    uri = declared(path)
    if not uri:
        pytest.skip("covered by test_every_definition_file_names_a_schema")
    root = schema(uri)
    if root is None:
        pytest.skip("schema host unreachable")
    if isinstance(root, int):
        pytest.skip("covered by test_every_schema_the_report_names_exists")

    def retrieve(ref):
        got = schema(ref)
        if got is None or isinstance(got, int):
            raise LookupError(ref)
        return Resource.from_contents(got, default_specification=DRAFT7)

    doc = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(root, registry=Registry(retrieve=retrieve))
    try:
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    except Exception as e:                       # a $ref we could not fetch
        pytest.skip(f"could not resolve a referenced schema: {e}")
    assert not errors, "\n".join(
        f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in errors[:5]
    )
