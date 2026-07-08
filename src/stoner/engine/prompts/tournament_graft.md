<!--
tournament_graft.md — user prompt for the steal-folding step
(`tournament/graft.py:graft_winner`, one plain completion after a human
confirms a tournament winner with `stoner tournament apply`).

The model gets the winning take's full body plus the named "steals" the
judge collected from losing takes (each a specific move with a short
quote), and must return the complete grafted chapter between
BEGIN CHAPTER / END CHAPTER sentinels — parsed and guarded exactly like
`review/revise.py` (empty or under 1/4 of the original word count refuses
the graft; the raw winner is applied instead). The original is never
destroyed.

Placeholders (substituted by `pipelines/common.py:render_prompt`; every one
is optional — missing keys render as ""):
  {project_name}    - StonerConfig.project_name
  {chapter_number}  - zero-padded chapter number
  {winner_body}     - full body text of the confirmed winning take
  {steals}          - bulleted named moves from the losing takes
  {style_guide}     - canon/style.md excerpt (banned-terms section removed)
-->

You are grafting the best moves from losing drafts onto the winning draft
of chapter {chapter_number} of "{project_name}". The winner earned its
place; your job is surgical: work each listed steal into the winner where
it strengthens the chapter, and change nothing else. Preserve the winner's
voice, structure, and length. If a steal cannot be worked in without
damaging the chapter, skip it.

## Style guide

{style_guide}

## Winning draft (original, full text)

{winner_body}

## Steals to graft (each is one named move from a losing draft)

{steals}

## Task

Rewrite the ENTIRE chapter with the steals grafted in. Respond with ONLY
the following, in exactly this format (no other prose):

SUMMARY: <one paragraph naming which steals you grafted and where>
BEGIN CHAPTER
<the complete grafted chapter body>
END CHAPTER
