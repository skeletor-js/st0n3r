# FAQ

### Does st0n3r send my writing anywhere?

Only when you run a command that calls a model, and then only to the provider you configured — carrying that call's chapter plus the canon/memory context, nothing more. Two commands can also reach the open web, both opt-in; everything else is local files only.

**Commands that call your configured model:**

- **Core pipeline** — `write` (draft, slop-gate revisions, archivist), `review`, `revise`, `archive`, and the autonomous trio `brainstorm` / `foundation` / `book`. `review-book` sends the whole manuscript as its prompt.
- **Voice** — none. `voice learn`, `voice show`, and `voice check` are fully deterministic; the optional `voice.gate` in the write pipeline scores drafts with math, not a model.
- **Pacing** — `pacing report` calls the reviewer model for its per-chapter LLM instruments; `pacing report --no-llm` (or `pacing.llm_instruments: false`) makes it fully offline.
- **Tournaments** — `tournament run` drafts N takes and judges them (writer + reviewer roles); `tournament apply` calls the model only when `--graft` folds losing takes' steals into the winner (the default — `--no-graft` is deterministic). `tournament list` / `status` / `vote` are offline.
- **Writers' Room** — `room session` (one call per editor per pass, plus cross-exam and at most one re-locate and one comment follow-up). `room comment` / `comments` / `notebook` / `status` are offline.
- **Cast** — `cast update`, `cast check`, and `cast scene` call a model; `cast init` / `list` / `show` do not.
- **Facts** — `facts sweep` runs the verisimilitude pass against the reviewer model. `facts research` reaches the open web (see below). `facts add` / `list` / `show` are offline.
- **Motifs & promises** — `motifs candidates` and `motifs rhyme` call the model for their advisory triage/verdict; `--no-judge` makes each offline. `motifs list` / `add` / `scan` and every `promises` command are deterministic and offline.
- **Readers** — `readers run` (persona reactions) and `readers bench` (pairwise judging against a comp) call the reader model. `readers personas` / `heatmap` / `comps` are offline.
- **Drafts** — `drafts refactor move-reveal` and `drafts refactor flip-pov` call the writer model, then run advisory model verification unless `--no-verify` (or `archaeology.verify_after_refactor: false`). `drafts verify` with chapter arguments also runs model verification. `drafts refactor merge` / `split` and every other `drafts` command (`blame`, `restore`, `diff`, `snapshot`, `prune`, `list`, `show`) are deterministic.
- **Ship** — `ship blurbs` calls the writer model to draft the synopsis/query/cover, and `ship audio --assist` calls the model to resolve ambiguous dialogue lines. `ship check` / `epub` / `pdf` / `docx` / `voices` / `all` are deterministic and local.

**Commands that reach the open web (opt-in, ledgered):**

- **`facts research`** refuses unless you set `facts.enabled: true`, and even then it performs web searches or URL fetches only to build the fact locker; every network action lands in the ledger under `facts.*`. See [the fact locker](facts.md).
- **A network TTS backend** for `ship audio` (`--backend openai`, or `ship.audio.backend: openai`) synthesizes speech through a vendor API. It is never a silent fallback — the default `say` backend is local — and every run records the backend used in the ledger.

Everything else — `slop`, `chapter`, `canon`, `beats`, `threads`, `status`, `ledger`, and the UI — works entirely on local files and makes no network requests. For a fully offline pipeline, point the model roles at [Ollama or another local server](providers.md#ollama-and-other-local-servers) and leave `facts.enabled` off with a local `ship audio` backend.

### What if I already have a manuscript?

Import each chapter — no API key needed:

```bash
stoner chapter import 1 ~/drafts/ch1.md --title "The Locked Room" --pov Mara --status revised
```

Existing YAML frontmatter in the source file is preserved; `import` refuses to overwrite a chapter that already exists. Then build the bible around the prose: create character/world entries (`stoner canon new`), and run `stoner archive N --auto` chapter by chapter, in order — the archivist extracts facts into canon and builds the rolling memory as it goes, so by the last chapter the harness knows your book well enough to draft chapter N+1.

### How do I add a character?

```bash
stoner canon new character "Mara Quill"
```

creates `canon/characters/mara-quill.md` from the template. Fill in the frontmatter facts and the Voice/Wants/Arc sections — the split matters, see [Concepts](concepts.md#the-canon-method). `stoner canon new world "The Undercroft"` does the same for places, factions, and systems. The archivist will also propose new entities it meets in drafts.

### Can I write chapters myself and just use the checks?

Yes, and it's a first-class workflow. Chapters are plain markdown; write them in any editor, then use `stoner slop` and `stoner review` on your own prose and `stoner archive` to keep canon in sync. The drafting agent is optional.

### Can I use it for nonfiction?

Mostly. The slop detector, review passes like `line`, `pacing`, and `adversarial`, the project structure, and the ledger are genre-agnostic. The canon templates and passes like `continuity` and `voice` assume characters, threads, and scenes — for a memoir they map surprisingly well; for a technical book you'd ignore the character machinery and treat canon as your fact sheet. Nothing breaks; some vocabulary just reads fictional.

### Can it write the whole book by itself?

Mechanically, yes: `stoner brainstorm "<seed>"` builds the premise and style, `stoner foundation` generates characters, world, threads, outline, and beat sheets, and `stoner book` drafts every chapter with slop gates, archiving, and whole-book review/revision rounds — resumable at any point. See [Autonomous mode](autonomous.md). Practically, the output is only as good as the two human checkpoints in the middle: editing the premise/style after `brainstorm`, and the outline/beats after `foundation`. Skip those and you'll get a book-shaped object; take them seriously and the machine does the typing while you do the authoring.

### Why did the slop gate reject my chapter?

Two triggers, configured under `gates:` in `stoner.yaml`: the score exceeded `slop_max_score` (default 25), or a finding hit a severity in `slop_block_severities` (default: any `critical` — a single "little did she know" does it). Words you've listed under `## Banned` in `canon/style.md` count as major findings, so a banned-word habit can push the score over the threshold too. During `stoner write` the harness auto-revises up to `max_revision_loops` times; if it still fails, the chapter is kept on disk and the failure reported. Run `stoner slop N` to see exactly what fired, and read [the slop doc](slop.md#limitations-and-false-positives) before deciding whether to fix the prose or loosen the gate — deliberate style tics are a legitimate reason to raise the threshold.

### How do I control costs?

- Use a small model for the archivist (the default is a Haiku-class model) — it runs after every draft.
- Trim `review_passes:` and run the expensive passes (`panel`, `adversarial`) only at milestones.
- Agents never read the whole manuscript ([by design](concepts.md#memory-why-agents-never-read-the-whole-manuscript)), so per-call cost stays roughly flat as the book grows.
- Review reports record token usage; agent transcripts in `.stoner/sessions/` record it per turn.
- `stoner slop` is free — run it as often as you like.

### What does an autonomous run cost, and how do I cap it?

Each chapter is a handful of model calls (the draft, up to two slop-gate revisions, one archivist extraction); whole-book review rounds add one large call each plus a revision per flagged chapter. The caps: `stoner book --max-chapters N` bounds a run to N chapters, `--max-minutes M` is a wall-clock limit checked between chapters, and `--review-every 0` drops the interim reviews. Because state is saved before every model call, hitting a cap (or Ctrl-C) just pauses the run — rerunning `stoner book` resumes it. Small daily capped runs are a legitimate way to write a book on a fixed budget; the arithmetic is in [Autonomous mode](autonomous.md#budgets-and-cost).

### Can I use local models via Ollama?

Yes: `ollama/llama3.3` works out of the box if Ollama is on the default port; other local servers (vLLM, LM Studio) take a one-stanza config entry. See [Providers](providers.md#custom-openai-compatible-endpoints). Expect small models to be shakier at the tool-calling loop and the strict-JSON review formats.

### Can I mix providers — one model to draft, another to review?

Yes. Each role in `stoner.yaml` takes any model string: draft with `anthropic/claude-sonnet-5`, review with `openai/gpt-5.2`, archive with a local model. `stoner write N --model ...` swaps only the drafting stage for one run; details in [model roles](providers.md#model-roles).

### The archivist wrote something wrong into canon. Now what?

Edit the file — canon is plain markdown and you are its owner. Anything that *contradicted* existing canon was never applied automatically; it was surfaced as a conflict for you ([how to resolve](review.md#archivist-conflicts-and-how-to-resolve-them)). The failure mode to watch is the other one: a wrong fact landing in a field canon had never recorded. `stoner ledger` shows exactly what `canon.archive` applied and when.

### How do I resume work / move a project between machines?

The project directory is the entire state — config, canon, manuscript, memory, ledger, reports. Copy it or clone the repo and you're resumed. It's all text, so git handles it well; committing after each `write`/`revise` gives you honest diffs of what the machine changed.

### Can I re-run `stoner write` on a chapter I don't like?

Yes — the writer sees the existing chapter's frontmatter and beat sheet and drafts fresh over it. If you might want the old version back, commit first. For surgical changes, `stoner review` + `revise` is usually better than a redraft; for different results, sharpen the beat sheet or pass `--task` instructions.

### Why does my prologue score differently from the same prose inside a chapter?

Very short documents are damped: below 200 words the score scales down proportionally, because a couple of unlucky word choices in a fragment aren't enough evidence. Band edges (a 29.9 "touched up" vs. 30.1 "slop-adjacent") are also just bands — read the findings, not only the number.

### Is there a way to see exactly what the model sees?

`stoner canon pack` prints the canon digest verbatim as agents receive it. `.stoner/sessions/` holds complete agent transcripts — system prompt, every tool call, every result. Nothing sent on your behalf is hidden from you.

### Where do I report bugs or read the code?

The repository is <https://github.com/skeletor-js/st0n3r>. The architecture notes in `docs/planning/` and the research brief in `docs/research/` explain the design decisions the [Concepts](concepts.md) page summarizes.
