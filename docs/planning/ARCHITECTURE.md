# st0n3r — Architecture

## Package layout (repo)

```
st0n3r/
  pyproject.toml
  src/stoner/
    __init__.py
    cli/                 # typer app; one module per command group
    config.py            # StonerConfig — loads stoner.yaml + env
    project.py           # WritingProject — paths, discovery, validation
    types.py             # shared pydantic models (Message, ToolCall, Finding…)
    ledger.py            # append-only JSONL action log
    providers/
      base.py            # Provider ABC: complete(), stream(), supports_tools
      anthropic.py
      openai_compat.py   # OpenAI + any base_url-compatible endpoint
      codex_cli.py       # shells out to `codex exec`
      registry.py        # name -> provider factory, model string parsing
    engine/
      agent.py           # tool-calling loop, transcripts, budgets
      tools.py           # writing tools exposed to the agent
      prompts/           # system prompts as .md templates
    canon/
      store.py           # CanonStore: read/query/update canon files
      archivist.py       # fact extraction + conflict detection vs canon
      templates/         # files written by `stoner init`
      memory.py          # rolling chapter summaries (memory.json)
    slop/
      lexicon.py         # loads data/*.yaml lexicons
      analyzers.py       # word/phrase/construction/statistics analyzers
      score.py           # weighting -> 0..100 score
      report.py          # SlopReport rendering (rich/markdown/json)
      data/              # lexicon YAML (words, phrases, patterns)
    review/
      passes.py          # built-in critic passes (continuity, pacing, voice…)
      runner.py          # runs passes -> Finding[]
      revise.py          # apply findings via agent
    pipelines/
      write.py           # plan -> draft -> gate -> review -> revise -> archive
      common.py
    ui/
      server.py          # FastAPI app (optional extra)
      static/index.html  # single-file dashboard
  tests/
  docs/
```

## Writing-project layout (`stoner init` output — the opinionated part)

```
mybook/
  stoner.yaml            # provider/model, style prefs, gates, pipeline config
  canon/                 # SINGLE SOURCE OF TRUTH — the bible
    premise.md           # logline, themes, promise to the reader
    style.md             # voice, POV, tense, banned words, comps
    characters/<slug>.md # YAML frontmatter (facts) + prose (voice, wants)
    world/<slug>.md      # settings, factions, rules/magic/tech
    timeline.md          # dated events table
    threads.md           # open/closed plot threads w/ status
  outline/
    outline.md           # act/chapter map
    beats/ch-NN.md       # per-chapter beat sheets
  manuscript/
    ch-NN.md             # the prose; YAML frontmatter: status, pov, words
  notes/                 # freeform scratch, never read by agent unless asked
  .stoner/
    memory.json          # rolling summaries per chapter + book-so-far
    ledger.jsonl         # every harness action
    sessions/            # full agent transcripts
    reviews/             # review + slop reports (json + md)
```

Conventions:
- Chapter files are `ch-01.md`, `ch-02.md`… frontmatter keys: `title`,
  `status: outline|draft|revised|final`, `pov`, `words` (maintained by harness).
- Character/world files: frontmatter holds *hard facts* (age, eye color,
  allegiances) the archivist can diff; body prose holds soft characterization.
- `threads.md` rows: `id, name, opened_in, status, resolved_in, notes`.
- The agent NEVER reads whole manuscripts into context; it gets canon +
  memory summaries + the neighboring chapters it asks for via tools.

## Provider abstraction

```python
class Provider(ABC):
    name: str
    def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    # CompletionRequest: model, system, messages[], tools[], max_tokens, temperature
    # CompletionResponse: text, tool_calls[], stop_reason, usage
```

- Model strings: `anthropic/claude-sonnet-5`, `openai/gpt-5.2`,
  `openrouter/<vendor/model>` (openai-compat w/ base_url), `codex/gpt-5-codex`.
- `openai_compat` takes `base_url` + `api_key` from config/env; that one class
  covers OpenAI, OpenRouter, Together, Groq, Ollama, vLLM, LM Studio.
- `codex_cli` runs `codex exec --json -` style invocations; tools are emulated
  by the harness (codex backend is text-only in v1; the engine degrades to
  structured-prompt mode when `supports_tools` is False).
- Tool-calling degradation: when a provider lacks native tools, the engine uses
  a JSON-in-fenced-block protocol with retry-on-parse-failure.

## Agent engine

- `Agent.run(task, tools, budget)` — loop: provider call → execute tool calls →
  append → until stop/budget. Transcript saved to `.stoner/sessions/`.
- Budgets: max turns, max tokens, wall clock.
- Tools (all scoped to the project root, path-jail enforced):
  `read_chapter`, `write_chapter` (frontmatter-merging), `list_project`, `search_text`,
  `query_canon`, `update_canon`, `read_outline`, `update_beats`,
  `get_memory`, `slop_check` (self-serve), `word_count`.

## Slop detector

Deterministic analyzers over a document → `SlopFinding[]` → weighted score.

| Analyzer | Examples |
|----------|----------|
| word lexicon | delve, tapestry, testament, palpable, myriad… (severity-tiered) |
| phrase lexicon | "couldn't help but", "little did X know", "a testament to" |
| constructions (regex) | "not just X, but Y"; "It's not about X, it's about Y"; participial openers; "eyes" clichés |
| punctuation | em-dash density, semicolon abuse, ellipsis abuse |
| repetition | repeated 2-4 grams in window; word echo; sentence-start echo |
| rhythm | sentence-length variance (burstiness), paragraph uniformity |
| density | adverb (-ly) rate, adjective stacking, filter words (felt, saw, seemed) |

Score 0 (clean) → 100 (pure slop); per-category subscores; findings carry
line/col spans for UI heatmap. Optional `--judge` adds an LLM pass for
higher-order slop (hollow profundity, symmetrical sentimentality).

## Review engine

Critic passes (each = prompt template + input assembly + Finding schema):
`continuity` (vs canon+memory), `pacing`, `voice` (vs style.md + character
sheets), `line` (prose quality), `logic`, `slop-judge`. Output: `Finding{pass,
severity, location, quote, issue, suggestion}` → saved to `.stoner/reviews/`,
rendered by CLI + UI. `stoner revise` feeds accepted findings to the agent.

## Pipelines

`stoner write ch-05`:
1. assemble context (canon + memory + beats + tail of ch-04)
2. plan scenes (if no beat sheet) → draft → **slop gate** (fail ⇒ auto-revise,
   max N loops) → critic passes → revise → archivist (extract facts, diff
   canon, update memory + threads + frontmatter) → ledger.

Every stage is also callable standalone.

## Web UI (`stoner ui`)

FastAPI + one static HTML file (no build step, no CDN). Panels: manuscript
browser w/ slop heatmap, canon browser, review findings triage
(accept/dismiss), pipeline runner with live log (SSE), ledger view. Local-only
by default (127.0.0.1).

## Config resolution

`stoner.yaml` (project) ← env vars (`STONER_*`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`) ← CLI flags. Fail with actionable
messages.
