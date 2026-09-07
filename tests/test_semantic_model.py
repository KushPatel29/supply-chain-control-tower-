"""
Invariants for the hand-authored semantic model.

A TMDL table names its source columns twice - once in `sourceColumn`, once in
the Power Query `TransformColumnTypes` step - and Power BI reconciles neither
against the file until someone opens Desktop and hits Refresh. A column renamed
upstream therefore breaks the model silently: the report renders blanks rather
than an error, which is worse. These tests are that reconciliation.

Nothing here is specific to one report. The path parameters are read from
expressions.tmdl, so this file drops into any PBIP repo unchanged.
"""
import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SM = next((ROOT / "powerbi" / "pbip").glob("*.SemanticModel")) / "definition"
TABLES = SM / "tables"

SOURCE_RE = re.compile(r'File\.Contents\((\w+)\s*&\s*"([^"]+\.csv)"\)')
TYPED_RE = re.compile(r'\{"([^"]+)",\s*(?:Int64\.Type|type [\w.]+)\}')
SOURCECOL_RE = re.compile(r"^\t\tsourceColumn: (.+)$", re.M)
# Columns a partition CREATES rather than reads. Enumerated by the step that
# creates them rather than by "any quoted string in the M code": the typed
# column list is quoted strings too, and exempting those would make the check
# against the CSV header vacuous.
DERIVED_RES = [
    re.compile(r'Table\.AddColumn\([^,]+,\s*"([^"]+)"'),
    re.compile(r'Table\.UnpivotOtherColumns\([^,]+,\s*\{[^}]*\},\s*"([^"]+)",\s*"([^"]+)"\)'),
    re.compile(r'Table\.RenameColumns\([^,]+,\s*\{(.+?)\}\s*\)', re.S),
    re.compile(r'Table\.Group\([^,]+,\s*\{[^}]*\},\s*\{\{"([^"]+)"'),
]
RENAME_PAIR_RE = re.compile(r'\{"[^"]+",\s*"([^"]+)"\}')


def derived_columns(text):
    """Names introduced by the Power Query steps in a partition body."""
    out = set()
    for m in DERIVED_RES[0].findall(text):
        out.add(m)
    for pair in DERIVED_RES[1].findall(text):
        out.update(pair)
    for block in DERIVED_RES[2].findall(text):
        out.update(RENAME_PAIR_RE.findall(block))
    out.update(DERIVED_RES[3].findall(text))
    return out
# A partition body is Power Query, not DAX: its square brackets are record
# syntax and must not be read as measure references.
PARTITION_RE = re.compile(
    r"^\tpartition .*?(?=\n\t(?:column |measure |partition )|\Z)", re.M | re.S)
MEASURE_QUOTED_RE = re.compile(r"^\tmeasure '([^']+)'", re.M)
MEASURE_BARE_RE = re.compile(r"^\tmeasure ([A-Za-z_][\w ]*?) =", re.M)
# A column reference is written table[column]; requiring no word character
# before the bracket leaves only measure references behind.
MEASURE_REF_RE = re.compile(r"(?<![\w'\]])\[([^\]\[]+)\]")


def segments(windows_path):
    """Split a Windows path or fragment on either separator, on any platform.

    `"\\output\\x.csv"` is one filename containing backslashes to a POSIX
    `Path`, not a directory and a file. Joining it whole is why this file
    passed on the machine that wrote the model and failed on every runner.
    """
    return [p for p in re.split(r"[\\/]+", windows_path) if p]


def path_parameters():
    """`expression <Name> = "<literal path>"` in expressions.tmdl.

    The literal is an absolute path on the machine that authored the model, so
    it does not exist on a CI runner or in anyone else's clone. Every one of
    them points inside this repository, so the path is rebased onto ROOT at the
    repository-name segment. Without that, this whole file passes locally and
    fails on every push - which is the opposite of what a gate is for.
    """
    text = (SM / "expressions.tmdl").read_text(encoding="utf-8")
    out = {}
    for name, value in re.findall(r'^expression (\w+) = "([^"]+)"', text, re.M):
        parts = segments(value.replace("\\\\", "\\"))
        if ROOT.name in parts:
            tail = parts[parts.index(ROOT.name) + 1:]
            out[name] = ROOT.joinpath(*tail) if tail else ROOT
        else:
            out[name] = Path(value)
    return out


PATHS = path_parameters()


def csv_backed_tables():
    """A partition may read several files - a fact table often joins its
    dimensions in Power Query - so collect all of them, not just the first."""
    out = []
    for f in sorted(TABLES.glob("*.tmdl")):
        text = f.read_text(encoding="utf-8")
        files = [PATHS[p].joinpath(*segments(n))
                 for p, n in SOURCE_RE.findall(text) if p in PATHS]
        if files:
            out.append((f.name, text, files))
    return out


def headers_of(files):
    cols = set()
    for path in files:
        if path.exists():
            cols |= set(next(csv.reader(path.open(encoding="utf-8-sig"))))
    return cols


CASES = csv_backed_tables()
IDS = [c[0] for c in CASES]


def test_the_path_parameters_resolve():
    assert PATHS, "no path parameter found in expressions.tmdl"
    for name, p in PATHS.items():
        assert p.exists(), f"{name} points at {p}, which does not exist"


def test_there_are_csv_backed_tables_to_check():
    """A regex that silently matches nothing would make every test below pass."""
    assert len(CASES) >= 2


@pytest.mark.parametrize("name,text,files", CASES, ids=IDS)
def test_the_files_the_table_reads_exist(name, text, files):
    missing = [str(f) for f in files if not f.exists()]
    assert not missing, f"{name} reads files that are not there: {missing}"


@pytest.mark.parametrize("name,text,files", CASES, ids=IDS)
def test_every_typed_column_exists_in_the_csv(name, text, files):
    header = headers_of(files) | derived_columns(text)
    typed = TYPED_RE.findall(text)
    assert typed, f"{name} types no columns at all"
    missing = [c for c in typed if c not in header]
    assert not missing, f"{name} types columns nothing it reads has: {missing}"


@pytest.mark.parametrize("name,text,files", CASES, ids=IDS)
def test_every_source_column_exists_in_the_csv(name, text, files):
    """`sourceColumn` is what a column actually binds to. A rename here renders
    blank rather than failing, which is the worst of the two.

    Columns a partition computes in Power Query are exempt: they are named in
    the step that creates them, not in any file header."""
    header = headers_of(files) | derived_columns(text)
    missing = [c.strip().strip("'") for c in SOURCECOL_RE.findall(text)
               if c.strip().strip("'") not in header]
    assert not missing, f"{name} binds columns nothing it reads has: {missing}"


@pytest.mark.parametrize("name,text,files", CASES, ids=IDS)
def test_a_sort_by_column_is_a_column_of_the_same_table(name, text, files):
    """`sortByColumn` pointing at a name the table does not have makes Desktop
    refuse to load the model - and it is easy to write while renaming."""
    own = {c.strip().strip("'") for c in SOURCECOL_RE.findall(text)}
    own |= {c.strip().strip("'")
            for c in re.findall(r"^\tcolumn '?([^'\n]+?)'?$", text, re.M)}
    for target in re.findall(r"^\t\tsortByColumn: (.+)$", text, re.M):
        assert target.strip().strip("'") in own, \
            f"{name} sorts by '{target}', which is not one of its columns"


def test_every_table_file_is_registered_in_the_model():
    """A tables/*.tmdl with no `ref table` line is invisible to Power BI."""
    model = (SM / "model.tmdl").read_text(encoding="utf-8")
    refs = {r.strip().strip("'") for r in re.findall(r"^ref table (.+)$", model, re.M)}
    on_disk = {f.stem for f in TABLES.glob("*.tmdl")}
    assert on_disk - refs == set(), f"unregistered tables: {sorted(on_disk - refs)}"


def test_every_relationship_names_a_table_that_exists():
    rel = SM / "relationships.tmdl"
    if not rel.exists():
        pytest.skip("no relationships file")
    on_disk = {f.stem for f in TABLES.glob("*.tmdl")}
    for line in re.findall(r"^\t(?:from|to)Column: (.+)$",
                           rel.read_text(encoding="utf-8"), re.M):
        table = line.rsplit(".", 1)[0].strip().strip("'")
        assert table in on_disk, f"relationship references missing table {table}"


def test_every_measure_reference_resolves():
    """`[Some Measure]` in a DAX expression that names nothing is a
    SemanticError at load time and "Something's wrong with one or more fields"
    at render time - the visual, not the model, is where it surfaces.

    Partition bodies are stripped first: Power Query uses square brackets for
    record syntax, and `[Delimiter = ",", Encoding = 65001]` is not a measure.
    """
    defined, referenced, columns = set(), set(), set()
    for f in TABLES.glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        dax = PARTITION_RE.sub("", text)
        defined |= set(MEASURE_QUOTED_RE.findall(dax))
        defined |= set(MEASURE_BARE_RE.findall(dax))
        columns |= {c.strip().strip("'") for c in SOURCECOL_RE.findall(text)}
        columns |= {c.strip().strip("'")
                    for c in re.findall(r"^\tcolumn '?([^'\n]+?)'?$", text, re.M)}
        referenced |= set(MEASURE_REF_RE.findall(dax))

    assert defined, "no measures found, so this proves nothing"
    unknown = {r for r in referenced if r not in defined and r not in columns}
    assert not unknown, f"measures referenced but not defined: {sorted(unknown)}"


def test_every_dax_var_carries_the_v_prefix():
    """DAX reserves far more words for VAR names than the documented four.

    `Goal`, `Status`, `Trend` and `Variance` are the well-known ones, but
    `Move` and `Scope` fail the same way, and there is no published list. The
    failure is invisible until runtime: the measure lands in SemanticError, its
    dependents in DependencyError, and the bound visual renders "Something's
    wrong with one or more fields" on a report that built and validated
    cleanly.

    Rather than tracking Microsoft's reserved words, every VAR carries a `v`
    prefix followed by a capital - a shape no keyword has.
    """
    bad = []
    for f in TABLES.glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"\bVAR\s+([A-Za-z_]\w*)", text):
            if not re.match(r"^v[A-Z]", m.group(1)):
                bad.append(f"{f.name}: VAR {m.group(1)}")
    assert not bad, ("VAR names that could collide with a DAX keyword: "
                     + ", ".join(sorted(set(bad))))


@pytest.mark.parametrize("name,text,files", CASES, ids=IDS)
def test_every_bound_column_is_also_typed(name, text, files):
    """A column that is read but never typed arrives as text.

    `Table.PromoteHeaders` keeps every column in the file; only the ones named
    in `TransformColumnTypes` get a type. A numeric column left out of that
    list still binds, still renders, and still sorts - alphabetically - and any
    measure that multiplies it fails at query time with Power BI's generic
    "this might be caused by a capacity or license issue".
    """
    typed = set(TYPED_RE.findall(text))
    derived = derived_columns(text)
    missing = [c.strip().strip("'") for c in SOURCECOL_RE.findall(text)
               if c.strip().strip("'") not in typed
               and c.strip().strip("'") not in derived]
    assert not missing, f"{name} binds columns it never types: {missing}"
