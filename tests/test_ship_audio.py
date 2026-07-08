"""Table-read tests: dialogue attribution + voice map (U7) and TTS backends,
synthesis, stitching, `ship all` (U8).

Zero real network and zero real `say` dependence: audio tests use a
test-local FakeTTS backend that writes silence WAVs, and the openai adapter is
exercised against a mocked httpx transport. ffmpeg is monkeypatched off so
stitching never shells out. The novella corpus is read-only (copied to
tmp_path)."""

from __future__ import annotations

import shutil
import wave
from pathlib import Path

import httpx
import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.ship.dialogue import (
    UNKNOWN,
    build_script,
    build_voice_map,
)
from stoner.ship.tts import TTSBackend
from stoner.types import CompletionRequest, CompletionResponse, Usage

CORPUS = Path(__file__).resolve().parent.parent / "examples" / "novella"


def corpus_copy(tmp_path: Path) -> WritingProject:
    dest = tmp_path / "novella"
    shutil.copytree(CORPUS, dest)
    return WritingProject(dest)


def _project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "book")
    scaffold_project(p, "book")
    return p


def _add_character(project: WritingProject, slug: str, name: str) -> None:
    project.write(f"canon/characters/{slug}.md", f"---\nname: {name}\n---\n\nBody.\n")


def _chapter(project: WritingProject, number: int, body: str, status: str = "revised") -> None:
    project.write_chapter(number, {"title": f"Ch {number}", "status": status}, body)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTTS(TTSBackend):
    name = "fake"
    is_network = False
    sample_rate = 8000

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_voices(self) -> list[str]:
        return ["v1", "v2", "v3"]

    def synthesize(self, text: str, voice: str, dest_wav: Path) -> None:
        self.calls.append((voice, text))
        dest_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(dest_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(b"\x00\x00" * int(self.sample_rate * 0.1))


class FakeProvider(Provider):
    name = "fake"
    supports_tools = False

    def __init__(self, text: str):
        self.text = text
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        return CompletionResponse(text=self.text, stop_reason="end", usage=Usage(input_tokens=3, output_tokens=4))


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    # Keep synthesis hermetic: never shell out to a real ffmpeg.
    monkeypatch.setattr("stoner.ship.audio.shutil.which", lambda name: None)


# ---------------------------------------------------------------------------
# U7: attribution ladder
# ---------------------------------------------------------------------------


def test_tag_attribution(tmp_path: Path):
    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _add_character(project, "denny-calloway", "Denny Calloway")
    _chapter(project, 1, '"You want to know what I think." Denny said it like he was deciding.')
    script = build_script(project, 1)
    dialogue = [s for s in script.segments if s.kind == "dialogue"]
    assert dialogue[0].speaker == "denny-calloway"
    assert dialogue[0].attribution == "tag"


def test_alternation_and_third_speaker_reset(tmp_path: Path):
    project = _project(tmp_path)
    for slug, name in [("ruth-vann", "Ruth Vann"), ("denny-calloway", "Denny Calloway"), ("wyatt-coombs", "Wyatt Coombs")]:
        _add_character(project, slug, name)
    body = (
        '"You came back," Denny said.\n\n'
        '"I did," Ruth said.\n\n'
        '"Sit down."\n\n'
        '"I\'ll stand."\n\n'
        '"Enough," Wyatt said.\n\n'
        '"Fine."'
    )
    _chapter(project, 1, body)
    script = build_script(project, 1)
    dialogue = [s for s in script.segments if s.kind == "dialogue"]
    speakers = [(s.speaker, s.attribution) for s in dialogue]
    assert speakers[0] == ("denny-calloway", "tag")
    assert speakers[1] == ("ruth-vann", "tag")
    assert speakers[2] == ("denny-calloway", "alternation")
    assert speakers[3] == ("ruth-vann", "alternation")
    assert speakers[4] == ("wyatt-coombs", "tag")
    # After a third tagged speaker the pair is (ruth, wyatt); the bare line
    # alternates from wyatt -> ruth.
    assert speakers[5][0] == "ruth-vann"


def test_curly_and_straight_quotes_extract(tmp_path: Path):
    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _chapter(project, 1, '“Curly here,” Ruth said. And then "straight here," Ruth said.')
    script = build_script(project, 1)
    dialogue = [s for s in script.segments if s.kind == "dialogue"]
    assert dialogue[0].text == "Curly here,"
    assert dialogue[1].text == "straight here,"


def test_unclosed_quote_degrades_to_narration(tmp_path: Path):
    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _chapter(project, 1, 'She said "this never closes and the paragraph keeps going without end')
    script = build_script(project, 1)
    assert all(s.kind == "narration" for s in script.segments)


def test_assist_resolves_unknowns_and_tolerates_garbage(tmp_path: Path):
    from stoner.ship.dialogue import assist_unknowns

    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _add_character(project, "denny-calloway", "Denny Calloway")
    # A bare quote with no tag/name and no alternation established -> UNKNOWN.
    _chapter(project, 1, '"Who is even speaking here."')
    script = build_script(project, 1)
    assert any(s.speaker == UNKNOWN for s in script.segments)

    good = FakeProvider('```json\n{"attributions": {"0": "ruth-vann"}}\n```')
    resolved, _usage = assist_unknowns(project, script, provider=good)
    llm = [s for s in resolved.segments if s.attribution == "llm"]
    assert llm and llm[0].speaker == "ruth-vann"

    # Garbage reply leaves lines UNKNOWN.
    script2 = build_script(project, 1)
    junk = FakeProvider("not json at all")
    resolved2, _u2 = assist_unknowns(project, script2, provider=junk)
    assert any(s.speaker == UNKNOWN for s in resolved2.segments)


def test_corpus_ch08_deterministic_attribution(tmp_path: Path):
    project = corpus_copy(tmp_path)
    script = build_script(project, 8)  # no provider -> zero LLM calls
    monologue = [
        s for s in script.segments
        if s.kind == "dialogue" and "know what I think" in s.text
    ]
    assert monologue and monologue[0].speaker == "denny-calloway"
    ruth_reply = [
        s for s in script.segments
        if s.kind == "dialogue" and "waiting for the part" in s.text
    ]
    assert ruth_reply and ruth_reply[0].speaker == "ruth-vann"


# ---------------------------------------------------------------------------
# U7: voice map
# ---------------------------------------------------------------------------


def test_voice_map_ranks_and_preserves(tmp_path: Path):
    counts = {"ruth-vann": 40, "denny-calloway": 12, "wyatt-coombs": 3}
    vm = build_voice_map(counts, ["v1", "v2", "v3"], narrator_voice="narr")
    assert vm["narrator"] == "narr"
    # Highest line count gets the first voice.
    assert vm["characters"]["ruth-vann"] == "v1"

    # Regeneration preserves a hand-edited assignment and appends a new char.
    edited = {"narrator": "narr", "characters": dict(vm["characters"])}
    edited["characters"]["ruth-vann"] = "custom"
    counts2 = dict(counts)
    counts2["iris-vann"] = 5
    vm2 = build_voice_map(counts2, ["v1", "v2", "v3"], narrator_voice="narr", existing=edited)
    assert vm2["characters"]["ruth-vann"] == "custom"
    assert "iris-vann" in vm2["characters"]


# ---------------------------------------------------------------------------
# U8: synthesis + stitching + cache
# ---------------------------------------------------------------------------


def _voice_map() -> dict:
    return {"narrator": "v1", "characters": {"ruth-vann": "v2", "denny-calloway": "v3"}}


def test_synthesize_and_stitch_frame_count(tmp_path: Path):
    from stoner.ship.audio import synthesize_chapter

    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _add_character(project, "denny-calloway", "Denny Calloway")
    _chapter(project, 1, '"One," Ruth said.\n\n"Two," Denny said.\n\nNarration three.')
    backend = FakeTTS()
    res = synthesize_chapter(project, 1, backend, _voice_map())
    assert res.wav_path.exists()
    with wave.open(str(res.wav_path), "rb") as w:
        frames = w.getnframes()
    per_chunk = int(backend.sample_rate * 0.1)
    assert frames == per_chunk * res.chunk_count
    assert res.synthesized == res.chunk_count


def test_stitch_params_mismatch_errors(tmp_path: Path):
    from stoner.ship.audio import _stitch
    from stoner.ship.tts import TTSError

    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    for path, rate in ((a, 8000), (b, 16000)):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * 100)
    with pytest.raises(TTSError, match="b.wav"):
        _stitch([a, b], tmp_path / "out.wav", 8000)


def test_cache_resumes_and_reuses(tmp_path: Path):
    from stoner.ship.audio import synthesize_chapter

    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _chapter(project, 1, '"One," Ruth said.\n\nNarration.')
    backend = FakeTTS()
    first = synthesize_chapter(project, 1, backend, _voice_map())
    assert first.synthesized == first.chunk_count and first.cache_hits == 0

    backend2 = FakeTTS()
    second = synthesize_chapter(project, 1, backend2, _voice_map())
    assert second.synthesized == 0 and len(backend2.calls) == 0
    assert second.cache_hits == second.chunk_count

    # Delete one cache file -> exactly one re-synthesis.
    cache_files = sorted((project.root / "export" / "audio" / "cache").glob("*.wav"))
    cache_files[0].unlink()
    backend3 = FakeTTS()
    third = synthesize_chapter(project, 1, backend3, _voice_map())
    assert third.synthesized == 1


def test_dialogue_only_and_announce(tmp_path: Path):
    from stoner.ship.audio import synthesize_chapter

    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _add_character(project, "denny-calloway", "Denny Calloway")
    _chapter(project, 1, '"One," Ruth said.\n\nNarration middle.\n\n"Two," Denny said.')

    # dialogue-only: narration text never reaches the backend.
    backend = FakeTTS()
    synthesize_chapter(project, 1, backend, _voice_map(), dialogue_only=True)
    assert all("Narration middle" not in text for _v, text in backend.calls)

    # announce: each speaker change adds a narrator-voiced name chunk.
    backend2 = FakeTTS()
    synthesize_chapter(project, 1, backend2, _voice_map(), dialogue_only=True, announce=True)
    announced = [text for _v, text in backend2.calls if text in ("Ruth Vann.", "Denny Calloway.")]
    assert announced == ["Ruth Vann.", "Denny Calloway."]


def test_audio_ledgers_backend(tmp_path: Path):
    from stoner.ship.audio import synthesize_chapter

    project = _project(tmp_path)
    _add_character(project, "ruth-vann", "Ruth Vann")
    _chapter(project, 1, '"Hi," Ruth said.')
    synthesize_chapter(project, 1, FakeTTS(), _voice_map())
    tail = Ledger(project.root).tail(1)[0]
    assert tail.action == "ship.audio"
    assert tail.detail["backend"] == "fake"


# ---------------------------------------------------------------------------
# U8: backend selection + openai adapter
# ---------------------------------------------------------------------------


def test_say_backend_missing_errors(monkeypatch):
    from stoner.config import StonerConfig
    from stoner.ship.tts import TTSError, get_backend

    monkeypatch.setattr("stoner.ship.tts.shutil.which", lambda name: None)
    with pytest.raises(TTSError, match="say"):
        get_backend("say", StonerConfig())


def test_openai_backend_missing_key_errors(monkeypatch):
    from stoner.config import StonerConfig
    from stoner.ship.tts import TTSError, get_backend

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TTSError, match="OPENAI_API_KEY"):
        get_backend("openai", StonerConfig())


def test_openai_adapter_request_shape(tmp_path: Path):
    from stoner.ship.tts import OpenAIBackend

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, content=b"RIFFfakewavdata")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = OpenAIBackend(api_key="secret-key", model="gpt-4o-mini-tts", client=client)
    dest = tmp_path / "out.wav"
    backend.synthesize("hello world", "alloy", dest)
    assert seen["url"].endswith("/v1/audio/speech")
    assert seen["body"]["model"] == "gpt-4o-mini-tts"
    assert seen["body"]["voice"] == "alloy"
    assert seen["body"]["input"] == "hello world"
    assert seen["body"]["response_format"] == "wav"
    assert seen["auth"] == "Bearer secret-key"
    assert dest.read_bytes() == b"RIFFfakewavdata"


# ---------------------------------------------------------------------------
# U8: ship all + corpus end-to-end (R19)
# ---------------------------------------------------------------------------


def test_ship_all_partial_failure_when_pdf_extra_missing(tmp_path: Path, monkeypatch):
    import builtins

    from stoner.ship.epub import write_epub
    from stoner.ship.manifest import require_ready
    from stoner.ship.pdf import write_pdf
    from stoner.ship.shunn import write_docx

    project = _project(tmp_path)
    _chapter(project, 1, "A finished paragraph.")
    manifest = require_ready(project, allow_incomplete=False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("reportlab"):
            raise ImportError("no reportlab")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # epub + docx succeed; pdf fails.
    epub_path, _ = write_epub(project, manifest=manifest)
    docx_path, _ = write_docx(project, manifest=manifest)
    from stoner.ship.manifest import ShipError

    with pytest.raises(ShipError, match=r"st0n3r\[export\]"):
        write_pdf(project, manifest=manifest)
    assert epub_path.exists() and docx_path.exists()


def test_corpus_full_line_end_to_end(tmp_path: Path, monkeypatch):
    from stoner.ship.audio import generate_voices, synthesize_chapter
    from stoner.ship.blurbs import run_blurbs
    from stoner.ship.epub import write_epub
    from stoner.ship.manifest import build_manifest
    from stoner.ship.shunn import write_docx

    project = corpus_copy(tmp_path)
    # Readiness passes with warnings, no override needed.
    manifest = build_manifest(project)
    assert manifest.ready and manifest.warnings

    epub_path, epub_chapters = write_epub(project)
    docx_path, _ = write_docx(project)
    assert epub_chapters == 15 and epub_path.exists() and docx_path.exists()

    class _Scripted(Provider):
        name = "scripted"
        supports_tools = False

        def __init__(self):
            self.n = 0

        def complete(self, req):
            self.n += 1
            return CompletionResponse(text=f"draft {self.n}", stop_reason="end", usage=Usage())

    blurbs = run_blurbs(project, provider=_Scripted())
    assert len(blurbs.written) == 3

    backend = FakeTTS()
    generate_voices(project, backend)
    res = synthesize_chapter(project, 1, backend, {"narrator": "v1", "characters": {}})
    assert res.wav_path.exists()
    actions = [e.action for e in Ledger(project.root).tail(30)]
    assert "ship.audio" in actions
