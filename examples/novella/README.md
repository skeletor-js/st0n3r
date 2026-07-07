# Sungrown

A complete 15-chapter, ~25,000-word novella generated end-to-end by st0n3r as
its proof of output — from a one-paragraph seed to a reviewed, revised
manuscript, with no human edits to any generated file.

**Ruth Vann, 61, has grown cannabis in the hills of Humboldt County for forty
years. Legalization finally arrives — and threatens to take from her the one
thing prohibition never could: the meaning of her work.** The antagonist is
paperwork, time, and the market. The register aims at John Williams' *Stoner*:
restrained, interior, devoted to the dignity of labor.

## How it was made

```bash
stoner init novella
stoner brainstorm "A quiet literary novella about Ruth Vann, 61, ..."  # -> premise, style
stoner foundation --characters 4 --world 3   # -> characters, world, threads, 15-ch outline + beats
stoner book --review-every 4                 # -> the manuscript
```

Models (via the `claude` Claude-Code-CLI provider): writer/reviewer
`claude-sonnet-5`, archivist `claude-haiku-4-5`.

## What the run did

- 15 chapters drafted, each through the slop gate (scores 1.7–20.3, all under
  the 25.0 threshold; the flagged tics were auto-revised where the gate
  demanded it)
- 6 whole-manuscript review rounds across the run, revising 12 chapters
  against critic findings; ended with **0 critical / 5 major** findings
  remaining (plateau stop — the harness reports what's left rather than
  pretending it's done)
- the archivist kept `canon/` in sync throughout: character sheets, world
  entries, timeline rows, and thread statuses in this directory were all
  written by it
- every action is in `.stoner/ledger.jsonl`; review reports in
  `.stoner/reviews/`; resumable run state in `.stoner/book-state.json`
  (session transcripts are gitignored for size)

Read `manuscript/ch-01.md` for the cold open and `manuscript/ch-15.md` for the
close, then poke through `canon/` to see what the archivist learned along the
way.
