# The voice fingerprint

`stoner voice` measures your prose and scores new writing as a *drift* against that measurement. It is entirely deterministic — no model calls, same answer every time — so like [the slop detector](slop.md) it can act as a gate in the [write pipeline](concepts.md#the-write-pipeline). The difference between the two: slop measures distance from good prose in general; voice measures distance from *your* prose in particular. A chapter can pass the slop gate clean and still not sound like your book.

```bash
stoner voice learn                       # build the fingerprint from voice.exemplars (deterministic)
stoner voice learn notes/chapter-one.md  # add extra exemplar files or directories
stoner voice learn --from-manuscript     # also learn from this project's revised/final chapters
stoner voice show                        # the fingerprint digest and per-feature stats
stoner voice check 3                      # score chapter 3 against the fingerprint (0 = in voice)
stoner voice check all --save             # score every chapter; save a report to .stoner/reviews/
stoner voice check ~/drafts/scene.md      # score any file
```

## What the fingerprint measures

The fingerprint is a bundle of closed-class, style-bearing statistics — the kind that survive topic changes and resist conscious control, which is exactly why they identify a voice. Function-word frequencies do the heavy lifting (the same signal behind [Burrows' Delta](CREDITS.md), the classic authorship-attribution measure), alongside sentence-length distribution, punctuation rates, and other rhythm features. `stoner voice show` prints the digest and the per-feature stats so you can see what the harness thinks your voice *is*.

The function-word inventory ships with the package (`src/stoner/voice/data/function_words.yaml`), hand-assembled from the standard closed-class word lists of English grammar — no external corpus or copyrighted frequency list was used. See [Credits](CREDITS.md).

## Learning a voice

`stoner voice learn` builds the fingerprint from the paths in `voice.exemplars` (default: `notes/exemplars/`, a directory `stoner init` creates for you). Drop in prose you consider on-voice — your own earlier work, a few polished chapters, passages whose rhythm you want to hold to — and run `learn`. Extra paths on the command line are added to the configured ones; `--from-manuscript` also folds in this project's own `revised`/`final` chapters, which is the natural move once a few chapters are locked and you want the book to hold its own established voice.

The fingerprint is stored under `.stoner/voice/`. Learning is deterministic and cheap; re-run it whenever your exemplar set changes.

## Checking prose

`stoner voice check <target>` scores a chapter number, a file path, or `all` against the fingerprint. The score is a **drift**: `0` means the prose sits exactly on the learned voice, and higher numbers mean farther off. Per-feature deltas show *which* dimension drifted — a spike in sentence-length variance reads differently from a spike in a particular function word's rate — so the score points at what to adjust rather than just grading.

`--fmt markdown|json` gives machine-readable output; `--save` writes a report to `.stoner/reviews/` tagged `kind: "voice"`, so it lands in the [UI](ui.md) Reviews panel alongside slop and review reports. (Unlike `review`, `voice check` does not save by default — it's cheap to re-run.)

## The optional drift gate

Voice can gate the write pipeline the way slop does, but it is **off by default** — set `voice.gate: true` in `stoner.yaml` to turn it on. When enabled and a fingerprint has been learned, a fresh draft whose drift exceeds `voice.max_drift_score` (default 40) is fed back for revision the same way an over-threshold slop score is. Because scoring is deterministic, it is a legitimate gate; because voice is a matter of taste, it stays opt-in, and a project that never runs `voice learn` never trips it.

## Configuration

One block in `stoner.yaml`:

```yaml
voice:
  exemplars: [notes/exemplars]   # files/dirs the fingerprint learns from
  gate: false                    # opt-in: gate drafts on measured voice drift
  max_drift_score: 40.0          # drift above this fails the gate (when on)
  weights: {...}                 # per-feature bucket weights (defaults from voice/drift.py)
```

`weights` tunes how much each feature bucket contributes to the drift score; the defaults live next to the scoring curve in `voice/drift.py` and are a reasonable starting point. Everything about voice is deterministic, so no model role applies.
