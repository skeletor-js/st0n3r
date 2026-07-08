# Promises & motifs

Two ledgers that track what a book promises and what it echoes. **Promises** type the rows in `canon/threads.md` by kind and follow each from plant to payoff, giving a deterministic no-unfired-guns gate. **Motifs** track recurrence — the same image, phrase, or object surfacing across chapters. Promises and the motif *matrix* are fully offline; two motif scans call a model and can be turned off.

```bash
# promises — all offline, no API key
stoner promises plant --kind mystery --desc "who cut the fence"   # an open, kind-typed thread row
stoner promises list                                              # planted promises and status
stoner promises payoff <id> --chapter 14                          # stamp resolved + payoff chapter
stoner promises check                                             # exit 1 while any promise-kind row is open

# motifs
stoner motifs add "the unlit lighthouse"     # register a motif by hand (offline)
stoner motifs scan                            # deterministic recurrence matrix (offline)
stoner motifs candidates --no-judge           # mine recurring n-grams; --no-judge skips the model
stoner motifs rhyme                           # does the ending rhyme with the opening?
```

## Promises: typed threads

`canon/threads.md` already tracks open questions; the promise ledger adds a **kind** to each — a *mystery*, *threat*, *want*, or *image* — and treats a kind-typed row as a Chekhov's gun that must fire. `stoner promises plant` writes an open, typed row; `stoner promises payoff <id>` stamps it `resolved` with the chapter that paid it off.

`stoner promises check` is a **deterministic gate**: it exits nonzero while any promise-kind row is still open, so it can gate a script or a release. This is the strict half — a *typed* promise left dangling is a real not-done. Plain kind-less open threads are the writer's call (abandoning a thread is a legitimate outcome), so they don't fail the check; they only ever warn. This is the same distinction [ship check](ship.md) draws between unfired guns (blockers) and plain open threads (warnings). Everything here is offline — no model, no key.

## Motifs: recurrence

`stoner motifs add` registers a motif; `stoner motifs scan` prints a **deterministic recurrence matrix** — every registered motif against every chapter, so you can see where an image lands and where it goes quiet. Registry and matrix need no model.

Two scans do call the reviewer model, and each takes `--no-judge` to run offline:

- **`stoner motifs candidates`** mines *unregistered* recurring n-grams — phrases that repeat across at least `candidate_min_chapters` (default 3) distinct chapters, capped at `candidate_cap` (default 12) — and optionally triages them with the model so you can promote the real motifs and ignore the coincidences. `--no-judge` returns the raw mined list.
- **`stoner motifs rhyme`** asks whether the ending rhymes with the opening: a deterministic overlap measure across `rhyme_window` (default 1) chapters at each end, plus an advisory model verdict. `--no-judge` keeps only the deterministic overlap; the report saves to `.stoner/reviews/` (tagged as a review report) unless `--no-save`.

## Configuration

```yaml
motifs:
  candidate_min_chapters: 3   # distinct-chapter spread a mined n-gram must clear
  candidate_cap: 12           # max candidates returned
  rhyme_window: 1             # chapters at each end the ending-rhyme scan compares
```

Promise/motif measurement is otherwise deterministic and needs no config; the two model-calling scans resolve against the `reviewer` role.
