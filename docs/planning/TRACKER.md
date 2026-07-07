# st0n3r — Build Tracker

State file for cross-run continuity. Update after every meaningful step.
Statuses: `todo` | `wip` | `done` | `blocked`.

_Last updated: 2026-07-07 (run 1)_

## Phase status

| Phase | Item | Status | Notes |
|-------|------|--------|-------|
| 0 | Research: autonovel + slop corpora + codex CLI | wip | subagent running |
| 1 | Planning docs (PLAN/ARCHITECTURE/TRACKER) | done | this commit |
| 1 | pyproject + package scaffold | done | commit 45e6b7e |
| 1 | types.py / config.py / project.py / ledger.py | done | commit 45e6b7e |
| 2 | providers: base + registry | done | commit 45e6b7e |
| 2 | providers: anthropic | wip | subagent A running |
| 2 | providers: openai_compat | wip | subagent A running |
| 2 | providers: codex_cli | wip | subagent A running |
| 2 | engine: agent loop + tools + prompts | wip | subagent A running |
| 3 | canon: store + templates + memory | wip | subagent C running |
| 3 | canon: archivist | wip | subagent C running |
| 4 | slop: lexicon data | wip | subagent B running; merge research when done |
| 4 | slop: analyzers + score + report | wip | subagent B running |
| 5 | review: passes + runner + revise | todo | |
| 6 | cli: init/status/write/review/slop/canon/revise/ui | todo | |
| 6 | pipelines: write pipeline | todo | |
| 7 | ui: server + static dashboard | todo | subagent D |
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
