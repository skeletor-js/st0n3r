<!--
readers_bench.md — the batched blind pairwise-read prompt for benchmarking.

One completion asks k personas to read two chapter-length passages, A and B,
and pick which held them better. The pairing is blind: which passage is the
manuscript and which is the comp is withheld (the A/B mapping lives in run
state). Each persona returns a pick ("a" or "b") plus the loss points for each
passage — verbatim quotes where a text lost them — never a numeric score.

Placeholders: {chapter_number}, {text_a}, {text_b}, {readers_block}.
Rendered by pipelines/common.render_prompt.
-->
# Blind read — pair {chapter_number}

Two passages, A and B. Each reader reads both as themselves and picks the one
that held their attention better. You do not know which is which; judge only the
reading experience.

## Passage A

{text_a}

## Passage B

{text_b}

## The readers

{readers_block}

## What to return

For each reader: a "pick" of "a" or "b" (the passage that held them better), and
the loss points for each passage — short VERBATIM quotes where that passage lost
their attention. Do NOT give scores, ratings, or numbers; report the pick and
where each text lost them.

Respond with STRICT JSON only (a single fenced json block or raw JSON, no other
prose) matching exactly this shape, keyed by the reader ids shown above:

```json
{
  "readers": {
    "READER_ID": {
      "pick": "a",
      "loss_a": [{"quote": "verbatim from passage A", "note": "why it lost them"}],
      "loss_b": [{"quote": "verbatim from passage B", "note": "why it lost them"}],
      "note": "one line on the choice"
    }
  },
  "summary": "one line on where the roster split"
}
```
