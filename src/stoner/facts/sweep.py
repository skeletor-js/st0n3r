"""The `verisimilitude` review pass: check a chapter against the fact locker.

An advisory pass (invariant 2 -- never gates). It feeds the chapter plus the
locker's facts digest to the model and asks for standard-shape findings in
exactly two categories:

- ``contradiction`` (severity major): the chapter asserts something a locker
  fact refutes -- quote the chapter, name the fact slug in the issue.
- ``check-this`` (severity minor): the chapter states a confident real-world
  specific (date, fee, statute, brand, named form, procedure) that no locker
  fact covers -- the issue is phrased as a verification task, not a verdict.

The pass reuses `review.passes`'s `_std_user_prompt` assembly and standard
parser, so `locate_span`, Finding status flow, saved reports, and the UI need
nothing new. Registration is additive (`PASSES["verisimilitude"]`, wired from
the bottom of `review/passes.py`); it is deliberately NOT in the default
`review_passes` list -- the pass is only meaningful with a populated locker,
and its cost should always be a deliberate choice.

`review.passes` is imported lazily inside the functions below (not at module
top) so the bottom-of-`passes.py` registration is import-cycle-safe in both
import orders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..review.passes import PassContext, ReviewPass

_SYSTEM = (
    "You are a verisimilitude editor. You hold fiction to real-world truth: "
    "you cross-check a chapter's confident specifics -- dates, fees, statutes, "
    "brands, named forms, procedures -- against a locker of sourced facts, and "
    "you flag both outright contradictions and confident specifics that no one "
    "ever verified. You never invent facts; you point at what to check."
)

_TASK = (
    "Check this chapter for verisimilitude against the sourced fact locker "
    "below. Return findings in exactly two categories:\n"
    "- \"contradiction\": the chapter asserts a real-world specific that a "
    "locker fact refutes. Severity major. Quote the offending chapter text and "
    "name the locker fact's slug (in [brackets], as shown in the locker) in the "
    "issue.\n"
    "- \"check-this\": the chapter states a confident real-world specific -- a "
    "date, fee, statute, brand, named form, or procedure -- that no locker fact "
    "covers. Severity minor. Phrase the issue as a verification task ('verify "
    "that ...'), not a claim of error.\n"
    "Do NOT flag fiction-internal inventions established in the canon above "
    "(invented systems, places, or characters) as unsourced -- only real-world "
    "specifics the story presents as true."
)


def _sweep_prompt(ctx: PassContext) -> tuple[str, str]:
    from ..review.passes import _std_user_prompt

    task = _TASK
    if ctx.facts_digest:
        task += "\n\n## Fact locker\n\n" + ctx.facts_digest
    else:
        task += (
            "\n\n## Fact locker\n\nThe locker is empty: treat every confident "
            "real-world specific in the chapter as unsourced -- every finding is "
            "a check-this."
        )
    return _SYSTEM, _std_user_prompt(ctx, task)


def build_verisimilitude_pass() -> ReviewPass:
    """Construct the ReviewPass. Called once from `review/passes.py`."""
    from ..review.passes import ReviewPass, _make_standard_parser

    return ReviewPass(
        name="verisimilitude",
        description=(
            "Check a chapter against the sourced fact locker: contradictions "
            "(major) and confident unsourced real-world specifics (check-this, minor)."
        ),
        build_prompt=_sweep_prompt,
        parse=_make_standard_parser("verisimilitude"),
    )
