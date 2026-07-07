"""Tests for the voice engine (src/stoner/voice): features, fingerprint,
drift scoring, CLI, gate helper, and advisory-digest integration.

Fixture prose is generated deterministically from closed-class word pools --
original, public-domain-safe text (no copyrighted corpus is embedded). Two
strongly contrasting voices are used throughout, in the spirit of the
long-breath-Melville vs clipped-declarative pairing:

- voice A ("long breath"): 25-45 word sentences, clause chains joined by
  commas and semicolons, latinate diction, no dialogue, no contractions.
- voice B ("clipped"): 3-10 word declarative sentences, dialogue with
  contractions, short paragraphs, plain Anglo-Saxon diction.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.cli.main import app
from stoner.project import WritingProject, count_words
from stoner.types import VoiceReport
from stoner.voice.drift import run_voice, voice_context_digest, voice_gate_check
from stoner.voice.features import (
    FunctionWordError,
    extract_features,
    load_function_words,
    segment_text,
)
from stoner.voice.fingerprint import (
    FINGERPRINT_VERSION,
    Fingerprint,
    FingerprintError,
    fingerprint_path,
    learn_fingerprint,
    load_fingerprint,
    render_digest,
    save_fingerprint,
)
from stoner.voice.report import render, verdict

runner = CliRunner()

# ---------------------------------------------------------------------------
# Deterministic fixture prose (two contrasting voices)
# ---------------------------------------------------------------------------

_A_LATINATE = [
    "consideration", "circumstance", "melancholy", "providence", "contemplation",
    "magnitude", "desolation", "apprehension", "tranquility", "significance",
    "observation", "recollection", "immensity", "procession", "inclination",
]
_A_NOUNS = [
    "sea", "ship", "harbor", "street", "water", "sky", "shore", "sailor",
    "voyage", "tide", "wind", "mast", "wave", "fog", "lantern",
]
_A_VERBS = [
    "drifted", "lingered", "gathered", "beheld", "wandered", "regarded",
    "surveyed", "remembered", "followed", "carried",
]
_A_ADJS = ["grey", "silent", "vast", "weary", "ancient", "dim", "slow", "cold", "heavy", "pale"]

_B_NOUNS = ["road", "sun", "dog", "door", "truck", "rain", "boy", "gun", "hill", "creek", "barn", "fence"]
_B_VERBS = ["ran", "stopped", "looked", "took", "went", "hit", "left", "came", "stood"]
_B_ADJS = ["hot", "dry", "old", "flat", "dark"]


def long_breath_text(seed: int, paragraphs: int) -> str:
    """Voice A: long clause-chained sentences, latinate, no dialogue."""
    rng = random.Random(seed)
    out = []
    for _ in range(paragraphs):
        sents = []
        for _ in range(rng.randint(3, 5)):
            clauses = []
            for _ in range(rng.randint(2, 4)):
                clauses.append(
                    f"the {rng.choice(_A_ADJS)} {rng.choice(_A_NOUNS)} "
                    f"{rng.choice(_A_VERBS)} beneath the {rng.choice(_A_ADJS)} "
                    f"{rng.choice(_A_NOUNS)}, in the {rng.choice(_A_LATINATE)} "
                    f"of the {rng.choice(_A_NOUNS)}"
                )
            s = rng.choice([", and ", "; and ", ", for ", "; "]).join(clauses)
            sents.append(s[0].upper() + s[1:] + ".")
        out.append(" ".join(sents))
    return "\n\n".join(out)


def clipped_text(seed: int, paragraphs: int) -> str:
    """Voice B: clipped declaratives, dialogue, contractions."""
    rng = random.Random(seed)
    out = []
    for _ in range(paragraphs):
        sents = []
        for _ in range(rng.randint(4, 7)):
            kind = rng.random()
            if kind < 0.3:
                sents.append(f'"It\'s {rng.choice(_B_ADJS)} out," he said.')
            elif kind < 0.6:
                sents.append(f"The {rng.choice(_B_NOUNS)} was {rng.choice(_B_ADJS)}.")
            else:
                sents.append(
                    f"He {rng.choice(_B_VERBS)} the {rng.choice(_B_NOUNS)}. She didn't wait."
                )
        out.append(" ".join(sents))
    return "\n\n".join(out)


A_TRAIN = long_breath_text(1, 60)   # ~10k words, comfortably above COMFORT_WORDS
B_TRAIN = clipped_text(1, 200)      # ~6k words
A_HELD = long_breath_text(99, 8)    # held-out same-voice text
B_HELD = clipped_text(99, 24)


@pytest.fixture(scope="module")
def fp_a() -> Fingerprint:
    return learn_fingerprint([("a.md", A_TRAIN)])


@pytest.fixture(scope="module")
def fp_b() -> Fingerprint:
    return learn_fingerprint([("b.md", B_TRAIN)])


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "mybook")
    return tmp_path / "mybook"


def _write_exemplar(root: Path, text: str = A_TRAIN, name: str = "sample.md") -> Path:
    dest = root / "notes" / "exemplars" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# U1: feature extraction
# ---------------------------------------------------------------------------


def test_function_words_load_and_are_rich() -> None:
    words = load_function_words(force_reload=True)
    assert len(words) >= 120
    assert "the" in words and "of" in words and "no" in words
    assert all(w == w.lower() for w in words)


def test_malformed_function_words_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "fw.yaml"
    bad.write_text("not: a list\n", encoding="utf-8")
    with pytest.raises(FunctionWordError, match="YAML list"):
        load_function_words(path=bad)
    bad.write_text("- ok\n- [nested]\n", encoding="utf-8")
    with pytest.raises(FunctionWordError, match="non-string"):
        load_function_words(path=bad)


def test_contrasting_voices_separate_on_features() -> None:
    a = extract_features(A_TRAIN)
    b = extract_features(B_TRAIN)
    # rhythm: A's sentences run several times longer
    assert a["rhythm.sentence_len_mean"] > 2 * b["rhythm.sentence_len_mean"]
    # punctuation: A leans on commas and semicolons, B does not
    assert a["punctuation.commas_per_sentence"] > b["punctuation.commas_per_sentence"]
    assert a["punctuation.semicolon_per_1000"] > b["punctuation.semicolon_per_1000"]
    # register: A latinate and uncontracted, B the reverse
    assert a["register.latinate_per_1000"] > b["register.latinate_per_1000"]
    assert b["register.contraction_per_1000"] > a["register.contraction_per_1000"]
    assert a["register.word_len_mean"] > b["register.word_len_mean"]
    # dialogue: only B has any
    assert b["dialogue.ratio"] > 0.05
    assert a["dialogue.ratio"] == 0.0


def test_extract_features_is_deterministic() -> None:
    assert extract_features(A_HELD) == extract_features(A_HELD)


def test_dialogue_curly_quotes_match_straight() -> None:
    straight = 'He waited. "Come in out of the rain," she said. He did not move.'
    curly = "He waited. “Come in out of the rain,” she said. He did not move."
    s = extract_features(straight)
    c = extract_features(curly)
    assert s["dialogue.ratio"] == pytest.approx(c["dialogue.ratio"])
    assert s["dialogue.ratio"] > 0.2


def test_no_dialogue_means_zero_ratio() -> None:
    assert extract_features(A_HELD)["dialogue.ratio"] == 0.0


def test_code_fences_are_excluded_from_every_feature() -> None:
    from stoner.slop.analyzers import mask_code_fences

    p1 = "The road ran flat to the hill, and the dog followed it home."
    p2 = "Rain came in the night; the creek rose against the fence."
    fenced = p1 + "\n\n```\ndelve; tapestry! testament? said, said, said\n```\n\n" + p2
    clean = p1 + "\n\n" + p2
    fenced_vec = extract_features(mask_code_fences(fenced))
    clean_vec = extract_features(clean)
    for name, value in clean_vec.items():
        assert fenced_vec[name] == pytest.approx(value), name


def test_segmentation_respects_paragraphs() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 99 for i in range(50)]  # ~5000 words
    text = "\n\n".join(p.strip() for p in paragraphs)
    segments = segment_text(text)
    assert 4 <= len(segments) <= 6
    originals = set(p.strip() for p in paragraphs)
    for seg in segments:
        for para in seg.split("\n\n"):
            assert para in originals  # never split mid-paragraph
    # order-preserving reassembly
    assert "\n\n".join(segments) == text


def test_segmentation_small_text_is_one_segment() -> None:
    text = "One short paragraph. " * 10
    assert segment_text(text) == [text]


# ---------------------------------------------------------------------------
# U2: fingerprint learn / persist / digest / config
# ---------------------------------------------------------------------------


def test_learn_is_deterministic() -> None:
    fp1 = learn_fingerprint([("a.md", A_TRAIN)])
    fp2 = learn_fingerprint([("a.md", A_TRAIN)])
    assert fp1.features == fp2.features
    assert json.dumps({k: (v.mean, v.std) for k, v in fp1.features.items()}) == json.dumps(
        {k: (v.mean, v.std) for k, v in fp2.features.items()}
    )


def test_learn_refuses_below_hard_floor() -> None:
    with pytest.raises(FingerprintError, match="voice learn"):
        learn_fingerprint([("tiny.md", "A few words only.")])


def test_learn_flags_thin_corpus() -> None:
    text = clipped_text(7, 60)  # between 1000 and 5000 words
    assert 1000 <= count_words(text) < 5000
    fp = learn_fingerprint([("thin.md", text)])
    assert fp.thin
    fp_full = learn_fingerprint([("full.md", A_TRAIN)])
    assert not fp_full.thin


def test_learn_strips_frontmatter() -> None:
    with_fm = "---\ntitle: exemplar\n---\n\n" + A_TRAIN
    fp1 = learn_fingerprint([("a.md", with_fm)])
    fp2 = learn_fingerprint([("a.md", A_TRAIN)])
    assert fp1.features == fp2.features


def test_save_load_roundtrip(tmp_path: Path) -> None:
    project = WritingProject.create(tmp_path / "p", "p")
    fp = learn_fingerprint([("a.md", A_TRAIN)])
    path = save_fingerprint(project, fp)
    assert path == fingerprint_path(project)
    loaded = load_fingerprint(project)
    assert loaded.features == fp.features
    assert loaded.total_words == fp.total_words
    # human-readable / git-diffable JSON on disk
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == FINGERPRINT_VERSION


def test_load_missing_fingerprint_is_actionable(tmp_path: Path) -> None:
    project = WritingProject.create(tmp_path / "p", "p")
    with pytest.raises(FingerprintError, match="stoner voice learn"):
        load_fingerprint(project)


def test_load_version_mismatch_is_actionable(tmp_path: Path) -> None:
    project = WritingProject.create(tmp_path / "p", "p")
    fp = learn_fingerprint([("a.md", A_TRAIN)])
    path = save_fingerprint(project, fp)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = FINGERPRINT_VERSION + 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FingerprintError, match="stoner voice learn"):
        load_fingerprint(project)


def test_load_corrupt_json_is_actionable(tmp_path: Path) -> None:
    project = WritingProject.create(tmp_path / "p", "p")
    path = fingerprint_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(FingerprintError, match="stoner voice learn"):
        load_fingerprint(project)
    path.write_text('["a", "list"]', encoding="utf-8")
    with pytest.raises(FingerprintError, match="stoner voice learn"):
        load_fingerprint(project)


def test_digest_is_bounded_plain_text(fp_a: Fingerprint) -> None:
    digest = render_digest(fp_a)
    assert digest
    assert len(digest) <= 1200
    assert "Voice fingerprint" in digest
    assert "**" not in digest and "#" not in digest  # plain text, no markup


def test_config_roundtrips_voice_block(tmp_path: Path) -> None:
    from stoner.config import StonerConfig

    (tmp_path / "stoner.yaml").write_text(
        "project_name: p\nvoice:\n  gate: true\n  max_drift_score: 20.0\n",
        encoding="utf-8",
    )
    cfg = StonerConfig.load(tmp_path)
    assert cfg.voice.gate is True
    assert cfg.voice.max_drift_score == 20.0
    assert cfg.voice.exemplars == ["notes/exemplars"]  # default preserved


def test_config_voice_defaults_apply_when_absent(tmp_path: Path) -> None:
    from stoner.config import StonerConfig

    (tmp_path / "stoner.yaml").write_text("project_name: p\n", encoding="utf-8")
    cfg = StonerConfig.load(tmp_path)
    assert cfg.voice.gate is False
    assert cfg.voice.max_drift_score == 40.0
    assert sum(cfg.voice.weights.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# U3: drift scoring, findings, rendering
# ---------------------------------------------------------------------------


def test_calibration_same_voice_scores_in_voice(fp_a: Fingerprint, fp_b: Fingerprint) -> None:
    assert run_voice(A_HELD, fp_a).score < 15.0
    assert run_voice(B_HELD, fp_b).score < 15.0


def test_calibration_cross_voice_scores_off_voice(fp_a: Fingerprint, fp_b: Fingerprint) -> None:
    assert run_voice(B_HELD, fp_a).score >= 30.0
    assert run_voice(A_HELD, fp_b).score >= 30.0


def test_cross_voice_scores_higher_than_same_voice(fp_a: Fingerprint) -> None:
    assert run_voice(B_HELD, fp_a).score > run_voice(A_HELD, fp_a).score


def test_finding_spans_match_quoted_text(fp_a: Fingerprint) -> None:
    text = "---\ntitle: Off Voice\nstatus: draft\n---\n\n" + B_HELD
    report = run_voice(text, fp_a, path="b.md")
    assert report.findings, "expected window findings on cross-voice text"
    for f in report.findings:
        assert f.span is not None
        assert text[f.span.start : f.span.end] == f.quote
        assert f.span.start < f.span.end
        assert text.count("\n", 0, f.span.start) + 1 == f.span.line


def test_off_voice_paragraphs_inside_in_voice_text_get_a_window_finding(
    fp_a: Fingerprint,
) -> None:
    a1 = long_breath_text(50, 4)
    b = clipped_text(50, 8)  # ~230 words: comfortably more than one window
    a2 = long_breath_text(51, 4)
    mixed = a1 + "\n\n" + b + "\n\n" + a2
    b_start = len(a1) + 2
    b_end = b_start + len(b)
    report = run_voice(mixed, fp_a, path="mixed.md")
    overlapping = [
        f
        for f in report.findings
        if f.span is not None and f.span.start < b_end and f.span.end > b_start
    ]
    assert overlapping, "expected a window finding covering the off-voice block"
    top = overlapping[0]
    assert "measured" in top.issue  # names features with measured-vs-fingerprint values
    assert top.suggestion
    assert top.source.startswith("voice:")


def test_short_document_score_is_damped(fp_a: Fingerprint) -> None:
    short = clipped_text(42, 2)  # well under the damping threshold
    long = clipped_text(42, 24)
    short_report = run_voice(short, fp_a)
    long_report = run_voice(long, fp_a)
    assert short_report.stats["word_count"] < 300
    assert short_report.score < long_report.score
    assert any("damped" in note for note in short_report.stats["notes"])
    assert short_report.stats["damping"] < 1.0


def test_thin_fingerprint_damps_score_with_note() -> None:
    thin_fp = learn_fingerprint([("thin.md", clipped_text(7, 60))])
    assert thin_fp.thin
    report = run_voice(A_HELD, thin_fp)
    assert any("thin fingerprint" in note for note in report.stats["notes"])
    assert report.stats["damping"] < 1.0


def test_run_voice_without_fingerprint_is_actionable() -> None:
    with pytest.raises(FingerprintError, match="stoner voice learn"):
        run_voice("Some chapter text.", None)


def test_empty_document_scores_zero(fp_a: Fingerprint) -> None:
    report = run_voice("", fp_a, path="empty.md")
    assert report.score == 0.0
    assert report.findings == []


def test_report_kind_is_voice(fp_a: Fingerprint) -> None:
    report = run_voice(A_HELD, fp_a, path="a.md")
    assert report.kind == "voice"
    assert json.loads(report.model_dump_json())["kind"] == "voice"


def test_render_all_formats_pure(fp_a: Fingerprint, capsys: pytest.CaptureFixture[str]) -> None:
    report = run_voice(B_HELD, fp_a, path="b.md")
    rich_text = render(report, "rich")
    md_text = render(report, "markdown")
    json_text = render(report, "json")
    assert "Voice report" in rich_text and "Subscores" in rich_text
    assert "Drift score:" in md_text and "Findings" in md_text
    parsed = VoiceReport.model_validate_json(json_text)
    assert parsed.path == "b.md"
    assert parsed.score == report.score
    out, err = capsys.readouterr()
    assert out == "" and err == ""  # render never writes stdout/stderr


def test_render_unknown_format_raises(fp_a: Fingerprint) -> None:
    report = run_voice(A_HELD, fp_a)
    with pytest.raises(ValueError):
        render(report, "yaml")


def test_verdict_bands() -> None:
    assert verdict(0) == "in voice"
    assert verdict(14.9) == "in voice"
    assert verdict(15) == "drifting"
    assert verdict(29.9) == "drifting"
    assert verdict(30) == "off voice"
    assert verdict(54.9) == "off voice"
    assert verdict(55) == "broke voice"
    assert verdict(100) == "broke voice"


# ---------------------------------------------------------------------------
# U4: CLI
# ---------------------------------------------------------------------------


def test_cli_voice_learn_creates_fingerprint_and_ledgers(project_dir: Path) -> None:
    _write_exemplar(project_dir)
    result = runner.invoke(app, ["voice", "learn"])
    assert result.exit_code == 0, result.output
    assert (project_dir / ".stoner" / "voice" / "fingerprint.json").exists()
    ledger_lines = (project_dir / ".stoner" / "ledger.jsonl").read_text().splitlines()
    actions = [json.loads(line)["action"] for line in ledger_lines]
    assert "voice.learn" in actions


def test_cli_voice_learn_empty_names_the_flag(project_dir: Path) -> None:
    result = runner.invoke(app, ["voice", "learn"])
    assert result.exit_code == 1
    assert "--from-manuscript" in result.output


def test_cli_voice_learn_from_manuscript(project_dir: Path) -> None:
    project = WritingProject(project_dir)
    project.write_chapter(1, {"title": "One", "status": "revised"}, clipped_text(2, 40))
    project.write_chapter(2, {"title": "Two", "status": "final"}, clipped_text(3, 40))
    project.write_chapter(3, {"title": "Draft", "status": "draft"}, clipped_text(4, 40))
    result = runner.invoke(app, ["voice", "learn", "--from-manuscript"])
    assert result.exit_code == 0, result.output
    fp = load_fingerprint(project)
    assert "manuscript/ch-01.md" in fp.exemplars
    assert "manuscript/ch-02.md" in fp.exemplars
    assert "manuscript/ch-03.md" not in fp.exemplars  # drafts never included


def test_cli_voice_check_before_learn_is_actionable(project_dir: Path) -> None:
    result = runner.invoke(app, ["voice", "check", "1"])
    assert result.exit_code == 1
    assert "stoner voice learn" in result.output


def test_cli_voice_check_save_and_ledger(project_dir: Path) -> None:
    _write_exemplar(project_dir)
    project = WritingProject(project_dir)
    project.write_chapter(1, {"title": "One", "status": "draft"}, clipped_text(9, 24))
    assert runner.invoke(app, ["voice", "learn"]).exit_code == 0
    result = runner.invoke(app, ["voice", "check", "1", "--save"])
    assert result.exit_code == 0, result.output
    saved = list((project_dir / ".stoner" / "reviews").glob("voice-ch-01-*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["kind"] == "voice"
    entries = [
        json.loads(line)
        for line in (project_dir / ".stoner" / "ledger.jsonl").read_text().splitlines()
    ]
    checks = [e for e in entries if e["action"] == "voice.check"]
    assert checks and "score" in checks[-1]["detail"]


def test_cli_voice_check_all_without_chapters_fails_cleanly(project_dir: Path) -> None:
    _write_exemplar(project_dir)
    assert runner.invoke(app, ["voice", "learn"]).exit_code == 0
    result = runner.invoke(app, ["voice", "check", "all"])
    assert result.exit_code == 1
    assert "No chapters" in result.output


def test_cli_voice_check_json_parses_as_voice_report(project_dir: Path) -> None:
    _write_exemplar(project_dir)
    project = WritingProject(project_dir)
    project.write_chapter(1, {"title": "One", "status": "draft"}, clipped_text(9, 24))
    assert runner.invoke(app, ["voice", "learn"]).exit_code == 0
    result = runner.invoke(app, ["voice", "check", "1", "--fmt", "json"])
    assert result.exit_code == 0, result.output
    report = VoiceReport.model_validate_json(result.output)
    assert report.path == "manuscript/ch-01.md"


def test_cli_voice_show_renders_digest(project_dir: Path) -> None:
    result = runner.invoke(app, ["voice", "show"])
    assert result.exit_code == 1  # actionable before learn
    _write_exemplar(project_dir)
    assert runner.invoke(app, ["voice", "learn"]).exit_code == 0
    result = runner.invoke(app, ["voice", "show"])
    assert result.exit_code == 0, result.output
    assert "Voice fingerprint" in result.output


# ---------------------------------------------------------------------------
# U5 (helper half): deterministic gate helper
# ---------------------------------------------------------------------------


def _gate_project(tmp_path: Path) -> WritingProject:
    return WritingProject.create(tmp_path / "gate", "gate")


def test_gate_off_by_default_returns_none(tmp_path: Path) -> None:
    project = _gate_project(tmp_path)
    assert project.config.voice.gate is False
    report, fails = voice_gate_check(project, B_HELD)
    assert report is None and fails is False


def test_gate_on_without_fingerprint_degrades_silently(tmp_path: Path) -> None:
    project = _gate_project(tmp_path)
    project.config.voice.gate = True
    report, fails = voice_gate_check(project, B_HELD)
    assert report is None and fails is False


def test_gate_on_off_voice_draft_fails(tmp_path: Path, fp_a: Fingerprint) -> None:
    project = _gate_project(tmp_path)
    save_fingerprint(project, fp_a)
    project.config.voice.gate = True
    project.config.voice.max_drift_score = 20.0
    report, fails = voice_gate_check(project, B_HELD, path="manuscript/ch-01.md")
    assert report is not None
    assert fails is True
    assert report.score > 20.0


def test_gate_on_in_voice_draft_passes(tmp_path: Path, fp_a: Fingerprint) -> None:
    project = _gate_project(tmp_path)
    save_fingerprint(project, fp_a)
    project.config.voice.gate = True
    report, fails = voice_gate_check(project, A_HELD)
    assert report is not None
    assert fails is False


def test_gate_off_with_fingerprint_still_returns_none(tmp_path: Path, fp_a: Fingerprint) -> None:
    project = _gate_project(tmp_path)
    save_fingerprint(project, fp_a)
    report, fails = voice_gate_check(project, B_HELD)
    assert report is None and fails is False


# ---------------------------------------------------------------------------
# U6: advisory digest -- review pass, writer prompt, agent tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def review_project(tmp_path: Path) -> WritingProject:
    from stoner.canon.scaffold import scaffold_project

    project = WritingProject.create(tmp_path / "rev", "rev")
    scaffold_project(project, "rev")
    project.write_chapter(1, {"title": "One", "status": "draft"}, clipped_text(9, 24))
    return project


def test_build_context_without_fingerprint_has_empty_digest(review_project: WritingProject) -> None:
    from stoner.review.passes import PASSES, build_context

    ctx = build_context(review_project, 1)
    assert ctx.voice_digest == ""
    _system, user = PASSES["voice"].build_prompt(ctx)
    assert "Measured voice fingerprint" not in user


def test_build_context_with_fingerprint_populates_digest(
    review_project: WritingProject, fp_a: Fingerprint
) -> None:
    from stoner.review.passes import PASSES, build_context

    save_fingerprint(review_project, fp_a)
    ctx = build_context(review_project, 1)
    assert ctx.voice_digest
    assert "Voice fingerprint" in ctx.voice_digest
    assert "Measured drift for this chapter" in ctx.voice_digest
    _system, user = PASSES["voice"].build_prompt(ctx)
    assert "Measured voice fingerprint" in user
    assert "Voice fingerprint" in user
    # other passes are untouched by the digest
    _system, line_user = PASSES["line"].build_prompt(ctx)
    assert "Measured voice fingerprint" not in line_user


def test_voice_context_digest_degrades_to_empty(review_project: WritingProject) -> None:
    assert voice_context_digest(review_project, "Some chapter body.") == ""


def test_writer_prompt_carries_voice_digest(review_project: WritingProject, fp_a: Fingerprint) -> None:
    from stoner.pipelines.common import chapter_context, render_prompt

    ctx = chapter_context(review_project, 1)
    assert ctx["voice_digest"] == ""  # no fingerprint yet
    save_fingerprint(review_project, fp_a)
    ctx = chapter_context(review_project, 1)
    assert "Voice fingerprint" in ctx["voice_digest"]
    system = render_prompt("writer.md", ctx)
    assert "Measured voice fingerprint" in system
    assert "Voice fingerprint" in system


def test_voice_check_tool_registered_and_degrades(review_project: WritingProject, fp_a: Fingerprint) -> None:
    from stoner.engine.tools import default_registry
    from stoner.types import ToolCall

    reg = default_registry()
    assert "voice_check" in reg
    result = reg.execute(review_project, ToolCall(name="voice_check", arguments={"chapter": 1}))
    assert result.is_error
    assert result.content.startswith("ERROR: no fingerprint")
    save_fingerprint(review_project, fp_a)
    result = reg.execute(review_project, ToolCall(name="voice_check", arguments={"chapter": 1}))
    assert not result.is_error
    assert "voice drift" in result.content
    missing = reg.execute(review_project, ToolCall(name="voice_check", arguments={"chapter": 42}))
    assert missing.is_error
