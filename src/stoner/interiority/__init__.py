"""Character Interiority Agents: private per-character state the writer can't see.

Major characters become persistent agents with private state kept outside
`canon/` (so it never reaches the writer prompt): a timeline-bounded knowledge
ledger, stated-vs-real wants, fears, active lies, and refusals. This state
powers a machine-checkable knowledge-boundedness check, an advisory review
pass, and a budgeted scene-simulation loop where character agents -- each
seeing only its own sheet -- collide to produce dialogue with real information
asymmetry.

The privacy boundary is the heart of the feature: nothing here is imported by
writer-facing context assembly. Public API below is what the CLI, the write
pipeline, and the interiority review pass consume.
"""

from __future__ import annotations

from .boundedness import (
    BoundednessError,
    attribution_prompt,
    check_boundedness,
    parse_attributions,
)
from .curator import (
    CastApplyResult,
    CastConflict,
    CuratorError,
    apply_cast_update,
    cast_update_prompt,
    parse_cast_update,
)
from .pipeline import CastReport, run_cast_check, run_cast_update
from .scene import SceneError, SceneResult, SceneTurn, parse_scene_block, run_scene
from .sheet import CastSheet, KnowledgeEntry, Lie, Refusal, Wants
from .store import (
    CastError,
    CastStore,
    knowledge_asof,
    private_digest,
    review_digest,
)

__all__ = [
    "BoundednessError",
    "CastApplyResult",
    "CastConflict",
    "CastError",
    "CastReport",
    "CastSheet",
    "CastStore",
    "CuratorError",
    "KnowledgeEntry",
    "Lie",
    "Refusal",
    "SceneError",
    "SceneResult",
    "SceneTurn",
    "Wants",
    "apply_cast_update",
    "attribution_prompt",
    "cast_update_prompt",
    "check_boundedness",
    "knowledge_asof",
    "parse_attributions",
    "parse_cast_update",
    "parse_scene_block",
    "private_digest",
    "review_digest",
    "run_cast_check",
    "run_cast_update",
    "run_scene",
]
