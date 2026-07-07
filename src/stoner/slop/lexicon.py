"""Lexicon loading for the slop detector.

Three flat YAML files under `data/` describe the vocabulary the analyzers
match against:

- ``words.yaml``    -- single tokens, matched whole-word / case-insensitive.
- ``phrases.yaml``  -- literal multi-word substrings, case-insensitive.
- ``patterns.yaml`` -- named regexes, case-insensitive.

Each entry carries a :class:`~stoner.types.Severity`. Entries are compiled
once and cached; call :func:`load_lexicon` with ``force_reload=True`` in
tests that need to bypass the cache (e.g. after monkeypatching the data
directory).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..types import Severity

DATA_DIR = Path(__file__).parent / "data"


class LexiconError(ValueError):
    """Raised when a lexicon YAML file is malformed."""


def _severity(raw: Any, *, where: str) -> Severity:
    try:
        return Severity(str(raw))
    except ValueError as exc:
        raise LexiconError(f"{where}: invalid severity {raw!r}") from exc


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise LexiconError(f"missing lexicon file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise LexiconError(f"{path} must contain a YAML list of mappings")
    for item in raw:
        if not isinstance(item, dict):
            raise LexiconError(f"{path} has a non-mapping entry: {item!r}")
    return raw


@dataclass(frozen=True)
class WordEntry:
    term: str
    severity: Severity
    regex: re.Pattern[str] = field(compare=False, repr=False)
    note: str = ""


@dataclass(frozen=True)
class PhraseEntry:
    phrase: str
    severity: Severity
    regex: re.Pattern[str] = field(compare=False, repr=False)
    note: str = ""


@dataclass(frozen=True)
class PatternEntry:
    name: str
    pattern: str
    severity: Severity
    regex: re.Pattern[str] = field(compare=False, repr=False)
    note: str = ""


@dataclass(frozen=True)
class Lexicon:
    words: tuple[WordEntry, ...]
    phrases: tuple[PhraseEntry, ...]
    patterns: tuple[PatternEntry, ...]


def load_words(path: Path | None = None) -> list[WordEntry]:
    path = path or (DATA_DIR / "words.yaml")
    out: list[WordEntry] = []
    for item in _load_yaml_list(path):
        term = str(item["term"])
        sev = _severity(item.get("severity", "minor"), where=f"word {term!r}")
        note = str(item.get("note", "") or "")
        rx = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        out.append(WordEntry(term=term, severity=sev, note=note, regex=rx))
    return out


def load_phrases(path: Path | None = None) -> list[PhraseEntry]:
    path = path or (DATA_DIR / "phrases.yaml")
    out: list[PhraseEntry] = []
    for item in _load_yaml_list(path):
        phrase = str(item["phrase"])
        sev = _severity(item.get("severity", "minor"), where=f"phrase {phrase!r}")
        note = str(item.get("note", "") or "")
        # Collapse literal whitespace in the phrase to `\s+` so minor
        # reflow (e.g. a line-wrapped source file) still matches.
        parts = [re.escape(p) for p in phrase.split(" ")]
        rx = re.compile(r"\s+".join(parts), re.IGNORECASE)
        out.append(PhraseEntry(phrase=phrase, severity=sev, note=note, regex=rx))
    return out


def load_patterns(path: Path | None = None) -> list[PatternEntry]:
    path = path or (DATA_DIR / "patterns.yaml")
    out: list[PatternEntry] = []
    for item in _load_yaml_list(path):
        name = str(item["name"])
        pattern = str(item["pattern"])
        sev = _severity(item.get("severity", "minor"), where=f"pattern {name!r}")
        note = str(item.get("note", "") or "")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise LexiconError(f"pattern {name!r} does not compile: {exc}") from exc
        out.append(PatternEntry(name=name, pattern=pattern, severity=sev, note=note, regex=rx))
    return out


_cache: Lexicon | None = None


def load_lexicon(*, force_reload: bool = False) -> Lexicon:
    """Load (and cache) the built-in word/phrase/pattern lexicons."""
    global _cache
    if _cache is None or force_reload:
        _cache = Lexicon(
            words=tuple(load_words()),
            phrases=tuple(load_phrases()),
            patterns=tuple(load_patterns()),
        )
    return _cache
