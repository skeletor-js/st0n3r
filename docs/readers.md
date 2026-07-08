# Reader simulation

`stoner readers` runs a roster of reader personas over the finished manuscript and reports where enough of them independently react — a synthetic focus group. It also benchmarks the book blind against a public-domain comp, chapter-aligned. Like the [review passes](review.md), everything it produces is advisory: it surfaces where readers disagree, it never gates.

```bash
stoner readers personas             # available personas (shipped + project), roster marked
stoner readers run --chapters 1-5   # the roster reads chs 1-5, then builds the heatmap
stoner readers heatmap              # render the latest run's attention heatmap + trouble segments
stoner readers comps add ~/pd/middlemarch.txt --author "George Eliot" --year 1872
stoner readers bench middlemarch --chapters 1-3   # blind pairwise vs. the comp
```

## The roster

Personas are reader archetypes with different tastes and attention. `stoner readers personas` lists the shipped set plus any the project defines, marking which are on the active roster. By default the harness picks a deterministic roster of `roster_size` (default 12) personas that maximizes attribute spread; name explicit personas in `readers.roster` to fix the panel.

The cost lever is `personas_per_call` (default 4): batching k personas per completion turns the call count from personas × chapters into ceil(personas/k) × chapters. A run stops cleanly at `max_calls_per_run` (default 150) and `--resume` continues it; `--max-calls` overrides the cap for one run.

## Runs and the heatmap

`stoner readers run` has the roster read the manuscript (or `--chapters 1-5` / `1,3,7`) and record where each persona's attention flags, where it snags, where it re-engages. `stoner readers heatmap` renders that into an attention heatmap and a list of **trouble segments** — spans where the roster's attention collectively dropped.

A negative segment becomes an advisory `Finding` — and is mirrored into `.stoner/reviews/` tagged `kind: "readers"`, so it triages in the [UI](ui.md) alongside every other report — when at least `agreement_threshold` of the roster (default 0.5) agrees on it. One bored reader is noise; half the panel losing the thread in the same place is signal. That agreement rule is the same principle behind the [`panel` review pass](review.md#the-eight-passes): only promote what multiple independent readers raise.

## Benchmarking against a comp

`stoner readers bench <comp>` puts your manuscript up against a comparison text in **blind, chapter-aligned pairwise** judgments — the personas compare your chapter to the comp's without knowing which is which — and scores the result with the same Elo rating math the [draft tournaments](tournaments.md) use, so a win rate becomes a rating you can read across runs.

Comps are supplied by you and must be **public domain** (or otherwise yours to use): `stoner readers comps add <local-file>` ingests a local text and splits it into chapters — there is no fetch-from-URL, and no comp text ships in the package. Record `--author`, `--year`, and `--source` for provenance. See [Credits](CREDITS.md#reader-simulation-comps-public-domain-only) for the attribution convention and the licensing responsibility, which is yours.

## Configuration

```yaml
readers:
  roster: []                 # explicit persona ids; empty = deterministic spread of roster_size
  roster_size: 12
  personas_per_call: 4       # cost lever: personas batched per completion
  max_calls_per_run: 150     # hard budget cap a run stops cleanly at
  agreement_threshold: 0.5   # fraction of the roster that must agree to raise a finding
```

Reader reactions resolve against the `reader` role (a cheap, high-volume model by default — `stoner init` sets a Haiku-class model). Set `models.reader` to change it.
