<!--
ship_attribution.md — user prompt for `stoner ship audio --assist` / script
generation. Filled by ship/dialogue.py via render_prompt. Placeholders:
  {characters} - the character slugs available to attribute to
  {lines}      - numbered ambiguous dialogue lines, each with local context

Advisory only: results are marked `attribution: llm` in the script and never
gate synthesis. Any line the model cannot confidently place must stay UNKNOWN.
-->

Some quoted dialogue lines from a novel chapter could not be attributed to a
speaker deterministically. Assign each to the character most likely speaking
it, using the surrounding narration as your only evidence.

## Characters (use these slugs exactly)
{characters}

## Ambiguous lines
{lines}

Respond with STRICT JSON only (a single fenced ```json block or raw JSON, no
other prose) in exactly this shape:

{"attributions": {"0": "<slug or UNKNOWN>", "1": "<slug or UNKNOWN>"}}

Keys are the line numbers above. Values must be a slug from the list or the
literal "UNKNOWN". Do not invent slugs. If a line is genuinely unclear, return
"UNKNOWN" for it rather than guessing.
