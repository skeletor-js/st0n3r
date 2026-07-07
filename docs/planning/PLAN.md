# st0n3r — Build Plan

> An AI harness purpose-built for long-form writing. Inspired by NousResearch's
> autonovel; named for William Stoner (John Williams, *Stoner*) — quiet,
> stubborn, devoted craft — with a wink for those in the know.

## Vision

A comprehensive, agent-agnostic harness for writing fiction (and long-form prose
generally) with AI. Not a chat wrapper: an opinionated system that

1. **Structures knowledge** so an agent stays consistent across a 100k-word
   manuscript (the *canon* — characters, world, timeline, threads, style).
2. **Detects AI slop** deterministically (lexicon + syntactic patterns +
   statistical tells) and optionally with an LLM judge.
3. **Reviews and edits** with configurable critic passes (continuity, pacing,
   voice, line edit) that produce structured, actionable findings.
4. **Runs pipelines** (plan → draft → slop gate → review → revise → archive)
   rather than one-shot generations.
5. **Works with any model**: Anthropic API, any OpenAI-compatible endpoint
   (OpenAI, OpenRouter, Together, Ollama, vLLM…), or the Codex CLI via a
   ChatGPT subscription.

## Deliverables

- `stoner` Python package (src layout, typed, py311+) with CLI (`stoner` /
  `st0n3r` alias).
- Opinionated project structure created by `stoner init` (see ARCHITECTURE.md).
- Slop detector with bundled lexicon data + report format + score.
- Review engine with built-in critic passes and structured findings.
- Agent engine: provider-agnostic tool-calling loop with writing tools.
- Pipelines: `write`, `revise`, `review`, `slop`, `archive` (canon sync).
- Local web UI: `stoner ui` (FastAPI + single-file frontend, optional extra).
- Tests (pytest), CI-ready.
- Docs: README, docs/ (getting started, concepts, providers, slop, canon,
  pipelines, UI, FAQ).

## Phases

| # | Phase | Contents | Status |
|---|-------|----------|--------|
| 0 | Research | autonovel study, slop corpora, codex CLI driving | see TRACKER |
| 1 | Core scaffold | pyproject, types, config, project layout, provider base | see TRACKER |
| 2 | Providers | anthropic, openai-compat, codex-cli adapters + agent loop | see TRACKER |
| 3 | Knowledge system | canon store, archivist extraction, templates, memory | see TRACKER |
| 4 | Slop detector | lexicon data, analyzers, scoring, reports | see TRACKER |
| 5 | Review engine | critic passes, findings schema, revise pipeline | see TRACKER |
| 6 | CLI + pipelines | typer CLI, write/review/revise orchestration | see TRACKER |
| 7 | Web UI | FastAPI backend + dashboard | see TRACKER |
| 8 | QA | tests, reviewer subagent passes, fixes | see TRACKER |
| 9 | Docs & polish | README, docs/, examples, licensing | see TRACKER |

## Principles

- **Deterministic where possible.** Slop detection, canon diffing, and project
  management do not require an LLM. LLM passes are additive.
- **Plain files, plain formats.** Markdown + YAML frontmatter. A writer can use
  the project structure with zero AI. Everything is git-friendly.
- **Provider-agnostic core.** One `Provider` interface; nothing outside
  `providers/` imports a vendor SDK.
- **Ledger everything.** Every agent action appends to `.stoner/ledger.jsonl`.
- **Small dependency surface.** Core: pydantic, typer, rich, pyyaml, httpx.
  Vendors + UI are optional extras.
- **Open-source ready.** MIT license, clean attribution for any borrowed lists.

## Model usage during build (orchestration notes)

Orchestrator: this session. Subagents: sonnet for module builds, haiku for
mechanical tasks, opus for design/quality review. Fable subagents only at
medium-or-lower effort, sparingly.

## Out of scope (v1)

- Cloud sync / multi-user collaboration.
- Embeddings-based retrieval (design leaves room; keyword + structured canon
  first).
- Fine-tuning or local model serving.


## v2 — Autonomy push (goal update, 2026-07-07)

Owner feedback: think bigger. New deliverables:

| # | Item | Detail |
|---|------|--------|
| A | claude_code provider | Drive the `claude` CLI (`claude -p`) as a backend — subscription-based, mirrors codex_cli. Also = proof-of-output vehicle in this environment. |
| B | Foundation pipeline | `stoner brainstorm "<seed>"` -> premise + style; `stoner canon generate` -> characters/world/threads/outline/beats from premise, with an evaluate-and-iterate quality loop (autonovel Phase 1 analog). |
| C | Autonomous book mode | `stoner book` — chapter loop over the outline using the existing write pipeline, plus periodic whole-manuscript review (`stoner review book`, autonovel Opus-loop analog), auto-revision cycles, resumable state (.stoner/book-state.json), budget caps. |
| D | UI v2 | Rebuild the dashboard on the Hearth design system (skeletor-js/Hearth). |
| E | Proof of output | Generate a complete novella through the harness end-to-end with the claude_code provider; commit it under examples/. |
| F | Docs v2 | Reconcile all docs + README with the above. |
