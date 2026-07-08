<!--
tournament_judge.md — user prompt for one blind pairwise take comparison
(`tournament/judge.py:judge_pair`, called twice per pair with presentation
order swapped).

The judge sees two anonymized takes ("Take A" / "Take B" — full text, no
angle names, no slop scores) plus story context, and must return a forced
A/B verdict as STRICT JSON. No numeric scores anywhere: absolute LLM scoring
collapses into a narrow band; comparative judgments spread out
(docs/research/RESEARCH.md, milestone-1 finding). Elo standings are computed
by deterministic code from these verdicts.

Placeholders (substituted by `pipelines/common.py:render_prompt`; every one
is optional — missing keys render as ""):
  {project_name}    - StonerConfig.project_name
  {chapter_number}  - zero-padded chapter number under tournament
  {premise}         - canon/premise.md
  {beats}           - outline/beats/ch-NN.md for this chapter
  {taste_digest}    - advisory prior from the writer's blind votes
                      (tournament/taste.py:digest); "" until enough votes
  {take_a}          - full body text of the take presented first
  {take_b}          - full body text of the take presented second
-->

You are judging two competing drafts of chapter {chapter_number} of
"{project_name}". They are presented blind as Take A and Take B. Read both
in full, then pick the one that works better as a chapter of this book:
stronger scene logic, sharper voice, better movement through the beats,
fewer wasted lines.

{taste_digest}

## Story context

### Premise

{premise}

### Beat sheet for this chapter

{beats}

## Take A

{take_a}

## Take B

{take_b}

## Verdict

Respond with STRICT JSON only — no prose before or after, no code fences:

{"winner": "A", "steal": {"from": "B", "move": "<the losing take's single strongest specific move, named concretely, with a short quote>"}, "why": "<one sentence>"}

Rules:
- "winner" MUST be exactly "A" or "B". You must choose; there is no tie.
- "steal" names the ONE best specific move from the take that lost — a
  line, an image, a cut, a structural choice — concrete enough that an
  editor could graft it into the winner. Include a short quote.
- "why" is one sentence. No scores, no ratings, no grades of any kind.
