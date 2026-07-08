# Draft archaeology

Every rewrite the harness makes to a chapter routes through a single snapshot chokepoint, so `stoner drafts` can show a chapter's whole history, attribute each current sentence to the rewrite that introduced it, restore any past state reversibly, and perform guarded structural refactors. It's version control tuned for prose — finer-grained than git commits, and it records *why* each change happened.

```bash
stoner drafts list 3              # ch-03's snapshots, newest last
stoner drafts show 3 <snap>       # one snapshot verbatim
stoner drafts diff 3 <a> <b>      # unified diff between snapshots (or a snapshot vs. current)
stoner drafts blame 3             # attribute each current sentence to the rewrite that introduced it
stoner drafts restore 3 <snap>    # restore a chapter (or one paragraph); reversible
stoner drafts snapshot 3          # manually snapshot the current chapter (reason: manual)
stoner drafts prune 3             # bound snapshot storage, keeping the originals
stoner drafts verify              # deterministic project-integrity check (may exit nonzero)
```

## The snapshot store

Snapshots live under `.stoner/drafts/ch-NN/`, written automatically before every rewrite path in the harness. Each carries a **reason** naming what produced it — `draft`, `slop-revise`, `review-revise`, `book-revise`, `tournament-graft`, `refactor-*`, `restore`, `human-edit`, `manual` — which is what makes the history legible rather than a pile of anonymous versions. Because the store is a chokepoint every rewrite passes through, [tournament](tournaments.md) takes, [revise](review.md) rewrites, and refactors all leave provenance for free.

Snapshots dedup by content (`archaeology.dedup`), so an idempotent re-run doesn't bloat the store.

## Blame and restore

`stoner drafts blame` is **deterministic** — no model. It walks the snapshot chain and attributes each sentence in the current chapter to the rewrite event that introduced it, so you can see at a glance which prose is original draft, which came from a slop revision, which a refactor moved in.

`stoner drafts restore` brings back a whole chapter or a single paragraph from a snapshot. The pre-restore state is itself snapshotted first (reason `restore`), so a restore is never destructive — you can always undo it.

`stoner drafts prune` bounds storage. The **original draft and every human-edit snapshot always survive**; pruned entries keep their content hashes so `blame` still resolves against them. `keep_per_chapter` sets the default retention.

## Structural refactors

`stoner drafts refactor` handles the chapter-spanning edits that a plain rewrite can't:

- **`merge` / `split`** — combine two adjacent chapters or split one in two, renumbering everything above. **Deterministic** — no model.
- **`move-reveal <src> <dst> --quote "..."`** — move a reveal from one chapter to another, weaving it in (`--position early|late`). Model-assisted, guarded, snapshot-first.
- **`flip-pov <chapter> --to "<character>"`** — rewrite a chapter from another character's POV. Model-assisted, guarded.

The two model-assisted refactors run **advisory model verification** afterward — a continuity check that the edit didn't break the book — unless you pass `--no-verify` or set `archaeology.verify_after_refactor: false`. Deterministic integrity checks run regardless.

## Verify

`stoner drafts verify` runs the deterministic project-integrity check (chapter numbering, snapshot chain, canon consistency) and may **exit nonzero**, so it can gate a script. Passing chapter numbers (`stoner drafts verify 3 4`) additionally runs advisory model verification on those chapters — a continuity review plus an archivist preview — against the reviewer model.

## Configuration

```yaml
archaeology:
  enabled: true               # route rewrites through the snapshot store
  dedup: true                 # skip snapshots identical to the last
  keep_per_chapter: null      # `drafts prune` default retention (null = keep all)
  verify_after_refactor: true # run advisory model verification after model-assisted refactors
```

Model-assisted refactors draft against the `writer` role; verification uses `reviewer`.
