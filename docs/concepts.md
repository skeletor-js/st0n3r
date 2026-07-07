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
3. **Archive.** The archivist extracts facts from the finished prose, diffs them against canon, applies the safe ones, appends timeline rows, updates threads, writes the chapter summary into memory. Contradictions become **conflicts** for you to resolve by hand — canon is never auto-overwritten. See [Review & revise](review.md#archivist-conflicts).

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

## Where things live

| Path | What | Who writes it |
|---|---|---|
| `canon/` | the bible | you + archivist (facts only) |
| `outline/beats/` | per-chapter beat sheets | you (or the agent, via `update_beats`) |
| `manuscript/` | the prose | you and/or the writer agent |
| `.stoner/memory.json` | rolling summaries | archivist |
| `.stoner/ledger.jsonl` | action log | everything |
| `.stoner/sessions/` | full agent transcripts | agent runs |
| `.stoner/reviews/` | slop + review reports | `slop --save`, `review` |

Everything is plain markdown, YAML, and JSON. There's no database, no lock-in; the whole project diffs cleanly under git, and you can edit any file by hand at any time — the harness reads from disk fresh on every operation.
