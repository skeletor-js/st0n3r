# Autonomous mode: seed to book

Three commands take a project from a one-line idea to a reviewed manuscript: `brainstorm` builds the premise and style, `foundation` builds the rest of the bible, and `book` drafts and revises every chapter. A fourth, `review-book`, runs a single whole-manuscript review on demand.

Everything they produce is the same plain markdown the [manual workflow](getting-started.md) uses — the same templates, the same canon layout — so you can stop the machine at any point, edit by hand, and pick up wherever you like. Autonomy here means the harness drives the loop, not that you're locked out of it.

## 1. `stoner brainstorm` — seed to premise

```
$ stoner brainstorm "an aging cannabis grower in Humboldt faces legalization"
Brainstormed — wrote canon/premise.md, canon/style.md
Logline: When California legalization arrives with a permit process, a corporate
buyout offer, and a market that no longer needs her, 61-year-old Humboldt grower
Ruth Vann must decide whether forty years of clandestine, exacting labor can
survive becoming legal...
Genre: Literary fiction / contemporary realism
Title options:
  - ...
Review/edit canon/premise.md and canon/style.md, then run: stoner foundation
```

(That logline is from `examples/novella`, a project grown from a seed much like this one.)

One model call fills the premise template (logline, genre and comps, themes, promise to the reader) and the style template (voice, POV, tense, sentence rhythm), and merges model-suggested banned words/phrases into the `## Banned` block — which the [slop detector](slop.md#tuning) then enforces.

If `premise.md` or `style.md` already has content beyond the template, `brainstorm` refuses rather than clobbering your work; `--force` overrides. The instructive comments in both files survive, so hand-editing afterward feels the same as starting from a blank template. **Do edit them** — everything downstream inherits their quality.

## 2. `stoner foundation` — premise to bible

```
$ stoner foundation
running foundation pipeline: characters -> world -> threads -> outline -> evaluate...
characters (4): ruth-vann, casey-berg, dale-orozco, mina-vann
world (3): the-vann-parcel, emerald-triangle, calgrow-collective
threads (6): t1, t2, t3, t4, t5, t6
outline: 18 chapter(s)
evaluate: 1 loop(s), verdict: ship
```

Requires a filled premise (brainstormed or hand-written). It then makes a sequence of small, separately-prompted calls — characters, world entries, opening threads, act structure — so each generation sees the canon built so far. Output lands in the standard places: character/world files with hard-fact frontmatter, `threads.md` rows, `outline/outline.md`, and one beat sheet per planned chapter under `outline/beats/`.

Then the **evaluate loop**: a comparative judgment pass ([never absolute scores](concepts.md#comparative-grading-over-absolute-scores)) names the single weakest element and whether it's weak enough to hurt drafting. If the verdict is `iterate`, just that element is regenerated with the judge's fix instructions and re-evaluated, up to `--max-loops` times (default 2).

Flags: `--characters N` (default 4), `--world N` (default 3), `--max-loops N`, `--model`. Elements that already exist are skipped and noted — rerunning is safe; `--force` regenerates them.

Between `foundation` and `book` is the highest-leverage moment for a human pass: read the beat sheets, fix the outline, sharpen a character's voice. Ten minutes here beats hours of revision later.

## 3. `stoner book` — the chapter loop

```
$ stoner book --max-chapters 4 --max-minutes 60
ch-01 drafted — 2,481 words, slop 11.2
ch-02 drafted — 2,190 words, slop 14.8
...
review round 1: 7 major finding(s), verdict needs-work
  revised ch-02 (3 finding(s))
  revised ch-03 (4 finding(s))
review round 2: 2 major finding(s), verdict needs-work
┃ metric                      ┃ value ┃
│ chapters written            │     4 │
│ total words                 │ 9,043 │
│ review rounds               │     2 │
│ remaining major findings    │     2 │
│ remaining critical findings │     0 │
state: /home/you/mybook/.stoner/book-state.json
```

The plan is every chapter with a beat sheet or an existing manuscript file. For each unwritten one, in order, `book` runs the full [write pipeline](concepts.md#the-write-pipeline) — draft, slop gate, archivist — so canon and memory stay current as the book grows.

Every `--review-every` chapters (default 4), and once more at the end, it runs **whole-book review rounds**: a single review of the entire manuscript, then a revision of each chapter that drew major or critical findings, then another review — up to `--max-rounds` per pass (default 2), stopping early when majors hit zero or **plateau** (a round that didn't reduce the major count means another one probably won't either). Set `--review-every 0` to skip interim reviews and keep only the final one.

The command exits nonzero if critical findings remain, so it can gate a script.

### Resumable by design

State lives in `.stoner/book-state.json`: chapters planned, chapters done (words, slop score, revision cycles), the current phase, and every book-review outcome. It is saved **before every model call**, so Ctrl-C, a crash, or a rate-limit death always leaves a resumable state:

```bash
stoner book               # picks up exactly where the last run stopped
stoner book --no-resume   # re-plans from scratch (chapter files are never deleted)
```

On resume, a chapter counts as written if the state recorded it *or* the file already holds substantial prose (500+ words with a draft/revised/final status) — so chapters you wrote by hand are respected, not redrafted. A corrupt state file is backed up to `book-state.json.bak` and a fresh one started. The [dashboard's Book panel](ui.md#book) renders this state live while a run is going.

### Budgets and cost

Rough per-call arithmetic, so you can predict a run before paying for it:

- **brainstorm**: 1 call. **foundation**: 5 calls (4 generations + evaluate), plus 2 per iterate loop.
- **each chapter**: a handful of calls — the draft (one per agent turn; exactly one for [text-only providers](providers.md#cli-backed-providers-codex-and-claude-code)), 0–2 slop-gate revisions, and one archivist extraction.
- **each whole-book review round**: one large call (the manuscript is the prompt — the most expensive single call in the harness) plus one revision call per flagged chapter. For books past ~12 chapters, only the 6 most recent go in as full text and the rest as memory summaries, which keeps this call from growing without bound.

Caps: `--max-chapters N` bounds the damage of any single run; `--max-minutes M` is a wall-clock cap checked between chapters (a run stops cleanly, never mid-call). Both compose with resume, so `stoner book --max-chapters 3` run daily is a perfectly good way to write a book on a budget. A cheap archivist model and a trimmed `review_passes` list help too — see the [FAQ](faq.md#what-does-an-autonomous-run-cost-and-how-do-i-cap-it).

## 4. `stoner review-book` — one whole-manuscript pass

```
$ stoner review-book
overall: The middle sags between the buyout offer and the harvest...
verdict: needs-work  (9 major, 1 critical)
┃ chapter 7 findings... ┃
saved to .stoner/reviews/book-1783442210.json
```

The same review `book` runs internally, standalone: the reviewer reads the manuscript once as a literary critic and once as a professor of fiction, and returns findings tagged by chapter, grouped in the output and saved as JSON + Markdown under `.stoner/reviews/`. Verdict is `ready` or `needs-work`. Feed the findings to `stoner revise <n>` per chapter, or just use the report as a reading list for your own revision pass.

## An honest end-to-end example

```bash
stoner init novella && cd novella
stoner brainstorm "an aging cannabis grower in Humboldt faces legalization"
$EDITOR canon/premise.md canon/style.md      # sharpen — this is the book's DNA
stoner foundation
$EDITOR outline/outline.md outline/beats/    # fix the plan before it's prose
stoner book --max-chapters 5 --max-minutes 90
stoner ui                                     # read, triage findings, repeat
stoner book                                   # resumes at chapter 6
stoner review-book                            # final verdict
```

The machine drafts; the two human checkpoints (after brainstorm, after foundation) and the reading in between are what keep the result a book rather than a very long slop report.
