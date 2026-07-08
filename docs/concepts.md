# Concepts

st0n3r takes its name, sideways, from John Williams's *Stoner* — a novel about a man who gives his whole working life to the patient, unglamorous craft of literature and is largely unrewarded for it. That is the temperament this harness tries to enforce on language models, which left to themselves are the opposite: fast, eager, ornamental, and forgetful. Everything below is a mechanism for slowing the machine down to the speed of craft.

## The canon method

The `canon/` directory is the single source of truth for the book. Not the manuscript — the canon. When the two disagree, that disagreement is surfaced as a problem to fix, never silently papered over.

Every character and world file is split in two, and the split is load-bearing:

- **Frontmatter = hard facts.** Age, eye color, relationships, allegiances, `status: alive`. These are YAML fields the archivist can *diff*: if canon says `eyes: gray` and chapter twelve says green, that's a machine-detectable contradiction, and the harness raises it instead of letting the model average it away.
- **Body = soft characterization.** Voice, wants and fears, arc. Prose the archivist never touches, because "how Mara talks" isn't a fact you can diff — it's a judgment that belongs to you.

Two markdown tables round out the bible. `canon/timeline.md` holds dated events in story order; the archivist appends rows as chapters land. `canon/threads.md` tracks every planted question and Chekhov's gun with a status of `open`, `resolved`, or `abandoned` — abandoned being an honest outcome, unlike a thread that just quietly vanishes. `stoner threads` lists them; a thread still open when the manuscript ends is worth a look before you call the book done.

Agents never see canon raw. They get a **context pack** (`stoner canon pack` shows it verbatim): premise first, then style, open threads, characters ordered by most recent appearance, world entries, and recent timeline — added whole until a character budget runs out, so lower-priority sections drop cleanly rather than getting mangled mid-sentence.

## Memory: why agents never read the whole manuscript

By chapter twenty your manuscript won't fit in a context window, and stuffing it in wouldn't help if it did — models attend poorly to 80,000-word prompts, and you'd pay for every token of it on every call.

So the harness never sends the manuscript. Instead, `.stoner/memory.json` keeps a **rolling summary**: a per-chapter summary written by the archivist, plus a condensed "book so far" rebuilt from them. When drafting chapter N, the writer gets the book-so-far, full summaries of the last three chapters, one-line compressions of everything earlier, and the closing ~500 words of chapter N-1 for continuity of voice. If it needs more, it has a `read_chapter` tool and can go look — pulling exactly the scene it needs instead of drowning in all of them.

When the book-so-far outgrows its cap, the *oldest* chapters are dropped first. The tail of the book matters more to "what is true right now," and the early chapters are the ones already best reflected in canon.

## The ledger

Every action — a chapter drafted, a fact applied, a slop check, each agent turn — appends a line to `.stoner/ledger.jsonl`. Full agent transcripts go to `.stoner/sessions/`. When you wonder "what changed my character sheet?", `stoner ledger` answers it. Nothing the harness does is off the record.

## The write pipeline

`stoner write N` runs three stages, each also available as its own command:

1. **Draft.** The writer agent gets the beat sheet, canon pack, memory, and style guide, plus tools (`query_canon`, `write_chapter`, `slop_check`, ...). It's instructed to check canon before inventing facts and can self-check its own slop score before finishing. The chapter lands in `manuscript/ch-NN.md`.
2. **Slop gate.** The deterministic detector scores the draft. If the score exceeds `gates.slop_max_score` (default 25) or any finding hits a blocked severity (default: `critical`), the harness feeds the worst findings to an automatic revision — up to `gates.max_revision_loops` times (default 2). If it still fails, the chapter is kept and the failure is *reported*, not hidden: you decide what happens next.
3. **Archive.** The archivist extracts facts from the finished prose, diffs them against canon, applies the safe ones, appends timeline rows, updates threads, writes the chapter summary into memory. Contradictions become **conflicts** for you to resolve by hand — canon is never auto-overwritten. See [Review & revise](review.md#archivist-conflicts-and-how-to-resolve-them).

(One nuance: [text-only providers](providers.md#cli-backed-providers-codex-and-claude-code) draft in a single comprehensive completion instead of the tool loop — the system prompt already carries everything the tools would fetch.)

This same pipeline is the unit of [autonomous mode](autonomous.md): `stoner book` runs it for every planned chapter, adding whole-book review and revision rounds on top, and `stoner brainstorm`/`stoner foundation` build the canon it draws from.

## Two immune systems

Quality checking is deliberately split into two independent systems that fail differently:

- **Deterministic checks** (`stoner slop`) are regex, counting, and statistics. They cost nothing, run in milliseconds, never hallucinate, and give the same answer every time — which is exactly what you want in a *gate*. Their weakness: they only catch what's in the lexicon and the math. See [The slop detector](slop.md).
- **LLM judges** (`stoner review`) catch what no regex can — a continuity error against canon, a sagging middle, two characters who sound identical. Their weakness: they're expensive, non-deterministic, and can be confidently wrong. So they never gate anything; they produce *findings* that sit in a report until you accept or dismiss them. See [Review & revise](review.md).

An LLM can miss a cliché the regex catches instantly; a regex can't know your timeline broke. Keeping them separate means one system's blind spot is not the other's.

## Comparative grading over absolute scores

Ask a model to rate a chapter 1–10 and nearly everything comes back a 7 or an 8 — absolute LLM scoring collapses into a band too narrow to act on. So st0n3r's judgment passes are built to force *relative* choices instead:

- the `grade` pass labels every paragraph **STRONG / FINE / WEAK / CUT** — no numbers allowed;
- the `adversarial` pass must cut exactly 400 words and justify each cut;
- the `panel` pass convenes four reader personas and only promotes issues that three of four raise independently.

"Which paragraph is weakest?" gets a useful answer where "how good is this?" gets flattery. The slop *score* is 0–100, but it's computed arithmetic, not model opinion — that's the other half of why the two immune systems stay separate.

## The instrument layer

The core loop above — canon, memory, the write pipeline, the two immune systems — is the whole harness at v0.2.0. Everything below is an additive layer of *instruments*: measurement, revision, and shipping tools that sit on top of the loop without changing it. Each owns its own directory, config block, and ledger namespace; each is off (or empty, or opt-in) until you reach for it, so a project that only wants to draft chapters pays nothing for the rest. They share one discipline with the core: deterministic checks may gate, model judgments only advise.

### Voice as a measured fingerprint

Style guides say "spare, declarative"; that's a wish, not a measurement. The **voice fingerprint** turns a body of exemplar prose into numbers — function-word frequencies, sentence-length distribution, punctuation rates, and other closed-class habits — and scores new prose as a *drift* against them, in the spirit of [Burrows' Delta](CREDITS.md). It is entirely deterministic (no model, like the slop detector), so it can act as an optional gate in the write pipeline. Where slop measures *distance from good prose in general*, voice measures *distance from your prose in particular* — a chapter can be clean of slop and still not sound like your book. See [Voice](voice.md).

### Interiority the narrator can't see

Canon is fed straight into the writer's prompt, which is exactly why canon cannot hold secrets — a character's unspoken want or active lie that reaches the prompt gets narrated onto the page, and subtext dies. So **cast sheets** live outside canon, in private per-character state (knowledge with the chapter it was learned, wants stated vs. real, lies, refusals) that the writer-facing context path never reads. From that private state comes a machine-checkable **knowledge-boundedness** check — a character acting on a fact before they could know it is a violation — and scene simulation where character agents collide by genuine information asymmetry. See [Cast](cast.md).

### Taste as a comparison, not a score

The same reason [absolute scores collapse](#comparative-grading-over-absolute-scores) shapes the revision instruments. A **draft tournament** drafts N takes of a chapter from distinct angles, judges them in *blind pairwise* comparisons, and proposes a winner for a human to confirm — "which of these two is better" is answerable where "rate this 1–10" is not. **Reader simulation** runs a roster of personas over the finished manuscript and reports where enough of them independently disagree, and can **benchmark** the book blind against a public-domain comp, chapter-aligned. Both turn judgment into votes. See [Tournaments](tournaments.md) and [Readers](readers.md).

### Advisory instruments: pacing and the room

Two instruments produce findings that never gate, only inform. **Pacing** reads the whole book as a shape — scene-vs-summary ratio, a tension curve, flatline and POV-whiplash runs — mixing deterministic structural measures with optional per-chapter LLM instruments. The **Writers' Room** is a persistent roster of editor personas that keep *notebooks*: a running opinion and open items that survive across sessions, so an editor can note "still not convinced the middle earns its length" in chapter 7 and re-check it later, cross-examine its own prior flags, and pin margin comments to spans. See [Pacing](pacing.md) and [Writers' Room](room.md).

### Verisimilitude: facts as canon

Models hallucinate checkable real-world detail confidently. The **fact locker** stores sourced specifics — a fee, a statute, a form, a brand — as first-class canon artifacts under `canon/facts/`, each requiring a source URL (a fact with no source is dropped, never stored). Facts feed the writer through the same context-pack channel as the rest of canon; the **verisimilitude sweep** holds a chapter against them, flagging contradictions and unsourced confident claims. Web research to *build* the locker is a pluggable, explicitly opt-in capability with a ledgered trail for every network action. See [Facts](facts.md).

### Promises and motifs

`canon/threads.md` already tracks open questions; the **promise ledger** types those rows by kind (mystery, threat, want, image) and tracks each from plant to payoff, giving a *deterministic* no-unfired-guns gate — a promise-kind row still open when the book ends fails the check, no model required. The **motif registry** is the recurrence half: a deterministic matrix of every registered motif across chapters, plus model-assisted candidate mining and an ending-rhymes-with-opening scan. See [Promises & motifs](motifs.md).

### Provenance and shipping

Every rewrite the harness makes — draft, slop-revise, review-revise, tournament graft, refactor, restore — routes through a single snapshot chokepoint, so **draft archaeology** can show a chapter's full history, attribute each current sentence to the rewrite event that introduced it (**blame**, deterministic), restore reversibly, and perform guarded structural refactors (merge, split, move-reveal, flip-POV). At the end, the **production line** runs a readiness check (gaps, non-shippable statuses, and unfired guns are blockers; plain open threads only warn) and exports dependency-free EPUB, typeset PDF, submission DOCX, marketing blurbs, and stitched table-read audio. See [Drafts](drafts.md) and [Ship](ship.md).

## Where things live

| Path | What | Who writes it |
|---|---|---|
| `canon/` | the bible | you + archivist (facts only) |
| `canon/facts/` | sourced real-world facts | you + `facts` (diff, not overwrite) |
| `canon/threads.md` | plot threads, typed by promise kind | you + archivist + `promises` |
| `canon/motifs.md` | the motif registry | you + `motifs` |
| `outline/beats/` | per-chapter beat sheets | you (or the agent, via `update_beats`) |
| `manuscript/` | the prose | you and/or the writer agent |
| `notes/exemplars/` | voice-fingerprint exemplar prose | you |
| `export/` | shipped EPUB/PDF/DOCX, blurbs, audio | `ship` |
| `.stoner/memory.json` | rolling summaries | archivist |
| `.stoner/ledger.jsonl` | action log | everything |
| `.stoner/sessions/` | full agent transcripts | agent runs |
| `.stoner/reviews/` | reports, tagged by `kind` (review, slop, book, voice, pacing, cast, readers) | `slop --save`, `review`, `review-book`, `pacing`, `voice --save`, `cast check`, `facts sweep`, `readers`, `motifs rhyme` |
| `.stoner/voice/` | the learned voice fingerprint | `voice learn` |
| `.stoner/cast/` | private per-character sheets + scene scripts | `cast` |
| `.stoner/tournaments/` `.stoner/taste/` | tournament runs and the learned taste model | `tournament` |
| `.stoner/room/` | editor notebooks, session records, margin comments | `room` |
| `.stoner/readers/` | reader runs, heatmaps, benchmarks | `readers` |
| `.stoner/drafts/` | per-chapter snapshot history (provenance) | every rewrite path |
| `.stoner/book-state.json` | resumable autonomous-run state | `stoner book` |

Everything is plain markdown, YAML, and JSON. There's no database, no lock-in; the whole project diffs cleanly under git, and you can edit any file by hand at any time — the harness reads from disk fresh on every operation.
