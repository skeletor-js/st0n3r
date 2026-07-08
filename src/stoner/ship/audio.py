"""Table-read synthesis: chunk -> per-chunk WAV cache -> stdlib `wave` stitch.

Narration and dialogue lines are chunked at sentence boundaries (API input
caps), each chunk synthesized to a cache file keyed by
hash(backend, voice, text) so an interrupted or re-run chapter only
synthesizes what is missing (R16). Chunks stitch with the stdlib `wave`
module; a params mismatch raises naming the offending chunk. `--dialogue-only`
filters to dialogue segments; `--announce` prepends a narrator-voiced name at
each speaker change. MP3 conversion happens only when `ffmpeg` is on PATH
(R17); otherwise the WAV stands with a note.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..ledger import Ledger
from ..project import WritingProject
from .dialogue import (
    NARRATOR,
    UNKNOWN,
    Script,
    build_script,
    count_dialogue_lines,
)
from .tts import TTSBackend, TTSError

_MAX_CHUNK_CHARS = 280
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

VOICES_REL = "export/audio/voices.yaml"


@dataclass
class ChapterAudioResult:
    wav_path: Path
    chapter: int
    chunk_count: int = 0
    cache_hits: int = 0
    synthesized: int = 0
    dialogue_only: bool = False
    mp3_path: Path | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Split at sentence boundaries, then greedily pack into <= max_chars."""
    text = text.strip()
    if not text:
        return []
    sentences = _SENTENCE_RE.split(text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    # A single over-long sentence still becomes one chunk (backend clamps).
    return chunks


def _speech_items(
    script: Script,
    voice_map: dict,
    dialogue_only: bool,
    announce: bool,
) -> list[tuple[str, str]]:
    """Ordered (voice, text) items for a chapter, honoring dialogue-only and
    speaker announcements."""
    narrator_voice = voice_map.get("narrator", "")
    chars = voice_map.get("characters", {})
    items: list[tuple[str, str]] = []
    last_speaker: str | None = None
    for seg in script.segments:
        if seg.kind == "narration":
            if dialogue_only:
                continue
            items.append((narrator_voice, seg.text))
            continue
        speaker = seg.speaker
        if speaker in (UNKNOWN, NARRATOR):
            voice = narrator_voice
        else:
            voice = chars.get(speaker, narrator_voice)
        if announce and speaker != UNKNOWN and speaker != last_speaker:
            name = speaker.replace("-", " ").title()
            items.append((narrator_voice, f"{name}."))
        items.append((voice, seg.text))
        last_speaker = speaker
    return items


# ---------------------------------------------------------------------------
# Cache + synthesis
# ---------------------------------------------------------------------------


def _cache_key(backend_name: str, voice: str, text: str) -> str:
    seed = f"{backend_name}\x00{voice}\x00{text}".encode()
    return hashlib.sha256(seed).hexdigest()[:24]


def _stitch(chunk_paths: list[Path], dest: Path, sample_rate: int) -> None:
    """Concatenate mono PCM WAV chunks; raise on any params mismatch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not chunk_paths:
        with wave.open(str(dest), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(sample_rate)
            out.writeframes(b"")
        return
    params = None
    frames: list[bytes] = []
    for path in chunk_paths:
        with wave.open(str(path), "rb") as w:
            these = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = these
            elif these != params:
                raise TTSError(
                    f"chunk {path.name} has WAV params {these} but the chapter "
                    f"started with {params}; cannot stitch mixed formats."
                )
            frames.append(w.readframes(w.getnframes()))
    assert params is not None  # non-empty chunk_paths guarantees this
    nch, sw, fr = params
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(nch)
        out.setsampwidth(sw)
        out.setframerate(fr)
        out.writeframes(b"".join(frames))


def synthesize_chapter(
    project: WritingProject,
    chapter: int,
    backend: TTSBackend,
    voice_map: dict,
    dialogue_only: bool = False,
    announce: bool = False,
    max_chars: int = _MAX_CHUNK_CHARS,
) -> ChapterAudioResult:
    """Synthesize (with cache) and stitch one chapter to export/audio/ch-NN.wav.
    Ledgers `ship.audio`."""
    script = build_script(project, chapter)
    items = _speech_items(script, voice_map, dialogue_only, announce)
    cache_dir = project.resolve("export/audio/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    chunk_paths: list[Path] = []
    hits = 0
    made = 0
    for voice, text in items:
        for chunk in chunk_text(text, max_chars):
            key = _cache_key(backend.name, voice, chunk)
            path = cache_dir / f"{key}.wav"
            if path.exists():
                hits += 1
            else:
                backend.synthesize(chunk, voice, path)
                made += 1
            chunk_paths.append(path)

    dest = project.resolve(f"export/audio/ch-{chapter:02d}.wav")
    _stitch(chunk_paths, dest, backend.sample_rate)

    result = ChapterAudioResult(
        wav_path=dest,
        chapter=chapter,
        chunk_count=len(chunk_paths),
        cache_hits=hits,
        synthesized=made,
        dialogue_only=dialogue_only,
    )
    mp3 = _maybe_mp3(dest)
    if mp3 is not None:
        result.mp3_path = mp3
    else:
        result.notes.append("ffmpeg not found on PATH — kept WAV, skipped MP3.")

    Ledger(project.root).append(
        "ship.audio",
        target=str(dest.relative_to(project.root)),
        chapter=chapter,
        backend=backend.name,
        is_network=backend.is_network,
        chunks=len(chunk_paths),
        cache_hits=hits,
        dialogue_only=dialogue_only,
    )
    return result


def _maybe_mp3(wav_path: Path) -> Path | None:
    """Convert to MP3 with ffmpeg when available; else return None."""
    if not shutil.which("ffmpeg"):
        return None
    mp3_path = wav_path.with_suffix(".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), str(mp3_path)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return mp3_path


# ---------------------------------------------------------------------------
# Voice map
# ---------------------------------------------------------------------------


def load_voice_map(project: WritingProject) -> dict | None:
    path = project.resolve(VOICES_REL)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def generate_voices(project: WritingProject, backend: TTSBackend) -> Path:
    """Write export/audio/voices.yaml (rank by line count, round-robin backend
    voices, preserve existing assignments). Ledgers `ship.voices`."""
    from .dialogue import build_voice_map

    chapters = [c.number for c in project.chapters()]
    counts = count_dialogue_lines(project, chapters)
    existing = load_voice_map(project)
    voices = backend.list_voices()
    vmap = build_voice_map(
        counts,
        voices,
        narrator_voice=project.config.ship.audio.narrator_voice,
        existing=existing,
    )
    path = project.resolve(VOICES_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(vmap, sort_keys=False, allow_unicode=True), encoding="utf-8")
    Ledger(project.root).append("ship.voices", target=VOICES_REL, voices=len(vmap.get("characters", {})))
    return path
