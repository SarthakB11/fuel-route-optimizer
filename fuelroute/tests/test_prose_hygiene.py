"""Documentation and comment hygiene.

The project's prose is meant to read consistently, so a few mechanical slips are
turned into build failures rather than review comments. These checks run over the
hand written source and documentation only. The generated data files under data/ are
excluded: they carry thousands of real US place and business names, and substring
matching against those produces nothing but false positives, for example the word
"loom" inside "Bloomington" and the town of Nanakuli in Hawaii.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CHECKED_SUFFIXES = {".py", ".md", ".html", ".yml", ".yaml", ".toml", ".cfg"}

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "data",
    "htmlcov",
}

EM_DASH = "—"

# Words that read as filler in technical writing. Checked as whole words so that, for
# example, "landscape" does not fire on a place name inside a docstring example.
FILLER_WORDS = ("delve", "foster", "seamless", "utilize", "utilise")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in CHECKED_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def test_there_are_files_to_check() -> None:
    """Guard against the walker silently matching nothing and passing vacuously."""
    files = _source_files()
    assert len(files) > 15, f"expected to find the project sources, found {len(files)}"
    names = {f.name for f in files}
    assert "README.md" in names
    assert "optimizer.py" in names


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_em_dashes(path: Path) -> None:
    """Em dashes are not used anywhere in this project's prose."""
    if path.name == Path(__file__).name:
        return
    text = path.read_text(encoding="utf-8")
    if EM_DASH in text:
        line = next(i for i, content in enumerate(text.splitlines(), 1) if EM_DASH in content)
        pytest.fail(f"{path.relative_to(REPO_ROOT)}:{line} contains an em dash")


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_filler_words(path: Path) -> None:
    """Keep the writing plain."""
    if path.name == Path(__file__).name:
        return
    text = path.read_text(encoding="utf-8")
    for word in FILLER_WORDS:
        match = re.search(rf"\b{word}\b", text, re.IGNORECASE)
        if match:
            line = text[: match.start()].count("\n") + 1
            pytest.fail(f"{path.relative_to(REPO_ROOT)}:{line} uses the filler word '{word}'")
