# st0n3r — Build Tracker

State file for cross-run continuity. Update after every meaningful step.
Statuses: `todo` | `wip` | `done` | `blocked`.

_Last updated: 2026-07-07 (run 2) — next-ten build IN PROGRESS_

## Next-ten phase status (plans 2026-07-07-001..011)

Baseline at run 2 start: 227 passed + 1 skipped, ruff clean, mypy 33 pre-existing
errors (frozen as baseline; new code must not add errors).

| Wave | Item | Status | Notes |
|------|------|--------|-------|
| W0 | 011 U1 seam conventions + report kinds | done | c34f89a |
| W0 | 009 U1 snapshot store + chokepoint | done | 18b26cc, 15 tests |
| W0 | 009 U2 route rewrite paths through chokepoint | done | 18b26cc; deviation: reason kwarg passed via inspect.signature guard (test stubs lack it) |
| W1 | 001 voice engine (U1-U4, U6, U7 + gate helper; write.py gate deferred to wiring) | done | 66836b1, +58 tests (415 green) |
| W1 | 004 pacing instrumentation (U1-U7) | done | 781de54, +73 tests (357 green) |
| W2 | 005 writers' room (U1-U7) | done | ad1a147, +77 tests (492 green) |
| W2 | 003 draft tournaments (U1-U7; write --tournament + book slots deferred to wiring) | done | merged, +62 tests (554 green) |
| W3 | 002 character interiority (U1-U7; run_write cast hook deferred to wiring) | done | merged, +50 tests (604 green) |
| W3 | 006 verisimilitude engine (U1-U6) | done | c1d1960, +62 tests (666 green) |
| W4 | 007 promise & motif ledger (U1-U6, incl. book.py promises event) | done | d2961c5, +51 tests (717 green) |
| W4 | 008 reader simulation (U1-U7) | done | f4e26cc, +56 tests (773 green) |
| W5 | 010 production line (U1-U8) | done | 4103937, +57 tests (830 green) |
| Wx | 009 U3-U8 (provenance/CLI/refactors) | done | 5e4c4ff, +41 tests (284 green) |
| tail | 011 U4 cross-feature wiring | done | e7a4f80, +19 tests (850 green) |
| tail | 011 U5 UI panel consolidation | done | be50743 (+1 test, 831 green); manual light/dark screenshot check outstanding |
| tail | 011 U6 docs reconciliation | done | aefc275; command sweep verified by orchestrator (agent was stopped mid-run; work intact) |
| tail | README showcase overhaul (owner request) | wip | Opus subagent worktree; hero.jpg + 7 fresh UI screenshots staged |
| tail | 011 U7 full-suite + novella proof run | done | offline + live halves complete; 3 live-run bugs found+fixed (5f511e9, f7459f9, b9fe4ab); all feature namespaces ledgered |

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
- **Run 2 (cont):** Wave 0 landed. 011 U1 committed (c34f89a: kind field on
  SlopReport/ReviewReport/book payload, reviews API kind surfacing with legacy sniff
  fallback, index.html FEATURE PANELS markers, main.py register-order comment). 009
  U1-U2 committed (18b26cc: archaeology/snapshots.py chokepoint + all four rewrite
  paths routed; 15 tests). Suite 243 passed / 1 skipped; ruff clean; mypy at 33-error
  baseline. Orchestrator read ALL eleven plans in full. Deviation noted: revise_chapter
  reason kwarg forwarded only when the callable's signature accepts it, because
  test_pipeline/test_book stub revise_chapter without it and the plan requires existing
  tests unmodified. Launching Wave 1 (001 voice + 004 pacing) plus 009 U3-U8, three
  subagents in isolated git worktrees (shared-seam files: main.py/config.py/ui — merged
  by orchestrator per plan 011 seam contracts).
- **Run 2 (cont):** Voice + archaeology subagents each died once on transient API
  server errors; resumed with context intact. Owner directive: all future subagents run
  on Opus (model override) to control token cost. 009 U3-U8 landed and merged
  (5e4c4ff): provenance/blame, drafts CLI, renumber+integrity, merge/split,
  move-reveal/flip-pov, post-refactor verify; +41 tests -> 284 passed / 1 skipped,
  ruff clean, mypy 33-error baseline. Deviations recorded in the commit message
  (prune in snapshots.py; merge archives vacated drafts dir; renumber ledgers via
  refactor entries; missing-chapter table refs advisory). Voice + pacing still
  building.
- **Run 2 (cont):** Pacing (004 U1-U7) landed and merged (781de54): +73 tests ->
  357 passed / 1 skipped, ruff clean, mypy baseline. Its worktree branched from
  v0.2.0 (worktrees base at session start, NOT current main) so seam conflicts in
  config.py/main.py/ui/server.py were resolved at merge per contracts (pacing before
  archaeology in config; pacing register before drafts; explicit-kind _review_kind).
  NOTE for U5 UI pass: pacing rail item sits between Book and Ledger, not in the
  marked feature region — cosmetic, consolidation pass owns panel order. Pacing
  deviations recorded in its report: run_pacing(llm=None) defaults from config;
  pacing.report ledgers on --no-save too; e2e provider injection via run_pacing.
  Voice still building.
- **Run 2 (cont):** Voice (001) landed and merged (66836b1): +58 tests -> 415 passed
  / 1 skipped, ruff clean, mypy baseline. Calibration separations hold both directions
  (A-fingerprint: A-text 0.0 / B-text 48.7; B-fingerprint: B <15 / A 30.9).
  voice_gate_check helper ready for wiring stage; pipelines untouched. WAVE 1
  COMPLETE.
- **Run 2 (PAUSED by owner):** Owner asked to pause after in-flight agents merged;
  Wave 2 was NOT launched. State at pause: main at Wave 1 complete, 415 passed /
  1 skipped, ruff clean, mypy 33-error baseline, working tree clean (only uv.lock
  untracked). Next session: launch Wave 2 (005 + 003) as Opus subagents in isolated
  worktrees; tell each agent to `git merge --ff-only main` first (worktrees base at
  session-start HEAD, v0.2.0 — all three Wave-1 agents hit this). Wave 2 notes:
  room config field goes after pacing / before archaeology; tournament field between
  voice and pacing; tournament apply/graft writes through the archaeology chokepoint
  with reason tournament-graft; room DIRS entries before .stoner/drafts; register
  lines per the main.py comment order. Then Waves 3-5, wiring (011 U4: voice gate in
  run_write via voice_gate_check, cast hook, write --tournament, book slots, room
  roster passes, ship promises, readers ratings), UI consolidation (fix pacing rail
  item placement), docs reconciliation, novella proof run (live steps need an API
  key; scripted-provider steps run without).
- **Run 3 (2026-07-07):** Resumed. Baseline re-verified at 88caf80: 415 passed /
  1 skipped, ruff clean, mypy 33. Wave 2 launched: 005 room + 003 tournaments as
  Opus subagents in worktrees, each instructed to ff to main first. Tournament
  apply routes through the archaeology chokepoint with reason tournament-graft
  (integration-plan override recorded in the subagent brief).
- **Run 3 (cont):** Usage-limit outage killed both Wave 2 agents mid-run; resumed
  from transcripts, worktrees intact (both had correctly ff'd to main first).
  Writers' room (005) landed and merged clean (ad1a147): +77 tests -> 492 passed /
  1 skipped, ruff clean, mypy baseline. Deviations: book-scope relocation is
  deterministic-only; chapter relocation batches all editors into the one fallback
  call (R17 bound). Tournaments (003) still building.
- **Run 3 (cont):** Tournaments (003) landed and merged: +62 tests -> 554 passed /
  1 skipped, ruff clean, mypy baseline, merged index.html JS node-checked. Seam
  conflicts with the room merge resolved (tournament register between voice and
  pacing; both endpoint blocks kept in ui/server.py; tournaments rail/panel/JS
  before room's). Tournament deviations: restores also via chokepoint (reason
  tournament-restore) to keep the drafts manifest in sync; Swiss pairing
  backtracks; drift check vs restored_sha; 409 on closed voting. WAVE 2 COMPLETE.
  Wave 3 launched: 002 interiority + 006 verisimilitude, Opus subagents in
  worktrees (cast config/register between voice and tournament; facts between
  room and archaeology/drafts; cast reports kind "cast"; 006 owns the only
  pre-reconciliation faq.md edit).
- **Run 3 (cont):** Interiority (002) landed and merged clean: +50 tests -> 604
  passed / 1 skipped, ruff clean, mypy baseline. Privacy sentinel regression in
  place; boundedness core pure; curator mirrors archivist; scene sim isolates
  sheets per captured-request assertions. Deviations: sheet digests are module
  functions in store.py; run_write cast hook deferred to wiring (run_cast_update
  already no-ops without sheets — wiring only adds the auto_update gate +
  degrade-note wrapper). Verisimilitude (006) still building.
- **Run 3 (cont):** Verisimilitude (006) landed and merged clean (c1d1960): +62
  tests -> 666 passed / 1 skipped, ruff clean, mypy baseline. WebSearchSpec seam
  in types/providers (anthropic native, claude_code tool-allowlist, others refuse);
  canon/facts locker + context_pack Facts section; ledgered web_fetch; opt-in
  research pipeline with book-context refusal; verisimilitude pass registered
  bottom-of-module; faq.md amended (the one sanctioned pre-reconciliation edit).
  Deviations: MockTransport test seams; FactsDisabledError. WAVE 3 COMPLETE.
  Wave 4 launched: 007 motifs/promises + 008 readers, Opus subagents in worktrees
  (motifs config/register after facts before archaeology/drafts; readers likewise —
  merge order resolved by orchestrator; motif reports kind "motifs", readers
  mirror reports kind "readers"; 007 owns the book.py promises event in-plan).
- **Run 3 (cont):** Promise & motif ledger (007) landed and merged clean (d2961c5):
  +51 tests -> 717 passed / 1 skipped, ruff clean, mypy baseline. Zero plan
  deviations; canon/promises.md deliberately unused per KTD 1. Readers (008)
  still building.
- **Run 3 (cont):** Readers (008) landed and merged (f4e26cc): +56 tests -> 773
  passed / 1 skipped, ruff clean, mypy baseline. Seam conflicts vs motifs merge
  resolved (MotifsConfig before ReadersConfig; motifs register before readers).
  Deviations: readers run auto-builds heatmap; deterministic finding ids
  rh-<ch>-<seg>; feature-local RunState bench fields. WAVE 4 COMPLETE. Wave 5
  launched: 010 production line, Opus subagent worktree (ship config after
  archaeology; ship register last; export extra with reportlab/python-docx;
  CanonStore.promises() live so unfired-guns blocker activates in-plan).
- **Run 3 (cont):** Production line (010) landed and merged (4103937): +57 tests
  -> 830 passed / 1 skipped, ruff clean, mypy baseline; pyproject gained the
  export extra (reportlab, python-docx; all grew export — `uv sync --extra dev
  --extra export` is now the dev setup). Agent live-proved ship
  check/all/voices/audio on a corpus COPY with the real say backend (cache
  resume verified); examples/novella untouched. Deviations: corpus uses straight
  quotes (plan said curly — parser handles both); PDF asserts raw bytes instead
  of adding pypdf; format orchestration in format modules. ALL TEN FEATURES
  LANDED. Tail launched: 011 U4 wiring + U5 UI consolidation as parallel Opus
  worktree agents (disjoint: pipelines/CLI vs ui/). U4 notes: write --tournament
  = human consent to apply; book slots behind --tournaments flag (default off);
  voice gate + cast hook per plan scenarios.
- **Run 3 (cont):** UI consolidation (011 U5) landed and merged (be50743): rail/pane
  order fixed (pacing into the feature region), helpers deduped (sevSort,
  findingRowHtml, reviewFindingsSection), undefined --line token fixed, reviews
  triage routes all seven kinds with a mixed-kinds test; 831 passed / 1 skipped.
  Outstanding follow-up: manual stoner ui screenshot check in both themes on
  examples/novella (no browser automation in worktrees). Wiring (011 U4) still
  building.
- **Run 3 (cont):** Wiring (011 U4) landed and merged (e7a4f80): all eight items,
  +19 tests -> 850 passed / 1 skipped; hooks default-off byte-identical; write
  --tournament = apply consent; book --tournaments flag. Docs reconciliation (U6)
  launched as an Opus worktree agent. Proof run (U7, offline half) executed on
  scratchpad COPIES of examples/novella (repo copy verified untouched):
  status/voice learn+check(+all --save)/pacing --no-llm/promises
  plant->check(1)->payoff->check(0) + --strict(1)/motifs add+scan+rhyme
  --no-judge/drafts snapshot+list+blame+refactor split(9->16 renumber, integrity
  clean)+verify/ship check(warnings only)+all(epub/pdf/docx)+voices+audio ch-08
  dialogue-only via real say (13.8MB, 55 chunks)/cast init+show/room
  comment+comments/facts add+list+show/readers personas+comps add/slop. Ledger on
  the proof copy covers every feature namespace EXCEPT tournament.* (needs a
  model). TWO live-run bugs found and fixed with regression tests: (1) motifs add
  crashed on legacy projects lacking canon/motifs.md — now bootstraps from the
  template (5f511e9); (2) legacy book-*.json reports sniffed as slop in the
  reviews listing — verdict+overall now sniffs book (f7459f9). UI screenshot
  check done in BOTH themes via preview browser (dark: rail order + manuscript;
  light: reviews kind chips incl. BOOK REVIEW on legacy files, pacing empty state
  and populated timeline) — closes the U5 follow-up. Suite at 852 passed / 1
  skipped.
- **Run 3 (cont): live proof unblocked WITHOUT an API key** — owner correction:
  st0n3r's own claude_code/codex_cli providers are the proof vehicle (the v2
  build did exactly this). Proof copy reconfigured: writer/reviewer/researcher
  = claude/opus (Claude Code CLI, Opus 4.8), archivist/reader = claude/haiku;
  facts.enabled true (max_searches 3). Smoke `motifs rhyme` with a live opus
  judge succeeded (~35s/call). Live proof sequence delegated to an Opus
  subagent: room session, tournament run/vote/apply (covers the missing
  tournament.* namespace), cast update/check, facts research (the one
  web-touching step), motifs candidates, readers run/heatmap/bench, one
  model-assisted refactor + verification, ship blurbs, ship audio --assist,
  pacing with LLM judge + cache-hit re-run. Also queued per owner: Opus README
  overhaul (hero banner at docs/assets/hero.jpg, badges, feature breakdowns
  with fresh screenshots, roadmap) — runs AFTER the docs-reconciliation agent
  merges; screenshots captured after the live proof so panels have real data
  (chromium installed in scratchpad for playwright capture).
- **Run 3 (cont): LIVE PROOF COMPLETE (all 11 steps, via claude CLI provider,
  zero API keys).** Highlights: room session 4 editors/53 findings answered the
  margin comment; tournament run/vote/apply/graft end-to-end (tournament.*
  ledgered — every feature namespace now covered); cast update+check; facts
  research pulled 4 real sourced facts off the web (northcoastjournal,
  lostcoastoutpost, kymkemp URLs); readers run + heatmap + bench (novella beat
  the comp 12-0); flip-pov refactor whose advisory verification correctly
  flagged the flip as a canon violation; ship blurbs; say-backend audio with
  --assist (no-op: zero UNKNOWN lines); pacing LLM judge across 16 chapters
  with cache-assisted resume (interrupted run finished from cache; re-run made
  1 fresh call — only the repeatably unjudgeable ch-10). THIRD live-run bug
  found+fixed (b9fe4ab): text-only tournament takes captured tool-call XML/
  preamble/log scaffolding because the angle task mentioned write_chapter on
  the single-shot path; task contract now branches on provider.supports_tools.
  Suite 853 passed / 1 skipped, ruff clean, mypy baseline.
- **Live-proof follow-ups (recorded, not blocking):** (1) cast update prints
  "want_shift: want_shift" — value echoes field name, cosmetic extraction/
  display nit in the curator output path; (2) haiku pacing judge repeatably
  fails to emit parseable JSON on ch-10 (degrades to unjudged+info finding as
  designed; unjudged results are deliberately not cached, so a full cache hit
  needs every chapter judgeable — document or accept); (3) guard interaction:
  tournament apply resets chapter status to draft, which then blocks ship
  audio without --allow-incomplete — correct per plans, worth a docs note;
  (4) room session (~16 min) and full LLM pacing (~15-20 min) exceed 10-minute
  budgets in one shot on CLI providers — resumable by design.
- **ORCHESTRATION NOTE (self):** shell cwd silently persisted inside the docs
  agent's worktree during a status check; relative-path git/pytest commands ran
  against the worktree while Edit-tool absolute paths hit main, producing a
  phantom "lost commits" scare (main was always intact; reflog + git -C
  untangled it). Accidental test append into the docs worktree was reverted.
  Rule: always `git -C /Users/jordanstella/GitHub/st0n3r` or cd explicitly at
  the start of every shell command chain.

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
