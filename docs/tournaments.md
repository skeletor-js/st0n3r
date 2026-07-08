# Draft tournaments

Ask a model to rate a chapter and everything comes back a 7 ([absolute scores collapse](concepts.md#comparative-grading-over-absolute-scores)). Ask it which of two takes is better and you get a usable answer. `stoner tournament` is that idea applied to drafting: write N takes of a chapter from distinct angles, judge them in blind pairwise comparisons, and propose a winner for a human to confirm.

```bash
stoner tournament run 3 --takes 4        # draft 4 angled takes of ch-03, judge blind, propose a winner
stoner tournament list                    # tournaments in this project
stoner tournament status <id>             # standings, steals, and budget for one run
stoner tournament vote <id>               # blind A/B: read two takes, pick one, then see the verdict
stoner tournament apply <id>              # write the confirmed winner into the manuscript (the human step)
```

## How a run works

`stoner tournament run <chapter>` drafts `--takes` takes (default from `tournament.takes`, or `tournament.slot_takes` for the opening and ending chapters), each from a distinct angle — the built-in presets plus any you add under `tournament.angles`. It then judges them **blind and pairwise** — the judge sees two takes with no angle labels and picks the stronger — up to `--max-comparisons` judge calls (`tournament.max_comparisons`, default 24), and proposes a winner. Drafting uses the writer role; judging uses the reviewer role.

Runs are budgeted and resumable: `--max-comparisons` caps judge calls, `tournament.max_tokens_budget` caps total tokens, and a crashed run resumes with `tournament run <chapter> --resume <id>`. Every take is snapshotted through [draft archaeology](drafts.md), so nothing a tournament drafts is lost. `--force` lets you run over a chapter that's already been revised or finalized.

## Confirming a winner

The winner is a *proposal*, not a commit — nothing lands in the manuscript until a human says so. Two ways to decide:

- **`stoner tournament apply <id>`** writes the proposed winner (or `--take N` for a specific one) into `manuscript/`. With `--graft` (the default) it folds the best *steals* from the losing takes into the winner — a strong line or beat that lost the overall comparison still earns its way in — via one model call; `--no-graft` applies the winner verbatim, no model. `--force` applies even if the chapter changed since the run; `-y` skips the confirmation prompt.
- **`stoner tournament vote <id>`** is blind A/B for a human: you read two takes, pick one, and only then see their angles and the judge's verdict. Votes **train the project's taste model** (`.stoner/taste/`) — they teach the harness your preferences over time; they do not themselves apply a chapter.

## In the write pipeline

Two opt-in hooks wire tournaments into drafting:

- **`stoner write N --tournament K`** runs a K-take tournament as the drafting step, *before* the slop gate. Passing `--tournament` is explicit consent to auto-apply the proposed winner — it folds the human confirmation into the flag, so the run stays hands-off. The winner then goes through the normal slop gate and archivist.
- **`stoner book --tournaments`** drafts the opening and ending slot chapters via a per-slot tournament (sized by `tournament.slot_takes`); middle chapters draft once. This spends the tournament budget where structure matters most — the chapters a reader remembers.

Both are off unless you ask for them. See [Autonomous mode](autonomous.md).

## Configuration

```yaml
tournament:
  takes: 3                          # default field size
  slot_takes: {opening: 5, ending: 5}  # override for ch-01 and the last planned chapter
  angles: []                        # extra drafting angles: [{name, instruction}, ...]
  max_comparisons: 24               # judge-call budget per run
  max_tokens_budget: 500000         # total-token budget per run
  graft: true                       # fold losing takes' steals into the winner at apply time
```

Takes draft against the `writer` role (overridable with `--model`); judging uses `reviewer`.
