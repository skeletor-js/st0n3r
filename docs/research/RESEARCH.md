# Research Brief (run 1, 2026-07-07)

Compiled by a research subagent; informs slop lexicons, review-engine design,
and the codex adapter. Condensed; keep for cross-run reference.

## 1. NousResearch/autonovel

- Autonomous seed→novel pipeline (world, characters, outline, prose, revisions,
  export). **No license** — treat as all-rights-reserved; borrow *ideas*, not code.
- **Five co-evolving layers + canon DB**: voice.md (how we write), world.md,
  characters.md, outline.md (+ foreshadowing ledger), chapters/ch_NN.md, and
  cross-cutting canon.md ("what is true", 400+ hard facts before drafting).
- **Debts**: unresolved cross-layer propagation tracked in state.json
  (`trigger`, `affected[]`, `status`) — solves "bible drifted from prose".
- Pipeline: Foundation loop (exit on scores) → sequential chapter drafts
  (keep if score > 6, max 5 retries; extract new canon facts every chapter) →
  3–6 revision cycles → Opus whole-manuscript review loop (max 4 rounds) → export.
- **Evaluation insight**: absolute 1–10 LLM scoring collapses into a ~2-point
  band; comparative methods work — Elo tournaments between chapters,
  sentence-level STRONG/FINE/WEAK/CUT grading, adversarial "cut 500 words"
  editing (over-explain + redundancy dominate cuts), 4-persona reader panel
  (editor, genre reader, writer, first reader) acting on 3/4-consensus items.
- Two immune systems: mechanical regex checks (no LLM) + LLM judge. Anthropic-only.
- Weaknesses to improve on: single provider, no tests, fantasy-tuned, pacing ceiling.

### Fiction anti-patterns (autonovel ANTI-PATTERNS.md, empirical)

1. Over-explain (narrator restates what the scene showed) — #1 by volume
2. Triadic listing ("X. Y. Z.")
3. Negative-assertion stacking ("He did not…")
4. Cataloging-by-thinking ("He thought about X. He thought about Y.")
5. Simile crutch ("the way X did Y" 4–8×/chapter)
6. Section-break (`---`) as rhythm crutch
7. Paragraph-length uniformity
8. Beats landing exactly on outline schedule
9. Repeated chapter-ending structure
10. Balanced antithesis in dialogue ("not X, but Y") making all voices identical
11. Dialogue with no stumbles/interruptions
12. Summary where scene is needed (70%+ of a chapter should be in-scene)

## 2. Slop lexicons — sources & licensing

- **MIT-licensed, safe to adapt**: `blader/humanizer` SKILL.md and
  `conorbronsdon/avoid-ai-writing` SKILL.md (3-tier vocab; P0/P1/P2 severities;
  per-context tolerance; false-positive guardrails; "what NOT to flag").
- **Unlicensed (ideas only, no verbatim copy)**: autonovel ANTI-SLOP.md,
  CWOnline/AIWords, FareedKhan-dev ai_words.txt.
- Wikipedia "Signs of AI writing" (WikiProject AI Cleanup) categories:
  AI vocabulary; undue-significance/legacy inflation ("stands as a testament");
  superficial `-ing` analysis tails ("…, highlighting the importance of");
  promotional brochure-speak (nestled, vibrant, breathtaking); weasel
  attributions ("industry reports", "experts argue"); copula avoidance
  ("serves as" for "is"); negative parallelism ("not just X, but Y" — the
  single most overused LLM construction); rule-of-three; elegant variation
  (synonym cycling for one entity); false ranges; em-dash overuse (spaced
  em-dashes especially); bold/emoji/title-case tics; chatbot artifacts
  ("I hope this helps", "Great question!"); knowledge-cutoff hedges
  ("as of my last update"); compulsive summaries.
- Near-definitive fingerprints: `citeturn0search0`, `contentReference[oaicite`,
  `oai_citation`, `utm_source=chatgpt.com`, unfilled `[Your Name]` placeholders.
- Statistical tells: low burstiness (uniform sentence lengths), TTR < ~0.40
  (human 0.50–0.65), trigram over-representation, paragraph-reshuffle immunity.
- **Caveat baked into everything**: no single sign proves AI authorship;
  score clusters, damp short docs, avoid punishing non-native writers.

## 3. Prior art worth borrowing

- **Novelcrafter Codex**: entity auto-linking of every mention; "Progressions"
  (state changes over time). Gap: no post-generation consistency verification —
  exactly what our archivist does.
- **gptauthor**: synopsis review checkpoint before drafting = cheap human gate.
- **StoryCraftr**: CLI-first UX validated.
- **GOAT-Storytelling-Agent**: spec → plot → chapter → scene hierarchy; scenes
  are the unit LLMs draft coherently. Backend-agnostic.
- **Sudowrite Story Bible**: Braindump layer (unstructured intent) above
  genre/synopsis; layers generated from prior layers; validates our canon stack.

## 4. Codex CLI as backend

- `codex exec "<prompt>"` non-interactive; stdin via `-`; progress → stderr,
  **final message → stdout**; `--json` = JSONL events;
  `-o/--output-last-message <file>`; `--sandbox read-only|workspace-write|danger-full-access`;
  `-C <dir>`; `--skip-git-repo-check`; `--ephemeral`; `--profile <name>`.
- Auth: "Sign in with ChatGPT" OAuth (subscription-billed) or `model_providers`
  config with `env_key`. Config `~/.codex/config.toml`.
- Flags reconstructed from docs snippets — **feature-detect via
  `codex exec --help` at runtime** (our adapter does this).

## Design consequences adopted

1. Review engine gains autonovel-inspired passes: `adversarial` (what would an
   editor cut + why, categorized), `panel` (multi-persona consensus), and
   sentence-grade mode (STRONG/FINE/WEAK/CUT) instead of absolute 1–10 scores.
2. Archivist = our version of canon extraction + debts (conflicts ≈ debts).
3. Slop lexicon: original compilation informed by MIT sources + public-knowledge
   categories; fiction anti-patterns get dedicated deterministic analyzers where
   feasible; attribution in docs/CREDITS.md.
4. Codex adapter feature-detects flags; degrade gracefully.
