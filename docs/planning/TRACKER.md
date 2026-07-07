# st0n3r — Build Tracker

State file for cross-run continuity. Update after every meaningful step.
Statuses: `todo` | `wip` | `done` | `blocked`.

_Last updated: 2026-07-07 (run 2) — next-ten build IN PROGRESS_

## Next-ten phase status (plans 2026-07-07-001..011)

Baseline at run 2 start: 227 passed + 1 skipped, ruff clean, mypy 33 pre-existing
errors (frozen as baseline; new code must not add errors).

| Wave | Item | Status | Notes |
|------|------|--------|-------|
| W0 | 011 U1 seam conventions + report kinds | wip | run 2 |
| W0 | 009 U1 snapshot store + chokepoint | wip | subagent, run 2 |
| W0 | 009 U2 route rewrite paths through chokepoint | wip | subagent, run 2 |
| W1 | 001 voice engine (all units) | todo | |
| W1 | 004 pacing instrumentation (all units) | todo | |
| W2 | 005 writers' room (all units) | todo | |
| W2 | 003 draft tournaments (all units) | todo | |
| W3 | 002 character interiority (all units) | todo | |
| W3 | 006 verisimilitude engine (all units) | todo | |
| W4 | 007 promise & motif ledger (all units) | todo | |
| W4 | 008 reader simulation (all units) | todo | |
| W5 | 010 production line (all units) | todo | |
| Wx | 009 U3-U8 (provenance/CLI/refactors) | todo | slots alongside waves |
| tail | 011 U4 cross-feature wiring | todo | |
| tail | 011 U5 UI panel consolidation | todo | |
| tail | 011 U6 docs reconciliation | todo | |
| tail | 011 U7 full-suite + novella proof run | todo | live steps need API key |

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
| 9 | final push | done | v0.2.0 |
| v2 | claude_code provider | done | bef8527; live-verified in-env |
| v2 | foundation pipeline (brainstorm/canon gen) | done | d047f9e, 13 tests; CLI wired |
| v2 | autonomous book mode + whole-book review | done | abeb1f4, 15 tests; CLI wired |
| v2 | UI v2 (Hearth design system) | done | in a0ed68c; both themes verified vs Hearth |
| v2 | proof of output (novella in examples/) | done | Sungrown: 15 ch / 25,241 words / 6 review rounds / 0 criticals |
| v2 | docs v2 reconciliation | done | 4c68596 + autonomous.md |

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

## Run journal (run 2 — next ten)

- **Run 2 (2026-07-07):** Session start on the next-ten build. Read plan 011 + 009 in
  full. Baseline verified: `uv sync --extra dev` then 227 passed / 1 skipped, ruff clean,
  mypy 33 pre-existing errors (baseline saved to scratchpad). Starting Wave 0:
  orchestrator does 011 U1 (report kinds + panel marker + register comment); subagent
  builds 009 U1-U2 (archaeology snapshot chokepoint).

## Run journal (run 1 — v1/v2)

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
- **Run 1 (FINAL):** v2 complete. Sungrown novella finished (15 ch, 25,241 words, 6 whole-book
  review rounds, 0 critical / 5 major remaining at plateau) and committed under examples/novella
  with its own README. Two live-run bugs found and fixed with tests (textproto format-retry;
  single-shot drafting for text-only providers) plus 4 doc-verification fixes. README rewritten
  for v0.2.0 with Hearth-UI screenshots over the real book. 227 tests green. Repo remains private.
  Remaining majors in the novella are honest reviewer findings a human author would triage in the
  UI — left in place as a realistic artifact.
