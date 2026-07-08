# Pacing instruments

`stoner pacing report` reads the whole book as a shape. Where [review](review.md) works a chapter at a time, pacing is book-level: scene-versus-summary balance, a tension curve across chapters, flatline stretches, and POV whiplash. It mixes deterministic structural measures with optional per-chapter LLM instruments, and it is **advisory only** — it never gates, on either kind of input. (The one deterministic gate in the harness stays the [slop detector](slop.md); pacing informs, it does not refuse.)

```bash
stoner pacing report                 # deterministic measures + LLM instruments (default)
stoner pacing report --no-llm        # deterministic only — free and offline
stoner pacing report --format json   # rich | markdown | json
stoner pacing report --no-save       # don't write a report to .stoner/reviews/
```

## The two layers

**Deterministic structural measures** run over every chapter with no model and no cost:

- **scene vs. summary** — the in-scene ratio per chapter; a book that drops below `in_scene_min_ratio` (default 0.70) is leaning on summary where it should be dramatizing.
- **ending echoes** — runs of chapters that end on the same shape (`ending_echo_min_run`, default 3); three chapters closing the same way is a tic a reader feels.
- **flatlines** — runs of chapters with no change in tension (`flatline_min_run`, default 3): a stretch where nothing escalates.
- **POV whiplash** — runs of consecutive POV breaks (`pov_break_min_run`, default 4).

**LLM instruments** add per-chapter judgments — tension level, whether the dramatic question changes hands, and beat drift against the outline. These call the reviewer model, so they are what `--no-llm` turns off; the default is on (`pacing.llm_instruments: true`). `--model` overrides the judge model for a run.

## Reading the report

The report prints the tension curve and the flagged runs, worst signals first. A single low-tension chapter is rarely worth acting on; a *run* of them is the point — pacing is about shape over distance, not any one chapter's number. Findings save to `.stoner/reviews/` tagged `kind: "pacing"` (unless `--no-save`), so the report surfaces in the [UI](ui.md) Pacing panel as a timeline.

## Configuration

```yaml
pacing:
  in_scene_min_ratio: 0.70   # flag chapters below this scene-vs-summary ratio
  ending_echo_min_run: 3     # flag this many chapters ending on the same shape
  flatline_min_run: 3        # flag this many chapters with no tension change
  pov_break_min_run: 4       # flag this many consecutive POV breaks
  llm_instruments: true      # run the per-chapter LLM judge (the --llm/--no-llm default)
```

The LLM instruments resolve against the `reviewer` role.
