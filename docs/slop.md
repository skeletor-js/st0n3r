# The slop detector

`stoner slop` is a deterministic analyzer for AI-flavored prose. No model calls, no cost, same answer every time — which is why it can act as a hard gate in the [write pipeline](concepts.md#the-write-pipeline) while the LLM [review passes](review.md) stay advisory.

```bash
stoner slop 2            # one chapter
stoner slop all          # every chapter
stoner slop notes/draft.md   # any file path (run from inside a project)
stoner slop 2 --fmt json --save   # machine-readable + saved to .stoner/reviews/
```

## What each analyzer detects

Seven analyzers run over the chapter body (YAML frontmatter stripped, fenced code blocks masked out):

| Analyzer | Catches | Real examples it flags |
|---|---|---|
| **lexicon** | single-word LLM tells, severity-tiered | *delve*, *tapestry*, *testament* (critical); *myriad*, *palpable*, *plethora* (major); *undeniably* (minor) |
| **phrases** | stock multi-word clichés | "couldn't help but", "a testament to", "the air was thick with", "washed over her", "little did she know" |
| **patterns** | named regex constructions | "not just X, but Y"; "let out a breath she didn't know she'd been holding"; body-part autonomy ("her eyes found", "of their own accord"); "heart pounding in his chest" |
| **punctuation** | rate-based habits per 1,000 words | em-dash rate above 6/1000, semicolons above 4, ellipses and exclamation marks above 3 |
| **repetition** | reuse the writer didn't notice | a 3- or 4-gram appearing 3+ times; a word echoed 3+ times within 40 words; 3+ consecutive sentences starting with the same word |
| **rhythm** | uniformity ("low burstiness") | sentence lengths with variation coefficient under 0.35; runs of 4+ near-equal-length sentences; uniform paragraph lengths |
| **density** | crutch-word saturation | -ly adverb rate over 25/1000; filter words (*felt, saw, seemed, noticed, realized*) over 6/1000; "X, Y, and Z" triplet overuse; stacked adjectives |

The lexicons live in YAML inside the package (`words.yaml`, `phrases.yaml`, `patterns.yaml` — about 900 entries) with a severity and often a note on each entry.

## Reading a report

```
$ stoner slop 2
Slop report: manuscript/ch-02.md
Score: 47.2 / 100 -- verdict: slop-adjacent
┃ Analyzer    ┃ Subscore ┃
│ lexicon     │    100.0 │
│ phrases     │    100.0 │
│ patterns    │    100.0 │
│ punctuation │     19.3 │
│ repetition  │    100.0 │
│ rhythm      │      0.0 │
│ density     │     33.4 │
┃ Sev      ┃ Category        ┃ Line ┃ Issue                                ┃
│ critical │ phrase          │   12 │ cliché phrase: 'little did she know' │
│ major    │ filter_word     │   10 │ filter word 'felt' distances the ... │
│ minor    │ word_echo       │   10 │ word 'felt' repeated 3x within a ... │
...
```

Three layers, in the order you should read them:

1. **Score and verdict.** 0–100, banded: under 15 **clean**, under 30 **touched up**, under 55 **slop-adjacent**, 55+ **slop**. The score is a weighted mean of the subscores plus a bonus for critical/major findings (capped at +20).
2. **Subscores** tell you *which kind* of problem dominates. A high lexicon/phrases score means vocabulary swaps will fix it; a high rhythm score means the sentences need restructuring, which no word-swap will touch.
3. **Findings** carry line numbers and verbatim quotes, worst first. In `--fmt json` output each finding includes exact character spans — that's what the [UI](ui.md) uses to highlight them in the prose.

The rich table prints once and pipes cleanly (`stoner slop 2 | less -R` keeps the colors).

## The philosophy: clusters, not accusations

No single word proves anything. Real writers use em-dashes; *labyrinth* is a fine word for an actual labyrinth. The detector is built around that caveat:

- **Rates, not counts.** Nearly everything is measured per 1,000 words, so a long chapter isn't punished for using "felt" eight times across 5,000 words.
- **Cluster evidence.** The score only climbs when signals stack: banned vocabulary *and* uniform rhythm *and* filter-word saturation together are what "slop" means here. One critical phrase in an otherwise clean chapter adds a few points, not a conviction.
- **Short-document damping.** Below 200 words the score is scaled down proportionally — two unlucky word choices in a 100-word fragment are not enough signal to convict anything. This is why a sloppy snippet can still score lower than the same prose at chapter length.
- **Finding caps.** At most 5 findings per term and 3 per repeated n-gram reach the report, so one tic doesn't flood the table (all occurrences still count toward the score).

## Tuning

**The gate** (what `stoner write` enforces) is configured in `stoner.yaml`:

```yaml
gates:
  slop_max_score: 25.0            # fail the gate above this score
  slop_block_severities: [critical]  # any finding of these severities also fails it
  max_revision_loops: 2           # auto-revision attempts before giving up
```

**Your own banned list** goes in the `## Banned` block of `canon/style.md`:

```yaml
words:
  - delve
  - shimmering        # your personal tic, not just the model's
phrases:
  - "couldn't help but"
```

This block works on both ends of the pipeline. Upstream, the writer agent reads the full style guide before every draft, and the revise stage passes the banned list explicitly with instructions to avoid the entries. Downstream, the detector itself flags every occurrence of your banned words and phrases as a `major` finding (marked "banned in canon/style.md") — in `stoner slop`, the write-pipeline gate, the agent's own `slop_check` tool, and the dashboard alike. Matching is whole-word and case-insensitive, so `shimmering` catches "Shimmering" but not "shimmeringly" — list the inflections you actually want banned. Note that a major finding raises the score but doesn't by itself fail the default gate (only `critical` severities block); add `major` to `slop_block_severities` if you want banned terms to be absolute.

**Scoring weights** are exposed programmatically via `SlopConfig` if you're scripting:

```python
from stoner.slop import run_slop, SlopConfig
report = run_slop(text, config=SlopConfig(weights={"lexicon": 0.3, "phrases": 0.2,
    "patterns": 0.2, "punctuation": 0.05, "repetition": 0.1, "rhythm": 0.1, "density": 0.05}))
```

The CLI always uses the default weights (lexicon 0.15, phrases 0.20, patterns 0.20, punctuation 0.10, repetition 0.15, rhythm 0.10, density 0.10).

## Limitations and false positives

- **Style is not slop.** A semicolon-loving literary voice or a deliberately incantatory repetition will score points it doesn't deserve. If the punctuation or rhythm subscore fights your actual style, raise `slop_max_score` rather than contorting the prose. The detector is a smoke alarm, not an editor.
- **Literal uses get flagged.** *Tapestry* in a scene about weaving is still a critical lexicon hit. Check the quote before acting on a finding.
- **The sentence splitter is pragmatic**, not linguistic — unusual abbreviation or dialogue punctuation can skew rhythm stats slightly.
- **It can't catch higher-order slop** — hollow profundity, symmetrical sentimentality, a scene that explains itself. That's what the LLM `line` and `adversarial` [review passes](review.md) are for; the two systems are deliberately separate.
- **It says nothing about authorship.** A high score means the prose shares statistical habits with model output — a human on autopilot can score high, and an edited model draft can score clean. Both readings are the point.
