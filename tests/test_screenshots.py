"""
The screenshots in the README have to be of different pages.

This exists because they were not. A capture run that "moved to the next page"
between shots never moved - Power BI Desktop ignores synthetic keystrokes, so
every frame was the page the file happened to open on - and the result was
committed and published: one report shipped two distinct images for six pages,
another a single image repeated under five different captions. Every number in
those documents was correct. The pictures were not, and nothing was looking.

Nothing here needs a third-party library: PNG dimensions come out of the IHDR
chunk, which is the first thing after the signature.
"""
import hashlib
import json
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "powerbi" / "screenshots"
PBIP = ROOT / "powerbi" / "pbip"
README = ROOT / "README.md"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path):
    """(width, height) from the IHDR chunk."""
    data = path.read_bytes()[:24]
    assert data[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def shots():
    return sorted(p for p in SHOTS.glob("*.png"))


def readme_images():
    text = README.read_text(encoding="utf-8")
    return [m for m in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
            if "screenshots/" in m]


def report_pages():
    meta = next(PBIP.glob("*.Report/definition/pages/pages.json"))
    return json.loads(meta.read_text(encoding="utf-8"))["pageOrder"]


def test_there_are_screenshots_to_check():
    """A glob that matches nothing would make every test below pass."""
    assert SHOTS.is_dir(), "no screenshots directory"
    assert len(shots()) >= 3


def test_no_two_screenshots_are_the_same_image():
    """The defect this file was written for. Two captions over one picture is
    not a near-miss - it is the reader being told something untrue about what
    the report contains."""
    seen = {}
    duplicates = []
    for p in shots():
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in seen:
            duplicates.append(f"{p.name} == {seen[digest]}")
        seen[digest] = p.name
    assert not duplicates, "identical screenshots: " + "; ".join(duplicates)


def test_every_page_of_the_report_has_a_screenshot():
    """A page nobody has photographed is a page nobody has looked at."""
    pages = report_pages()
    assert len(shots()) >= len(pages), (
        f"the report has {len(pages)} pages and there are {len(shots())} "
        "screenshots")


def test_every_screenshot_is_shown_in_the_readme():
    """An orphan file in screenshots/ is either a page the README forgot or a
    leftover from a page that no longer exists."""
    shown = {Path(m).name for m in readme_images()}
    orphans = sorted(p.name for p in shots() if p.name not in shown)
    assert not orphans, f"screenshots the README never shows: {orphans}"


def test_every_readme_image_exists():
    missing = [m for m in readme_images() if not (ROOT / m).exists()]
    assert not missing, f"README images that are not there: {missing}"


@pytest.mark.parametrize("path", shots(), ids=lambda p: p.name)
def test_each_screenshot_looks_like_a_report_page(path):
    """Catches the two ways a capture goes wrong without erroring: a frame
    clipped to a corner, and a window grabbed before it had drawn."""
    width, height = png_size(path)
    assert width >= 1000, f"{path.name} is {width}px wide - clipped?"
    ratio = width / height
    assert 1.3 <= ratio <= 2.3, (
        f"{path.name} is {width}x{height} (ratio {ratio:.2f}); a report page "
        "is roughly 16:9")
    assert path.stat().st_size > 40_000, (
        f"{path.name} is {path.stat().st_size} bytes - too plain to be a "
        "rendered page")
