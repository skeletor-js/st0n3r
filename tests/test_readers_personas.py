"""Tests for the reader persona library and loader (U1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject
from stoner.readers.personas import (
    Persona,
    PersonaError,
    age_band,
    default_roster,
    load_personas,
    load_shipped,
    select_roster,
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Book")
    scaffold_project(proj, "Book")
    return proj


def _persona_dir(project: WritingProject) -> Path:
    d = project.root / ".stoner" / "readers" / "personas"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# shipped library
# ---------------------------------------------------------------------------


def test_shipped_library_loads_distinct_personas():
    personas = load_shipped(force_reload=True)
    assert len(personas) >= 20
    # ids are unique (dict keys) and every persona renders a non-empty block.
    assert len(personas) == len({p.id for p in personas.values()})
    for p in personas.values():
        block = p.voice_block()
        assert block.strip()
        assert p.name in block
        assert p.id in block


def test_shipped_library_spans_attributes():
    personas = load_shipped(force_reload=True)
    bands = {p.age_band for p in personas.values()}
    patience = {p.patience for p in personas.values()}
    genres = {p.primary_genre for p in personas.values()}
    # distinctness across the three axes the disagreement analysis splits on.
    assert bands >= {"teen", "20s", "30s", "40s", "50s", "60+"}
    assert patience == {"low", "medium", "high"}
    assert len(genres) >= 5


def test_age_band_boundaries():
    assert age_band(16) == "teen"
    assert age_band(20) == "20s"
    assert age_band(39) == "30s"
    assert age_band(60) == "60+"


# ---------------------------------------------------------------------------
# project overlay
# ---------------------------------------------------------------------------


def test_project_persona_new_id_merges_in(project: WritingProject):
    (_persona_dir(project) / "extra.yaml").write_text(
        "- id: custom_reader\n"
        "  name: Custom Reader\n"
        "  age: 40\n"
        "  patience: medium\n"
        "  genre_priors: [literary]\n"
        "  tastes: [likes short chapters]\n",
        encoding="utf-8",
    )
    personas = load_personas(project, force_reload=True)
    assert "custom_reader" in personas
    assert personas["custom_reader"].name == "Custom Reader"
    # shipped personas still present.
    assert len(personas) == len(load_shipped(force_reload=True)) + 1


def test_project_persona_overrides_shipped_by_id(project: WritingProject):
    shipped = load_shipped(force_reload=True)
    victim = next(iter(shipped))
    (_persona_dir(project) / "override.yaml").write_text(
        f"- id: {victim}\n"
        "  name: Overridden\n"
        "  age: 99\n"
        "  patience: high\n"
        "  genre_priors: [horror]\n"
        "  tastes: [overridden taste]\n",
        encoding="utf-8",
    )
    personas = load_personas(project, force_reload=True)
    assert personas[victim].name == "Overridden"
    assert personas[victim].age == 99
    assert len(personas) == len(shipped)  # override, not addition


def test_duplicate_ids_within_a_project_file_raise_naming_file(project: WritingProject):
    (_persona_dir(project) / "dupes.yaml").write_text(
        "- id: same\n  name: A\n  age: 30\n  patience: low\n  genre_priors: [x]\n  tastes: [y]\n"
        "- id: same\n  name: B\n  age: 31\n  patience: low\n  genre_priors: [x]\n  tastes: [y]\n",
        encoding="utf-8",
    )
    with pytest.raises(PersonaError) as exc:
        load_personas(project, force_reload=True)
    assert "dupes.yaml" in str(exc.value)
    assert "same" in str(exc.value)


def test_malformed_persona_names_file_and_field(project: WritingProject):
    (_persona_dir(project) / "broken.yaml").write_text(
        "- id: no_tastes\n  name: Nope\n  age: 30\n  patience: low\n  genre_priors: [x]\n",
        encoding="utf-8",
    )
    with pytest.raises(PersonaError) as exc:
        load_personas(project, force_reload=True)
    msg = str(exc.value)
    assert "broken.yaml" in msg
    assert "tastes" in msg


# ---------------------------------------------------------------------------
# roster selection
# ---------------------------------------------------------------------------


def test_default_roster_deterministic_and_respects_size():
    personas = load_shipped(force_reload=True)
    a = default_roster(personas, 12)
    b = default_roster(personas, 12)
    assert a == b
    assert len(a) == 12
    assert len(set(a)) == 12
    # small rosters maximize spread: an 8-pick roster still hits every patience
    # level and several genres.
    eight = default_roster(personas, 8)
    assert len({personas[i].patience for i in eight}) == 3
    assert len({personas[i].age_band for i in eight}) >= 4


def test_select_roster_explicit_ids_validated():
    personas = load_shipped(force_reload=True)
    ids = list(personas)[:3]
    assert select_roster(personas, ids, 12) == ids
    with pytest.raises(PersonaError):
        select_roster(personas, ["does_not_exist"], 12)


def test_select_roster_falls_back_to_default():
    personas = load_shipped(force_reload=True)
    assert select_roster(personas, [], 5) == default_roster(personas, 5)


def test_persona_primary_genre_and_band():
    p = Persona(
        id="x", name="X", age=25, patience="low", genre_priors=["thriller", "romance"], tastes=["fast"]
    )
    assert p.primary_genre == "thriller"
    assert p.age_band == "20s"
