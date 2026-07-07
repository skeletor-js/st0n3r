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
| 7 | ui: server + static dashboard | done | 30 tests; in 903f562 |
| 8 | tests: unit + smoke | done | 168 tests green |
| 8 | reviewer subagent QA pass + fixes | done | 18/18 confirmed findings fixed, 11 regression tests |
| 9 | README + docs/ | wip | docs agent + orchestrator README |
| 9 | LICENSE (MIT) + attribution | done | LICENSE + docs/CREDITS.md |
| 9 | final push | todo | |
| v2 | claude_code provider | done | bef8527; live-verified in-env |
| v2 | foundation pipeline (brainstorm/canon gen) | done | d047f9e, 13 tests; CLI wired |
| v2 | autonomous book mode + whole-book review | done | abeb1f4, 15 tests; CLI wired |
| v2 | UI v2 (Hearth design system) | done | in a0ed68c; both themes verified vs Hearth |
| v2 | proof of output (novella in examples/) | wip | run in progress: brainstorm done, foundation running |
| v2 | docs v2 reconciliation | todo | |

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
- **Run 1 (cont):** UI landed (30 tests). All modules built; 168 tests green. CLI smoke-tested (init/status/providers). Launching QA workflow (find->verify) + docs agent + README.
- **Run 1 (cont):** QA workflow done: 5 reviewers (opus/sonnet) -> 36 adversarial verifications; 18 findings confirmed, all fixed:
  2 critical (revise empty-body guard; archivist nested-frontmatter diffing), XSS escapes in UI tables, --model scoped to writer role,
  loop-guard nudge now a user message, usage counted on parse failure, timeline idempotency, write_chapter frontmatter merge,
  slop_check agent tool added, ch>=1000 regex, keyless CLI creation (chapter new/import, canon new, beats), archivist.md rewritten
  truthful, unused reviewer.md removed. 179 tests green. README got screenshots (docs/assets) + demo-driven fixes (rich double-print, UI word count).
- **Run 1 (cont):** GOAL EXPANDED by owner: autonomy, proof of output, Hearth UI. claude CLI confirmed working in-env (claude_code provider = proof vehicle). Hearth cloned to scratchpad. Launching wave 3.
- **Run 1 (cont):** claude_code provider landed + live smoke (PONG, real usage). Banned terms wired into detector. UI v2 agent launched with Hearth design brief. Foundation + book agents still running. Proof-of-output plan: novella on a legacy cannabis farm during legalization (Stoner-quiet, owner's industry), writer=claude/sonnet, archivist/reviewer=claude/haiku, under examples/.
- **Run 1 (cont):** Foundation + book mode + Hearth UI all landed and wired (224 tests). Proof run started: examples/novella (Ruth Vann, Humboldt legalization novella), writer/reviewer=claude/sonnet, archivist=claude/haiku. Brainstorm complete; foundation generating.
