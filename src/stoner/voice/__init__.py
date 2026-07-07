"""Voice engine: a learned stylometric fingerprint plus 0-100 drift scoring.

The slop detector's sibling (see :mod:`stoner.slop`): instead of comparing
prose against a fixed lexicon of AI tells, it compares prose against a
deterministic fingerprint of *this* writer's measured voice. No LLM anywhere
in fingerprinting or drift scoring.

Public API:

- :func:`learn_fingerprint` / :func:`load_fingerprint` /
  :func:`save_fingerprint` -- build and persist
  ``.stoner/voice/fingerprint.json``.
- :func:`run_voice` -- score one document, return a
  :class:`~stoner.types.VoiceReport`.
- :func:`voice_gate_check` -- deterministic gate helper for the write
  pipeline (returns ``(None, False)`` when the gate is off or no
  fingerprint exists).
"""

from __future__ import annotations

from .drift import run_voice, voice_context_digest, voice_gate_check
from .features import extract_features, load_function_words, segment_text
from .fingerprint import (
    Fingerprint,
    FingerprintError,
    learn_fingerprint,
    load_fingerprint,
    render_digest,
    save_fingerprint,
)

__all__ = [
    "Fingerprint",
    "FingerprintError",
    "extract_features",
    "learn_fingerprint",
    "load_fingerprint",
    "load_function_words",
    "render_digest",
    "run_voice",
    "save_fingerprint",
    "segment_text",
    "voice_context_digest",
    "voice_gate_check",
]
