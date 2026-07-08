# The fact locker

Literature is dense with true detail; models hallucinate confidently. The fact locker is st0n3r's memory of checkable real-world specifics — the exact name of a form, a fee, a statute, a procedure, a brand — and the sweep pass holds a draft to them. Web access is a pluggable, explicitly opt-in capability with a ledgered trail for every network action.

```bash
stoner facts add --name "Category II reinspection fee" \
  --claim "Humboldt Category II violations carry a \$1,200 reinspection fee and 60-day cure" \
  --source-url https://humboldtgov.org/... --confidence high   # offline, no network
stoner facts research "Humboldt cannabis permit fees" --apply    # opt-in, touches the web
stoner facts research "..." --url https://humboldtgov.org/code   # seed URLs for fetch mode
stoner facts list                                                # local, read-only
stoner facts show category-ii-reinspection-fee
stoner facts sweep 3                                             # verisimilitude pass on ch-03
```

## Facts are canon

Each fact is a file under `canon/facts/<slug>.md`, a first-class canon artifact:

```yaml
---
name: Category II reinspection fee
claim: "Humboldt Category II violations carry a $1,200 reinspection fee and 60-day cure"
tags: [permits, humboldt]
source_url: https://humboldtgov.org/...
source_title: Humboldt County Code
accessed: 2026-07-07
confidence: high      # high | medium | low
status: unverified    # unverified | verified | disputed (you set this after review)
---
## Quotes
> verbatim source passage...
## Notes
```

The frontmatter is the diffable contract. `source_url` is mandatory — a fact with no source is dropped, never stored, so the harness never silently degrades into remembering detail it cannot attribute. The `status` field and everything below the frontmatter are yours: the harness never machine-edits a fact's body or human-set status once the entry exists.

Applying researched facts follows the canon method exactly (the same [diff-don't-overwrite](concepts.md) logic the archivist uses): new facts write cleanly, a candidate whose slug matches an existing fact but whose claim materially differs surfaces as a **conflict** for you to resolve, and nothing is auto-overwritten. Dry-run is the default; `--apply` commits non-conflicting facts only.

Locker facts feed the writer through the existing channel: `context_pack()` gains a **Facts** section, so drafting and the review passes pick facts up with no extra wiring. The section is whole-or-nothing under budget pressure — a fact never appears half-quoted.

## The three web paths

`facts research` is refused unless you opt in with `facts.enabled: true` in `stoner.yaml`, and even then it refuses inside autonomous `stoner book` mode — book mode consumes the locker, it never builds it. When it runs, it picks a web-capable path in this order:

| Path | Requires | What it can do | Limits |
|---|---|---|---|
| **Native search** | an Anthropic API researcher role, or the `claude` provider | discover and read sources across the open web | Anthropic native search is billed by the provider (currently ~$10 per 1,000 searches); the `claude` CLI path cannot enforce a domain allowlist and reports no per-search count |
| **Fetch** | any tool-capable provider **plus** `--url` seeds or existing locker sources | read URLs it is given or already knows and quote from them | **verification, not discovery** — it cannot search; it only fetches pages you point it at |
| **None** | — | nothing | the command fails with a message naming the three remedies above; there is no silent degradation to unsourced facts |

Set the researcher role with `models.researcher:` in `stoner.yaml` (empty resolves against the writer role). Cap a run's native-search budget with `facts.max_searches` (default 8); restrict both native search and fetch to specific domains with `facts.allowed_domains`.

Fetch mode uses the harness's `httpx` dependency plus a stdlib HTML-to-text reducer — no scraping library. The extraction is deliberately crude (the consumer is a model quoting passages, not a rendering engine); JS-heavy pages may reduce poorly, and extraction failures come back as plain error strings.

## The ledger trail

Every network action is written to `.stoner/ledger.jsonl` under the `facts.*` prefix:

- `facts.research.start` / `facts.research.done` — a run's bounds, path, model, and counts
- `facts.search` — a native-search batch: count, and the queries/URLs where the provider exposes them (the `claude` path records "count unknown")
- `facts.fetch` — one line per harness-side fetch, including failures, carrying the URL and status
- `facts.apply` / `facts.conflict` — each fact written, each conflict surfaced
- `facts.add` — a hand-added fact
- `facts.sweep.run` — a verisimilitude sweep

This is the privacy audit: after any research run you can see exactly what left the machine.

## The verisimilitude sweep

`stoner facts sweep <chapter>` runs one advisory review pass — `verisimilitude` — against the locker. It is [never gating](concepts.md#two-immune-systems) (invariant: LLM judgments stay advisory). It emits standard findings in two categories:

- **contradiction** (major): the chapter asserts a real-world specific that a locker fact refutes; the finding quotes the chapter and names the fact slug.
- **check-this** (minor): the chapter states a confident real-world specific — a date, fee, statute, brand, named form, or procedure — that no locker fact covers; the finding is phrased as a verification task, not a claim of error.

The pass includes the canon digest so it does not flag fiction-internal inventions (an invented system or place) as unsourced. Findings flow through the normal review machinery: they land in `.stoner/reviews/`, locate their quotes to spans, and triage in the [UI](ui.md) unchanged.

The pass is only meaningful with a populated locker, so it is **not** in the default `review_passes` list — running it is always a deliberate choice, either via `stoner facts sweep` or by naming it explicitly in `stoner review <n> --passes verisimilitude`.
