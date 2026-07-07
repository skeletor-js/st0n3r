# Getting started

st0n3r is a command-line harness for writing long-form fiction with AI models — one that treats your story bible as law, checks drafts for AI-flavored prose, and keeps a paper trail of everything it does. This page takes you from install to a drafted first chapter.

If you want the reasoning behind the workflow, read [Concepts](concepts.md) first. It's short.

## Install

st0n3r needs Python 3.11 or newer. The core package has no provider SDKs; install the extra for whatever you'll actually use:

```bash
pip install st0n3r                # core: slop detector, project structure, canon tools
pip install 'st0n3r[all]'         # everything: Anthropic + OpenAI SDKs + web UI
```

Individual extras: `st0n3r[anthropic]`, `st0n3r[openai]` (also covers OpenRouter, Together, Groq, Ollama, and any OpenAI-compatible endpoint), `st0n3r[ui]` for the dashboard.

From source, with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/skeletor-js/st0n3r
cd st0n3r
uv venv && uv pip install -e '.[all]'
```

Both `stoner` and `st0n3r` work as the command name. Check the install:

```
$ stoner --version
st0n3r 0.1.0
```

## API keys

Keys live in environment variables, never in config files. Set the one(s) for your provider:

| Provider | Environment variable |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Together | `TOGETHER_API_KEY` |
| Groq | `GROQ_API_KEY` |
| Ollama (local) | none needed |
| Codex CLI | none — run `codex login` once instead |

`stoner providers` shows what's configured and whether a key is visible:

```
$ stoner providers
│ anthropic  │ anthropic     │ -                      │ key set              │
│ codex      │ codex_cli     │ -                      │ codex CLI missing    │
│ ollama     │ openai_compat │ http://localhost:1143… │ no key needed        │
│ openai     │ openai_compat │ -                      │ OPENAI_API_KEY unset │
...
```

Custom endpoints, local models, and per-role model choice are covered in [Providers & models](providers.md).

## Start a project

```
$ stoner init mybook
Created project mybook at /home/you/mybook
  8 starter files written. Begin with canon/premise.md.
  Configure models in stoner.yaml, then try: stoner status
```

This scaffolds the opinionated layout:

```
mybook/
  stoner.yaml            # models, gates, review passes
  canon/                 # the story bible — single source of truth
    premise.md           # logline, themes, promise to the reader
    style.md             # voice, POV, tense, and the ## Banned list
    characters/          # one file per character (copy _template.md)
    world/               # places, factions, systems, items
    timeline.md          # dated events (the archivist appends rows)
    threads.md           # planted questions and Chekhov's guns
  outline/
    outline.md           # act structure and chapter map
    beats/ch-01.md       # per-chapter beat sheets — what the agent drafts from
  manuscript/            # the prose: ch-01.md, ch-02.md, ...
  notes/                 # scratch space; agents never read it unasked
  .stoner/               # memory, ledger, transcripts, reports
```

Now do the part no tool can do for you: fill in `canon/premise.md` and `canon/style.md`. Every template file opens with guidance comments. The more specific the style guide, the better everything downstream behaves — the writer agent reads it before every draft, and the reviewer holds chapters to it.

## Structure before prose (no API key needed)

These commands only create and arrange files, so they work offline:

```
$ stoner canon new character "Mara Quill"
Created canon/characters/mara-quill.md — fill in the facts and voice sections.

$ stoner chapter new 1 --title "The Locked Room" --pov Mara
Created manuscript/ch-01.md

$ stoner beats 2
Created outline/beats/ch-02.md — the writer agent drafts from this.
```

`stoner canon new world "The Undercroft"` does the same for places, factions, and systems. Character and world files put hard facts (age, eye color, allegiances) in YAML frontmatter and soft characterization (voice, wants, arc) in prose below — that split is what lets the harness catch contradictions later. See [Concepts](concepts.md#the-canon-method).

Already have prose? Pull it in as a chapter without retyping anything:

```
$ stoner chapter import 2 ~/drafts/old-chapter-two.md --title "The Archive" --pov Mara
Imported /home/you/drafts/old-chapter-two.md -> manuscript/ch-02.md
```

`import` refuses to overwrite an existing chapter, and `--status` lets you mark it `draft`, `revised`, or `final`.

## Write your first chapter

Fill in `outline/beats/ch-01.md` — goal, conflict, turn, exit state — then:

```
$ stoner write 1
ch-01 written — 2,314 words
slop: 31.4 -> 12.9 (1 revision loop(s), gate passed)
archivist: 4 facts applied, 0 conflict(s)
```

One command ran the full pipeline: the writer agent drafted from your beats, canon, and memory; the deterministic slop gate scored the draft and forced an automatic revision because it started too high; and the archivist read the finished chapter, updated character sheets, the timeline, and the rolling memory. Details in [Concepts](concepts.md#the-write-pipeline).

Useful flags: `--task "open mid-scene, no throat-clearing"` adds drafting instructions, `--model openai/gpt-5.2` swaps the drafting model for this run only, `--skip-archive` skips the canon sync.

## The command tour

**`stoner status`** — where the manuscript stands:

```
$ stoner status
mybook — /home/you/mybook
chapters: 2   words: 4,207
   draft: 2
┃ ch ┃ title           ┃ status ┃ pov  ┃ words ┃
│ 01 │ The Locked Room │ draft  │ Mara │ 2,314 │
│ 02 │ The Archive     │ draft  │ Mara │ 1,893 │
```

**`stoner slop <chapter|file|all>`** — the deterministic AI-tell detector. No API calls, no cost, works on anything:

```
$ stoner slop 2
Slop report: manuscript/ch-02.md
Score: 47.2 / 100 -- verdict: slop-adjacent
│ critical │ phrase │ 12 │ cliché phrase: 'little did she know' │
│ major    │ phrase │  8 │ cliché phrase: 'couldn't help but'   │
...
```

`--fmt markdown` or `--fmt json` for machine-readable output, `--save` to keep the report in `.stoner/reviews/`. How to read the score: [The slop detector](slop.md).

**`stoner review <n>`** — LLM critic passes (continuity, pacing, voice, line by default) that save a findings report. **`stoner revise <n>`** — applies accepted findings by rewriting the chapter. Both are covered in [Review & revise](review.md).

**`stoner archive <n>`** — previews what facts a chapter would add to canon; `--auto` applies the non-conflicting ones.

**`stoner canon list | show <path> | search <query> | pack`** — inspect the bible. `pack` prints the exact digest agents receive, which is the fastest way to see your book the way the model sees it.

**`stoner threads`** — the plot-thread table, so nothing planted gets silently dropped.

**`stoner ledger`** — every action the harness took, newest last:

```
$ stoner ledger --n 3
│ 07-07 15:10 │ chapter.import │ manuscript/ch-02.md │ {"source": "notes/old-ch2.md"} │
│ 07-07 15:10 │ slop.check     │ manuscript/ch-02.md │ {"score": 47.16}               │
```

**`stoner ui`** — a local dashboard for browsing the manuscript, triaging review findings, and seeing slop findings highlighted in the prose. See [The web UI](ui.md).

## Where to go next

- [Concepts](concepts.md) — why the harness is shaped this way
- [Providers & models](providers.md) — model strings, roles, custom endpoints, Codex CLI
- [The slop detector](slop.md) and [Review & revise](review.md) — the two quality systems
- [FAQ](faq.md) — nonfiction, existing manuscripts, costs, privacy

If a command fails with a one-line error and you need the full traceback, set `STONER_DEBUG=1`.
