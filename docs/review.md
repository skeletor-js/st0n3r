# Review & revise

`stoner review` runs LLM critic passes over a chapter and saves the findings; `stoner revise` applies the findings you accept. This is the judgment half of the two immune systems — the deterministic half is [the slop detector](slop.md), and the reasoning for keeping them separate is in [Concepts](concepts.md#two-immune-systems).

```bash
stoner review 3                            # default passes from stoner.yaml
stoner review 3 --passes adversarial,grade # pick specific passes
stoner review 3 --model openai/gpt-5.2     # override the reviewer model
```

Every pass sees the same context: the full chapter, the canon digest, rolling memory, your style guide, and the tail of the previous chapter — mirroring what the writer saw, so critiques are grounded in the same facts.

## The eight passes

| Pass | What it catches | When to run it |
|---|---|---|
| `continuity` | facts contradicting canon, timeline slips, characters knowing things they shouldn't | every chapter (default) |
| `pacing` | summary where scene is needed, stalled or rushed momentum, repeated chapter-ending shapes | every chapter (default) |
| `voice` | style-guide violations, characters who all sound alike, "not X, but Y" dialogue | every chapter (default) |
| `line` | sentence-level tells: over-explaining, triadic listing, simile crutches, filter words, too-smooth dialogue | every chapter (default) |
| `logic` | plot holes, unearned turns, motivation gaps | after plot-heavy chapters; before locking an act |
| `adversarial` | a forced cut of exactly 400 words, every cut classified (OVER-EXPLAIN, REDUNDANT, THROAT-CLEARING, WEAK-BEAT) | when a chapter feels bloated but you can't see where |
| `panel` | four reader personas (acquisitions editor, genre reader, rival novelist, first-time reader); only issues 3+ raise independently become major findings | before calling a chapter done; milestone checks |
| `grade` | every paragraph labeled STRONG / FINE / WEAK / CUT, plus the distribution | revision planning — it shows you the weak paragraphs on a map |

The default set (`continuity`, `pacing`, `voice`, `line`) is configurable via `review_passes:` in `stoner.yaml`. `adversarial`, `panel`, and `grade` are comparative by design — see [Concepts](concepts.md#comparative-grading-over-absolute-scores) for why they never emit numeric ratings.

A pass that fails (provider error, unparseable response) degrades into a single info-severity finding noting the failure; the other passes' results are kept.

## Report files

Each run writes a pair to `.stoner/reviews/`:

```
ch-03-1783437321.json   # structured: findings with severity, quote, span, status
ch-03-1783437321.md     # human-readable rendering of the same
```

Findings quote the chapter verbatim, and the harness locates each quote to a line/character span where it can — that's what lets the [UI](ui.md) highlight them in the text. Reports also record token usage per run. Slop reports saved with `stoner slop --save` land in the same directory, as do whole-manuscript reports (`book-<ts>.json`/`.md`) from `stoner review-book` — the single cross-chapter pass that [autonomous mode](autonomous.md#4-stoner-review-book--one-whole-manuscript-pass) also runs between drafting batches.

## The revise flow

Every finding has a status: `open` (fresh), `accepted`, `dismissed`, or `fixed`. Revision applies **accepted** findings only:

1. **Triage.** Open `stoner ui`, go to the report, and Accept or Dismiss each finding. This is the step worth doing slowly — you are the editor; the model just drafts the fix.
2. **Apply.**

```
$ stoner revise 3
revised ch-03: 2,101 -> 2,043 words, 6 finding(s) applied
Tightened the tavern scene per the adversarial cuts; removed filter
words in the opening; fixed Bren's eye color to match canon.
```

The model rewrites the whole chapter with instructions to change nothing beyond what the findings require. Frontmatter survives; `status` bumps to `revised`.

Options:

- `--all` skips triage and applies every open finding along with the accepted ones. Fine for slop-style mechanical findings; risky for judgment calls you haven't read.
- `--report ch-03-1783437321.json` targets a specific report; by default the latest one for that chapter is used.
- `--model` overrides the reviewer model for the rewrite.

**Safety behavior:** if the model's revision comes back empty or suspiciously short (under a quarter of the original length — the signature of a truncated response), `revise` refuses to overwrite and the chapter is left untouched. The fix is usually raising `max_tokens` in `stoner.yaml` so a full chapter fits in one response, then retrying.

Since chapters are plain files under your version control, `git diff manuscript/ch-03.md` after a revise is the fastest honest review of what actually changed.

## Archivist conflicts and how to resolve them

The archivist (the third stage of `stoner write`, or standalone `stoner archive N`) extracts hard facts from a chapter and diffs them against canon frontmatter. Three outcomes:

- **New fact** — canon had no value for that field. Applied automatically (with `--auto`, or during the write pipeline).
- **Confirmation** — matches canon. Nothing to do.
- **Conflict** — canon says one thing, the chapter says another:

```
conflict Mara Quill.eyes: canon='gray' new='green' — resolve by hand
```

Conflicts are **never** auto-applied, whatever flags you pass. Severity signals how much a contradiction matters if left alone: `status` (alive/dead) is critical; `age`, `eyes`, `hair`, `build`, `role`, `type` are major; anything else minor. Ages get one year of tolerance, so a birthday isn't a contradiction.

To resolve one, decide which side is true, then either:

- **Canon is right:** fix the prose (a review `continuity` pass followed by `revise` will do it, or edit by hand), or
- **The chapter is right:** update the field in `canon/characters/<slug>.md` yourself.

Then rerun `stoner archive N --auto` — with the contradiction gone, remaining facts apply cleanly. Without `--auto`, `archive` is a pure preview and touches nothing, which makes it a safe way to see what a chapter "claims" before committing it to the bible.
