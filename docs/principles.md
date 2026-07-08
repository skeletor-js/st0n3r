# Principles

LLM prose defaults to slop. Left alone, a model writing at length forgets facts established forty chapters ago, drifts out of voice, and settles into the same overworked vocabulary and rhythms. Every mechanism in st0n3r is a way of slowing the machine down to the speed of craft. Six principles run through all of it.

**Prose quality is a fight against a default, not a feature you request.** The slop detector exists because "write well" is not an instruction a model reliably follows. So the harness measures the tells — clichés, filter words, uniform sentence rhythm, punctuation habits — deterministically, and gates on them.

**Absolute LLM scores collapse, so judgment must be comparative.** Ask a model to rate a chapter 1–10 and almost everything comes back a 7. So st0n3r's judgment passes force relative choices instead: paragraphs labeled STRONG / FINE / WEAK / CUT with no numbers allowed, a cut of exactly 400 words, blind pairwise take-versus-take comparisons, a panel that promotes only what several readers raise independently.

**Deterministic checks may gate; LLM output only advises.** Two immune systems that fail differently. Regex, counting, and statistics cost nothing, never hallucinate, and give the same answer every time — that is what you want in a *gate*. Model judgment catches what no regex can — a continuity break, a sagging middle, two characters who sound alike — but it is expensive, non-deterministic, and can be confidently wrong, so it never gates. It produces *findings* that sit in a report until you accept or dismiss them. One system's blind spot is not the other's.

**Canon is the single source of truth, and frontmatter is diffable fact.** The `canon/` directory is the book's law — not the manuscript. Character and world files split in two: YAML frontmatter holds hard facts a machine can diff (`age: 34`, `eyes: gray`), prose below holds the soft characterization only prose can carry. When the manuscript and the bible disagree, that disagreement is surfaced as a conflict to resolve, never silently averaged away.

**The ledger is an append-only account of every mutation.** A chapter drafted, a fact applied, a slop check, each agent turn — every action appends a line to `.stoner/ledger.jsonl`, and full transcripts land in `.stoner/sessions/`. When you wonder "what changed my character sheet?", `stoner ledger` answers it. Nothing the harness does is off the record.

**Plain files, no database, local-first.** The whole project is Markdown, YAML, and JSON. It diffs cleanly under git, you can edit any file by hand at any time, and the harness reads from disk fresh on every operation. The dashboard binds to `127.0.0.1`. Nothing leaves your machine unless you run a command that calls a model, and the two commands that can reach the open web are both opt-in and ledgered.

For how these principles turn into mechanisms — the canon method, rolling memory, the write pipeline — see [Concepts](concepts.md). For the full instrument-by-instrument walkthrough, see [The tour](tour.md).
