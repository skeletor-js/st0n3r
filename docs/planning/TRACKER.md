# st0n3r — Build Tracker

State file for cross-run continuity. Update after every meaningful step.
Statuses: `todo` | `wip` | `done` | `blocked`.

_Last updated: 2026-07-07 (run 1)_

## Phase status

| Phase | Item | Status | Notes |
|-------|------|--------|-------|
| 0 | Research: autonovel + slop corpora + codex CLI | done | docs/research/RESEARCH.md |
| 1 | Planning docs (PLAN/ARCHITECTURE/TRACKER) | done | this commit |
| 1 | pyproject + package scaffold | done | commit 45e6b7e |
| 1 | types.py / config.py / project.py / ledger.py | done | commit 45e6b7e |
| 2 | providers: base + registry | done | commit 45e6b7e |
| 2 | providers: anthropic | done | commit 4de3131, 42 tests |
| 2 | providers: openai_compat | done | commit 4de3131, 42 tests |
| 2 | providers: codex_cli | done | commit 4de3131, 42 tests |
| 2 | engine: agent loop + tools + prompts | done | commit 4de3131, 42 tests |
| 3 | canon: store + templates + memory | done | 37 tests; in 86149a8 |
| 3 | canon: archivist | done | apply_updates(auto=False)=dry-run preview |
| 4 | slop: lexicon data | done | 158 words / 218 phrases / 46 patterns |
| 4 | slop: analyzers + score + report | done | 26 tests, commit 4d3dbc8 |
| 5 | review: passes + runner + revise | done | commit f64c60f, 19 tests |
| 6 | cli: init/status/write/review/slop/canon/revise/ui | done | commit 3e38db6, 16 tests |
| 6 | pipelines: write pipeline | done | commit 3e38db6 |
| 7 | ui: server + static dashboard | wip | subagent E running |
| 8 | tests: unit + smoke | todo | alongside modules |
| 8 | reviewer subagent QA pass + fixes | todo | |
| 9 | README + docs/ | todo | |
| 9 | LICENSE (MIT) + attribution | todo | |
| 9 | final push | todo | |

## Decisions log

- 2026-07-07: Python 3.11+, src layout, uv for dev. CLI name `stoner`.
- 2026-07-07: Deps — core: pydantic v2, typer, rich, pyyaml, httpx.
  Extras: `anthropic`, `openai`, `ui` (fastapi+uvicorn), `all`.
- 2026-07-07: Codex backend = shell out to `codex exec` (text-only v1);
  engine has structured-prompt fallback for non-tool providers.
- 2026-07-07: MIT license, keep repo private until owner opens it.
- 2026-07-07: Subagents must NOT commit; orchestrator commits. Disjoint paths
  per subagent to avoid conflicts.

## Open questions / risks

- Codex CLI flags may have drifted vs research; codex adapter must fail soft
  with a clear message if binary missing.
- Slop lexicon licensing: only copy lists that are CC/PD or rewrite ours from
  multiple sources (facts/wordlists are fine; keep attribution in
  docs/CREDITS.md).

## Run journal

- **Run 1 (2026-07-07):** Session start. Repo empty. Wrote planning docs,
  launched research subagent. Next: scaffold package + core modules, then
  parallel subagent builds (A: providers+engine, B: slop, C: canon, D: ui).
- **Run 1 (cont):** Repo confirmed private on GitHub. Wave-1 subagents launched (A: providers+engine, B: slop, C: canon).
- **Run 1 (cont):** Subagent A landed: providers (anthropic/openai_compat/codex_cli) + engine (agent loop, tools, textproto, prompts), 42 tests green. Committed 4de3131.
- **Run 1 (cont):** Subagent C landed: canon store/templates/memory/archivist, 37 tests. Note: archivist apply_updates(auto=False) is a dry-run preview; new_entities surfaced for triage, never auto-created.
- **Run 1 (cont):** Slop detector landed (26 tests). Orchestrator wrote CLI + write pipeline (16 tests). Wave 2 running: D=review engine, E=web UI. 119 tests green outside in-flight scopes.
- **Run 1 (cont):** Review engine landed (8 passes, 19 tests, 168 repo-wide green). UI subagent still running.
