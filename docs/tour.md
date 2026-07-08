# The tour

This is the long walk through everything the harness does — what each instrument is for, the design decision that makes it trustworthy, and the commands to reach for. For the reasoning behind the architecture, read [Principles](principles.md); for a hands-on start, read [Getting started](getting-started.md).

## How it's meant to be used

The workflow is a loop: build the bible, draft against gates, review and revise, then ship. Two entry points share the same files, so you can move between them freely — the harness driving the loop does not lock you out of it.

**Start a project.** `stoner init` scaffolds the opinionated layout: `stoner.yaml`, the `canon/` bible, `outline/beats/`, `manuscript/`, and `.stoner/` for memory, ledger, and reports.

**For a new book — brainstorm and foundation.** `stoner brainstorm "<seed>"` fills the premise and style templates from one line of idea. `stoner foundation` generates characters, world, threads, an outline, and a beat sheet per chapter, with an evaluate-iterate loop that names the single weakest element and regenerates just that. The two edit stops in the middle — sharpening premise/style, then the outline/beats — are where authoring actually happens.

**For an existing book — import.** `stoner chapter import` brings prose in without retyping, then `stoner archive N --auto` chapter by chapter builds canon and rolling memory around the manuscript you already have. No API key needed to import.

**Write with gates.** `stoner write N` runs the full pipeline: the writer agent drafts from your beats, canon, and memory; the deterministic slop gate scores the draft and auto-revises if it is over threshold; the archivist extracts new facts, diffs them against canon, and syncs memory. A voice-drift gate is available too, off by default.

**Review and revise.** `stoner review N` runs critic passes and saves a findings report. You triage — accept or dismiss each finding, in the terminal or the dashboard — and `stoner revise N` rewrites the chapter applying only what you accepted.

**Let it run, with checkpoints.** `stoner book` drafts every planned chapter through the write pipeline, runs whole-book review and revision rounds as it goes, and saves resumable state before every model call. Ctrl-C is safe; rerunning picks up where it stopped.

**Ship.** `stoner ship check` refuses on real not-dones (chapter gaps, unfired guns) and warns on the rest; then EPUB, PDF, DOCX, blurbs, and table-read audio.

Every command works on plain files. Nothing is hidden in a database; a writer can use the project structure with zero AI and it is still a good filing system.

## The instruments

The core loop — canon, memory, the write pipeline, the two immune systems — is the whole harness on its own. Ten instrument groups sit on top of it: measurement, revision, and shipping tools, each its own config block and ledger namespace, most of them off until you opt in. A project that only wants to draft chapters pays nothing for the rest. They share the core's one discipline: **deterministic checks may gate, model judgments only advise.**

| Capability | Command | Kind |
|---|---|---|
| AI-slop detection | `stoner slop` | deterministic **gate** |
| Voice drift | `stoner voice check` | deterministic (optional **gate**) |
| No-unfired-guns | `stoner promises check` | deterministic **gate** |
| Ship readiness | `stoner ship check` | deterministic **gate** |
| Knowledge-boundedness | `stoner cast check` | advisory (LLM-attributed) |
| Critic passes | `stoner review` | advisory |
| Whole-book review | `stoner review-book` | advisory |
| Pacing | `stoner pacing report` | advisory |
| Writers' Room | `stoner room session` | advisory |
| Verisimilitude | `stoner facts sweep` | advisory |
| Reader simulation | `stoner readers run` | advisory |
| Motif recurrence | `stoner motifs scan` | deterministic report |
| Draft blame / restore | `stoner drafts` | deterministic |
| Draft tournaments | `stoner tournament run` | comparative, human-confirmed |

---

## The canon method

The `canon/` directory is the book's law. Every character and world file is split, and the split is load-bearing: frontmatter holds facts the archivist can diff, the prose body holds characterization it never touches. `canon/timeline.md` holds dated events; `canon/threads.md` tracks every planted question and Chekhov's gun as `open`, `resolved`, or `abandoned`. After each chapter, the archivist extracts new facts, diffs them against canon, applies what is safe, and flags what contradicts.

**The design decision that makes it trustworthy:** conflicts are never auto-applied, whatever flags you pass. A fact that contradicts existing canon is surfaced for you to resolve by hand — the manuscript and the bible cannot silently drift apart. Agents never see canon raw; they get a **context pack** (`stoner canon pack` prints it verbatim) assembled by priority so lower-value sections drop cleanly under a budget instead of getting mangled mid-sentence.

```bash
stoner canon new character "Mara Quill"   # instantiate from the template (no API key)
stoner canon pack                          # exactly what the agent sees
stoner archive 3                           # preview the facts ch-03 would add
stoner threads                             # every open promise, tracked to resolution
```

See [Concepts](concepts.md).

---

## The slop detector

`stoner slop` is a deterministic analyzer for AI-flavored prose. Seven analyzers — lexicon, phrases, patterns, punctuation, repetition, rhythm, density — run over the chapter with about 900 lexicon entries and statistical rhythm checks, producing a 0–100 score banded clean / touched-up / slop-adjacent / slop. It is a hard gate in the write pipeline; no model, no cost, same answer every time.

**The design decision that makes it trustworthy:** clusters, not accusations. No single word convicts. Everything is measured as a *rate* per 1,000 words, the score only climbs when signals stack, and documents under 200 words are damped because a couple of unlucky word choices in a fragment are not evidence. It is a smoke alarm, not an editor — and it reads your own `## Banned` list from `canon/style.md`.

```bash
stoner slop 2                    # one chapter, scored with findings and spans
stoner slop all                  # every chapter
stoner slop 2 --fmt json --save  # machine-readable, saved to .stoner/reviews/
```

<p align="center"><img src="assets/cli-slop.png" alt="stoner slop CLI report" width="90%"></p>

See [The slop detector](slop.md).

---

## The review engine

`stoner review` runs LLM critic passes and saves the findings; `stoner revise` applies the ones you accept. Eight passes are available — `continuity`, `pacing`, `voice`, `line` (the default set), plus `logic`, `adversarial`, `panel`, and `grade`. `stoner review-book` reads the whole manuscript once as a literary critic and once as a professor of fiction, returning findings tagged by chapter.

**The design decision that makes it trustworthy:** the comparative passes never emit a number. `grade` labels every paragraph STRONG / FINE / WEAK / CUT; `adversarial` must cut exactly 400 words and classify each cut; `panel` convenes four reader personas and only promotes issues three of four raise independently. Findings quote the chapter verbatim and locate to spans, so the dashboard can highlight them and `revise` can apply only what you accepted.

| Command | What |
|---|---|
| `stoner review 3` | default passes, saved report |
| `stoner review 3 --passes adversarial,grade` | pick passes |
| `stoner revise 3` | rewrite applying accepted findings |
| `stoner review-book` | one whole-manuscript pass |

See [Review & revise](review.md).

---

## The voice fingerprint

`stoner voice` measures a body of exemplar prose into numbers — function-word frequencies, sentence-length distribution, punctuation rates, closed-class habits — and scores new writing as a *drift* against it, in the spirit of Burrows' Delta. Where slop measures distance from good prose in general, voice measures distance from *your* prose in particular. A chapter can pass the slop gate clean and still not sound like your book.

**The design decision that makes it trustworthy:** it is entirely deterministic — no model, same answer every time — which is exactly why it is allowed to act as a gate. Because voice is a matter of taste, that gate stays opt-in and default-off; a project that never runs `voice learn` never trips it.

```bash
stoner voice learn                # build the fingerprint from your exemplars
stoner voice learn --from-manuscript  # also learn from this book's revised chapters
stoner voice check 3              # drift of chapter 3 (0 = in voice)
stoner voice check all --save     # score every chapter, save a report
```

<p align="center"><img src="assets/ui-voice.png" alt="Voice drift in the dashboard" width="90%"></p>

See [Voice](voice.md).

---

## Pacing instruments

`stoner pacing report` reads the whole book as a shape: scene-versus-summary ratio per chapter, a tension curve across chapters, flatline runs, ending echoes, and POV whiplash. It mixes deterministic structural measures with optional per-chapter LLM instruments.

**The design decision that makes it trustworthy:** it is advisory on *both* kinds of input — it never gates, even on its deterministic measures, because pacing is about shape over distance and a single low-tension chapter is rarely worth acting on. A *run* of them is the signal, and the report surfaces runs worst-first.

```bash
stoner pacing report            # deterministic measures + LLM instruments
stoner pacing report --no-llm   # deterministic only, free and offline
```

<p align="center"><img src="assets/ui-pacing.png" alt="Pacing timeline in the dashboard" width="90%"></p>

See [Pacing](pacing.md).

---

## Draft tournaments

`stoner tournament` drafts N takes of a chapter from distinct angles, judges them in blind pairwise comparisons, and proposes a winner for a human to confirm. Votes train the project's taste model over time. It applies the same insight as the comparative review passes — "which of these two is better" is answerable where "rate this 1–10" is not.

**The design decision that makes it trustworthy:** the winner is a *proposal*, not a commit. Nothing lands in the manuscript until a human says so, via `stoner tournament apply` or after a blind A/B `vote`. Every take is snapshotted through draft archaeology, so nothing a tournament drafts is lost, and grafting the best "steals" from losing takes into the winner is one explicit model call, not a silent merge.

```bash
stoner tournament run 3 --takes 4   # 4 angled takes, judged blind
stoner tournament vote <id>         # blind A/B; trains the taste model
stoner tournament apply <id>        # write the confirmed winner (the human step)
stoner write 3 --tournament 4       # tournament as the drafting step, then the slop gate
```

<p align="center"><img src="assets/ui-tournaments.png" alt="Tournament standings and blind voting" width="90%"></p>

See [Tournaments](tournaments.md).

---

## The Writers' Room

A one-shot review forgets everything the moment it finishes. The Writers' Room is a persistent roster of editor personas that keep *notebooks* — a running opinion and a list of open items that survive across sessions — so an editor can flag "the middle still doesn't earn its length" in one pass and re-check that exact concern in the next. The default roster is four editors, each a persona wrapped around existing review passes.

**The design decision that makes it trustworthy:** the cost is bounded and predictable — one call per editor per assigned pass, one cross-examination per editor, plus at most one re-locate fallback and one comment follow-up — so the roster size is the only cost knob. It is advisory only; the room never gates. Margin comments anchor to a verbatim quote, so they survive edits that do not touch that span.

```bash
stoner room session 3             # run the roster over chapter 3
stoner room session --book        # a whole-book milestone session
stoner room notebook "Line Editor"   # one editor's running opinion + open items
stoner room comment 3 --quote "..." --text "..."   # pin a margin comment
```

<p align="center"><img src="assets/ui-room.png" alt="Writers' Room notebooks and comments" width="90%"></p>

See [Writers' Room](room.md).

---

## Cast: character interiority

`stoner cast` gives major characters a private interior state the writer agent can never see: what they know and *when* they learned it, what they want but won't say, the lies they maintain, the topics they refuse. From that comes a machine-checkable knowledge-boundedness check — a character acting on a fact before they could know it is a violation — and a scene-simulation loop where character agents collide by genuine information asymmetry.

**The design decision that makes it trustworthy:** canon is fed straight into the writer's prompt, which is exactly why canon cannot hold secrets — a secret that reaches the prompt gets narrated onto the page and subtext dies. So private state lives outside canon in `.stoner/cast/`, and the writer-facing context path never reads it. Subtext reaches the manuscript only through scene scripts *you* choose to feed `stoner write N --task`.

```bash
stoner cast init "Ruth Vann"      # seed a sheet from canon (no API key)
stoner cast update 3 --auto       # curate private state from chapter 3
stoner cast check 3               # flag anachronistic knowledge (advisory)
stoner cast scene --who ruth-vann,dale-kestner --chapter 3 --brief "..."
```

See [Cast](cast.md).

---

## The fact locker

Models hallucinate checkable real-world detail confidently. The fact locker stores sourced specifics — a fee, a statute, a form, a brand — as first-class canon artifacts under `canon/facts/`, fed to the writer through the same context-pack channel as the rest of canon. The verisimilitude sweep holds a chapter against them, flagging contradictions and unsourced confident claims.

**The design decision that makes it trustworthy:** `source_url` is mandatory — a fact with no source is dropped, never stored, so the harness never degrades into remembering detail it cannot attribute. Web research to *build* the locker is explicitly opt-in (`facts.enabled: true`), refuses inside autonomous book mode, and writes every network action to the ledger under `facts.*`. There is no silent fallback to unsourced facts; a provider that cannot search fails loudly.

```bash
stoner facts add --name "..." --claim "..." --source-url https://...   # offline
stoner facts research "Humboldt permit fees" --apply   # opt-in, ledgered, touches the web
stoner facts sweep 3              # verisimilitude pass on ch-03
```

See [Facts](facts.md).

---

## Promises & motifs

Two ledgers for what a book promises and what it echoes. **Promises** type the rows in `canon/threads.md` by kind — mystery, threat, want, image — and follow each from plant to payoff. **Motifs** track recurrence: a deterministic matrix of every registered motif across chapters, plus model-assisted candidate mining and an ending-rhymes-with-opening scan.

**The design decision that makes it trustworthy:** `stoner promises check` is a *deterministic* gate — it exits nonzero while any promise-kind row is still open, no model required, so it can gate a script or a release. Plain kind-less open threads only warn, because abandoning a thread is a legitimate authorial call. That is the same line `stoner ship check` draws between unfired guns (blockers) and open threads (warnings).

```bash
stoner promises plant t7 "who cut the fence" --kind mystery
stoner promises check             # exit 1 while any promise-kind row is open
stoner motifs scan                # deterministic recurrence matrix
stoner motifs rhyme               # does the ending rhyme with the opening?
```

See [Promises & motifs](motifs.md).

---

## Reader simulation

`stoner readers` runs a roster of reader personas over the finished manuscript and reports where enough of them independently react — a synthetic focus group with attention heatmaps. It can also benchmark the book blind against a public-domain comp, chapter-aligned, scored with the same Elo math the tournaments use.

**The design decision that makes it trustworthy:** agreement gating. A negative segment becomes a finding only when at least half the roster agrees on it — one bored reader is noise, half the panel losing the thread in the same place is signal. Comps are supplied by you and must be public domain; no comp text ships in the package and there is no fetch-from-URL.

```bash
stoner readers run --chapters 1-5   # the roster reads, then builds the heatmap
stoner readers heatmap              # attention heatmap + trouble segments
stoner readers bench middlemarch --chapters 1-3   # blind pairwise vs. a comp
```

<p align="center"><img src="assets/ui-readers.png" alt="Reader attention heatmap" width="90%"></p>

See [Readers](readers.md).

---

## Draft archaeology

Every rewrite the harness makes — draft, slop-revise, review-revise, tournament graft, refactor, restore — routes through a single snapshot chokepoint. So `stoner drafts` can show a chapter's whole history, attribute each current sentence to the rewrite that introduced it, restore any past state reversibly, and perform guarded structural refactors (merge, split, move-reveal, flip-POV). It is version control tuned for prose, and it records *why* each change happened.

**The design decision that makes it trustworthy:** `blame` is deterministic — it walks the snapshot chain, no model — and restore is never destructive, because the pre-restore state is itself snapshotted first. Pruning always keeps the original draft and every human-edit snapshot, and keeps content hashes so `blame` still resolves against pruned entries.

```bash
stoner drafts blame 3             # attribute each sentence to the rewrite that made it
stoner drafts diff 3 <a> <b>      # unified diff between snapshots
stoner drafts restore 3 <snap>    # reversible restore of a chapter or paragraph
stoner drafts refactor split 4 --at 12       # deterministic structural edits
```

See [Drafts](drafts.md).

---

## The production line

`stoner ship` turns a finished manuscript into things you can send: a readiness check, a dependency-free byte-reproducible EPUB3, a typeset trade-paperback PDF, a Shunn submission DOCX, marketing blurbs, and stitched table-read audio.

**The design decision that makes it trustworthy:** the network is never touched silently. The deterministic exports need no model and no network; blurbs and `--assist` audio call the writer model; a network TTS backend is reached only by explicit choice (`--backend openai`) and the default `say` backend is local. `ship check` refuses on genuine not-dones and records any `--allow-incomplete` override in the ledger.

```bash
stoner ship check                # blockers refuse, warnings inform
stoner ship epub                 # dependency-free, byte-reproducible EPUB3
stoner ship all                  # check + EPUB + PDF + DOCX
stoner ship audio 1              # stitched table-read WAV for ch-01
```

See [Ship](ship.md).

---

## The dashboard

`stoner ui` serves a local dashboard for the project in the current directory — a single static page plus a small JSON API, no build step, no CDN, no external requests. It reads straight from disk on every request, so edits from your editor, the CLI, or an agent mid-run show up on refresh. Nine panels: Manuscript, Canon, Reviews, Pacing, Tournaments, Writers' Room, Readers, Book, and Ledger. It is built on the [Hearth](https://github.com/skeletor-js/Hearth) design system, with warm light and dark themes.

The Manuscript panel runs live slop checks and paints every finding onto the prose, tinted by severity:

<p align="center"><img src="assets/ui-manuscript.png" alt="Manuscript view" width="49%"> <img src="assets/ui-slop.png" alt="Slop heatmap on real prose" width="49%"></p>

Reviews are triaged in place — accept or dismiss, then `stoner revise` applies what you accepted — with every report kind (review, slop, book, voice, pacing, cast, readers) routed to the right shape:

<p align="center"><img src="assets/ui-reviews-kinds.png" alt="Reviews panel, reports by kind" width="49%"> <img src="assets/ui-reviews.png" alt="Findings triage" width="49%"></p>

**Local-only by default:** the server binds to `127.0.0.1:8377`. There is no auth layer, so `--host 0.0.0.0` (which exposes triage writes to your network) is for trusted networks only. Model calls are never made from the UI — drafting, reviewing, and revising stay in the CLI.

See [The web UI](ui.md).

---

## Autonomous mode

Three commands take a project from a one-line idea to a reviewed manuscript. `brainstorm` builds the premise and style; `foundation` builds the rest of the bible with an evaluate-iterate loop; `book` drafts every planned chapter through the full write pipeline and runs whole-book review and revision rounds on top.

**The design decision that makes it trustworthy:** state is saved before every model call, so Ctrl-C, a crash, or a rate-limit death always leaves a resumable run — and a chapter you wrote by hand is respected on resume, not redrafted. Autonomy here means the harness drives the loop, not that you are locked out of it; the two human checkpoints (after brainstorm, after foundation) are what keep the result a book rather than a book-shaped object.

<p align="center"><img src="assets/ui-book.png" alt="Autonomous run progress" width="90%"></p>

```bash
stoner book --max-chapters 4 --max-minutes 60   # bounded, resumable run
stoner book --tournaments                       # slot chapters via draft tournament
stoner book                                      # resumes exactly where it stopped
```

See [Autonomous mode](autonomous.md).

---

## The provider layer

Every model is named by one string, `provider/model`. `anthropic`, `openai`, `openrouter`, `together`, `groq`, and `ollama` work out of the box with keys from env vars; any other OpenAI-compatible endpoint is one `providers:` block away. Each role — writer, reviewer, archivist, reader, researcher — takes its own model, so you can draft with one model and review with another.

**Run entirely through the local CLIs, no API key.** `codex/<model>` drives the Codex CLI under a ChatGPT subscription and `claude/<model>` drives the Claude Code CLI under a Claude subscription — each model call becomes one `codex exec` or `claude -p` invocation. These backends are text-only (no native tool-calling): drafting is single-shot from a context-complete system prompt, and everywhere else the engine degrades to a fenced-JSON tool protocol automatically. The Claude Code adapter disables every built-in tool and runs in a throwaway temp directory, so the CLI can never touch your project — st0n3r's own path-jailed tools are the only way a model writes to disk.

```yaml
# stoner.yaml
models:
  writer: anthropic/claude-sonnet-5
  reviewer: openrouter/deepseek/deepseek-chat
  archivist: anthropic/claude-haiku-4-5-20251001
```

```bash
stoner providers                 # which backends are configured and authed
stoner write 1 --model claude/claude-sonnet-5   # draft through the Claude CLI, no key
```

See [Providers & models](providers.md).
