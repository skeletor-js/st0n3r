"""Tests for the deterministic pacing instruments (U2). Everything here is
pure over fabricated ChapterData -- no project, no provider, no writes."""

from __future__ import annotations

from stoner.config import PacingConfig
from stoner.pacing.data import ChapterData
from stoner.pacing.instruments import (
    beat_coverage,
    classify_ending,
    ending_echo,
    length_trajectory,
    pov_cadence,
    run_instruments,
    scene_map,
)
from stoner.pacing.segments import segment_chapter
from stoner.project import count_words

CONFIG = PacingConfig()

SCENE_PARA = (
    '"Hold the line," Mara said, and the line held, barely, while Holt '
    'dragged the cart clear of the gate and the archers found their footing '
    'on the wall.'
)
SUMMARY_PARA = (
    "She had spent the winter rebuilding the granary, and by the time the "
    "thaw came she had rationed what little grain had survived. Over the "
    "next weeks the village had buried four more, and she had stopped "
    "counting the letters she had sent south."
)


def make_chapter(
    number: int,
    body: str = "",
    pov: str = "",
    words: int | None = None,
    beats_text: str = "",
    beats_fm: dict | None = None,
    has_beats: bool = False,
) -> ChapterData:
    return ChapterData(
        number=number,
        pov=pov,
        body=body,
        words=words if words is not None else count_words(body),
        beats_text=beats_text,
        beats_frontmatter=beats_fm or {},
        has_beats=has_beats,
        segments=segment_chapter(body),
    )


# -- POV cadence -------------------------------------------------------------


def test_pov_alternating_with_long_run_flags_cadence_break():
    seq = ["A", "B", "A", "B", "A", "B", "A", "A", "A", "A"]
    chapters = [make_chapter(i + 1, pov=p) for i, p in enumerate(seq)]
    result = pov_cadence(chapters, CONFIG)
    breaks = [f for f in result.findings if "cadence_break" in f.category]
    assert len(breaks) == 1
    assert breaks[0].category.startswith("ch-07:")
    assert result.series[1] == "A"


def test_pov_single_pov_book_flags_nothing():
    chapters = [make_chapter(i + 1, pov="Mara") for i in range(8)]
    result = pov_cadence(chapters, CONFIG)
    assert result.findings == []


def test_pov_appearing_once_then_vanishing_is_flagged():
    seq = ["A", "C", "A", "B", "A", "B", "A", "B"]
    chapters = [make_chapter(i + 1, pov=p) for i, p in enumerate(seq)]
    result = pov_cadence(chapters, CONFIG)
    vanish = [f for f in result.findings if "vanishing_pov" in f.category]
    assert len(vanish) == 1
    assert "'C'" in vanish[0].issue


def test_pov_missing_frontmatter_degrades_to_unknown_lane():
    chapters = [make_chapter(1, pov=""), make_chapter(2, pov="Mara")]
    result = pov_cadence(chapters, CONFIG)
    assert result.series[1] == "unknown"
    assert result.stats["unknown_pov_chapters"] == 1


# -- Length trajectory --------------------------------------------------------


def test_length_declining_series_flags_drift():
    lengths = [5000, 4600, 4100, 3400, 2700, 2100, 1600, 1000]
    chapters = [make_chapter(i + 1, words=w) for i, w in enumerate(lengths)]
    result = length_trajectory(chapters, CONFIG)
    drifts = [f for f in result.findings if "monotonic_decline" in f.category]
    assert len(drifts) == 1


def test_length_jittery_but_stable_series_does_not_flag():
    lengths = [5000, 4300, 5600, 4700, 5400, 4100, 5800]
    chapters = [make_chapter(i + 1, words=w) for i, w in enumerate(lengths)]
    result = length_trajectory(chapters, CONFIG)
    assert result.findings == []


def test_length_dead_uniform_series_flags():
    chapters = [make_chapter(i + 1, words=3000 + i % 2) for i in range(6)]
    result = length_trajectory(chapters, CONFIG)
    assert any("dead_uniform" in f.category for f in result.findings)


def test_length_outlier_flagged():
    lengths = [3000, 3100, 2900, 3050, 9000, 2950]
    chapters = [make_chapter(i + 1, words=w) for i, w in enumerate(lengths)]
    result = length_trajectory(chapters, CONFIG)
    outliers = [f for f in result.findings if "outlier" in f.category]
    assert len(outliers) == 1
    assert outliers[0].category.startswith("ch-05:")


# -- Ending echo --------------------------------------------------------------


def _punch_chapter(n: int) -> ChapterData:
    body = SCENE_PARA + "\n\n" + SCENE_PARA + "\n\nThe door stayed shut.\n"
    return make_chapter(n, body=body)


def test_ending_echo_run_of_identical_shapes_flags():
    chapters = [_punch_chapter(n) for n in range(1, 5)]
    result = ending_echo(chapters, CONFIG)
    echoes = [f for f in result.findings if "ending_echo" in f.category]
    assert len(echoes) == 1
    # all four chapters share the shape -> major
    assert echoes[0].severity.value == "major"
    assert result.stats["runs"] == [[1, 4, "one_line_punch"]]


def test_ending_echo_varied_endings_flag_nothing():
    bodies = [
        SCENE_PARA + "\n\nWould the wall hold until morning?\n",  # question
        SCENE_PARA + '\n\n"Run," she said. "Run and do not look back at any of it."\n',  # dialogue
        SCENE_PARA + "\n\n" + SCENE_PARA + "\n",  # long action -> cliff
        SCENE_PARA + "\n\nIt was done.\n",  # punch
    ]
    chapters = [make_chapter(i + 1, body=b) for i, b in enumerate(bodies)]
    result = ending_echo(chapters, CONFIG)
    assert result.findings == []
    assert len(set(result.series.values())) >= 3


def test_classify_ending_covers_all_five_shapes():
    assert classify_ending("A long paragraph.\n\nWhere had it gone?") == "question_close"
    assert classify_ending('Prose.\n\n"We hold here tonight," Mara said quietly.') == "dialogue_close"
    assert classify_ending("Prose before.\n\nThe city burned.") == "one_line_punch"
    assert classify_ending("Prose.\n\n" + SUMMARY_PARA) == "summary_close"
    assert classify_ending("Prose.\n\n" + SCENE_PARA.replace('"', "")) == "cliff_close"


# -- Scene map -----------------------------------------------------------------


def test_scene_map_flags_summary_heavy_chapter_and_quotes_block():
    body = SCENE_PARA + "\n\n" + SUMMARY_PARA + "\n\n" + SUMMARY_PARA
    ch = make_chapter(1, body=body)
    assert ch.segments.in_scene_fraction < 0.5
    result = scene_map([ch], CONFIG)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.category == "ch-01:summary_heavy"
    assert finding.quote and finding.quote in body


def test_scene_map_in_scene_chapter_yields_no_finding():
    body = SCENE_PARA + "\n\n" + SCENE_PARA + "\n\n" + SCENE_PARA
    ch = make_chapter(1, body=body)
    assert ch.segments.in_scene_fraction > 0.8
    result = scene_map([ch], CONFIG)
    assert result.findings == []
    assert result.series[1] > 0.8


# -- Beat coverage ---------------------------------------------------------------


FULL_BEATS = (
    "## Goal\n\nTake the granary.\n\n## Conflict\n\nHolt refuses.\n\n"
    "## Turn\n\nThe grain is already gone.\n\n## Exit State\n\nMara owes Holt a debt.\n"
)
BEATS_EMPTY_TURN = (
    "## Goal\n\nTake the granary.\n\n## Conflict\n\nHolt refuses.\n\n"
    "## Turn\n\n<!-- fill in -->\n\n## Exit State\n\nMara owes Holt a debt.\n"
)


def test_beat_coverage_missing_pov_mismatch_and_empty_section():
    chapters = [
        make_chapter(1, body=SCENE_PARA, pov="Mara"),  # no beat sheet
        make_chapter(
            2, body=SCENE_PARA, pov="Mara",
            beats_text=FULL_BEATS, beats_fm={"chapter": 2, "pov": "Holt"}, has_beats=True,
        ),
        make_chapter(
            3, body=SCENE_PARA, pov="Mara",
            beats_text=BEATS_EMPTY_TURN, beats_fm={"chapter": 3, "pov": "Mara"}, has_beats=True,
        ),
    ]
    result = beat_coverage(chapters, CONFIG)
    missing = [f for f in result.findings if f.category == "ch-01:beats_missing"]
    mismatch = [f for f in result.findings if f.category == "ch-02:pov_mismatch"]
    empty = [f for f in result.findings if f.category == "ch-03:empty_section"]
    assert len(missing) == 1
    assert len(mismatch) == 1
    assert len(empty) == 1
    assert "'Turn'" in empty[0].issue
    assert result.series == {1: "missing", 2: "present", 3: "present"}


# -- short-series guards / purity ------------------------------------------------


def test_two_chapter_book_no_instrument_crashes():
    chapters = [
        make_chapter(1, body=SCENE_PARA, pov="Mara"),
        make_chapter(2, body=SUMMARY_PARA, pov="Holt"),
    ]
    results = run_instruments(chapters, CONFIG)
    assert set(results) == {"scene_map", "pov", "length", "ending_echo", "beats"}
    for result in results.values():
        assert set(result.series) == {1, 2}


def test_all_findings_carry_pacing_source_and_chapter_category():
    body = SUMMARY_PARA + "\n\n" + SUMMARY_PARA
    chapters = [make_chapter(n, body=body, pov="Mara") for n in range(1, 7)]
    results = run_instruments(chapters, CONFIG)
    for name, result in results.items():
        for f in result.findings:
            assert f.source == f"pacing:{name}"
            assert f.category.startswith("ch-")
            assert ":" in f.category
