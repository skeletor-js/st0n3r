"""Text-to-speech backends for the table read: a ship-local protocol + two
adapters, no vendor SDK (invariant 7).

A backend declares `name`, `is_network`, and a `sample_rate`, lists its voices,
and synthesizes one line to a mono 16-bit PCM WAV. The registry mirrors
`providers/registry.py`: `get_backend(name, config)` looks a backend up and
raises an actionable error rather than silently degrading.

`say` (macOS): a subprocess adapter, zero-dependency local default, available
only when `shutil.which("say")` finds it. `openai`: a plain httpx POST to
`/v1/audio/speech` (`gpt-4o-mini-tts`), no `openai` SDK import -- it is not a
text-completion Provider so it does not belong in `providers/`. Network
synthesis is explicit opt-in only (never a silent fallback); a missing
`OPENAI_API_KEY` errors by name and the key value is never printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config import StonerConfig

# The 13 voices the OpenAI speech endpoint accepts, as of the model below.
_OPENAI_VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar",
]


class TTSError(RuntimeError):
    """Raised for backend selection/config/synthesis failures (actionable)."""


class TTSBackend(ABC):
    """A speech backend. `synthesize` must write a mono 16-bit PCM WAV."""

    name: str = "base"
    is_network: bool = False
    sample_rate: int = 22050

    @abstractmethod
    def list_voices(self) -> list[str]:
        ...

    @abstractmethod
    def synthesize(self, text: str, voice: str, dest_wav: Path) -> None:
        ...


# ---------------------------------------------------------------------------
# say (macOS, local, subprocess)
# ---------------------------------------------------------------------------


class SayBackend(TTSBackend):
    name = "say"
    is_network = False
    sample_rate = 22050

    def list_voices(self) -> list[str]:
        try:
            out = subprocess.run(
                ["say", "-v", "?"], capture_output=True, text=True, check=True
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
        voices: list[str] = []
        for line in out.splitlines():
            # "Alex                en_US    # comment" -> first token is the voice.
            token = line.split("  ", 1)[0].strip()
            if token:
                voices.append(token)
        return voices

    def synthesize(self, text: str, voice: str, dest_wav: Path) -> None:
        dest_wav.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["say", "-o", str(dest_wav), "--file-format=WAVE",
               f"--data-format=LEI16@{self.sample_rate}"]
        if voice:
            cmd[1:1] = ["-v", voice]
        try:
            subprocess.run([*cmd, text], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise TTSError(f"`say` synthesis failed: {e}") from e


# ---------------------------------------------------------------------------
# openai (network, plain httpx, no SDK)
# ---------------------------------------------------------------------------


class OpenAIBackend(TTSBackend):
    name = "openai"
    is_network = True
    sample_rate = 24000  # the endpoint's wav output rate

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-tts", client: Any | None = None):
        # The key is stored to send in a header; it is never logged or printed.
        self._api_key = api_key
        self._model = model
        self._client: Any = client

    def list_voices(self) -> list[str]:
        return list(_OPENAI_VOICES)

    def synthesize(self, text: str, voice: str, dest_wav: Path) -> None:
        import httpx

        client = self._client or httpx.Client(timeout=60.0)
        try:
            resp = client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "voice": voice or "alloy",
                    "input": text,
                    "response_format": "wav",
                },
            )
            if resp.status_code != 200:
                raise TTSError(
                    f"OpenAI speech request failed with status {resp.status_code}"
                )
            dest_wav.parent.mkdir(parents=True, exist_ok=True)
            dest_wav.write_bytes(resp.content)
        finally:
            if self._client is None:
                client.close()


# ---------------------------------------------------------------------------
# Registry (mirrors providers/registry.py shape)
# ---------------------------------------------------------------------------

BUILTIN_BACKENDS = ("say", "openai")


def get_backend(name: str, config: StonerConfig) -> TTSBackend:
    """Return a ready backend by name, or raise TTSError with a clear message.

    Selection is explicit: the network `openai` backend is only ever returned
    when the caller names it, never as a fallback from a failed local backend.
    """
    name = (name or "").lower()
    if name == "say":
        if not shutil.which("say"):
            raise TTSError(
                "the `say` backend needs macOS's `say` on PATH, which was not "
                "found. Install a different backend or set ship.audio.backend."
            )
        return SayBackend()
    if name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise TTSError(
                "the `openai` TTS backend needs the OPENAI_API_KEY environment "
                "variable, which is not set."
            )
        return OpenAIBackend(api_key=api_key, model=config.ship.audio.model)
    raise TTSError(
        f"unknown TTS backend {name!r}. Known: {', '.join(BUILTIN_BACKENDS)}."
    )
