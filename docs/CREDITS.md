# Credits & Attribution

st0n3r stands on the shoulders of prior work:

- **[NousResearch/autonovel](https://github.com/NousResearch/autonovel)** — the
  direct inspiration for this project. Ideas we adapted (no code copied; the
  repo is unlicensed): layered story-bible architecture, canon-as-fact-database,
  cross-layer consistency "debts", comparative evaluation over absolute scoring,
  adversarial editing, and multi-persona reader panels.
- **[blader/humanizer](https://github.com/blader/humanizer)** (MIT) and
  **[conorbronsdon/avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing)**
  (MIT) — tiered AI-vocabulary catalogs and false-positive guardrails that
  informed our slop lexicons.
- **Wikipedia:WikiProject AI Cleanup — "Signs of AI writing"** — the broadest
  public catalog of AI-writing tells; several analyzer categories trace to it.
- **sam-paech/slop-forensics** and the **EQ-Bench slop score** — the statistical
  framing (over-represented vocabulary, burstiness) behind our rhythm and
  lexicon analyzers.
- **Burrows' Delta** (John Burrows, *"Delta: a Measure of Stylistic
  Difference"*, 2002) — the function-word z-distance framing behind the voice
  engine's drift scoring. The shipped function-word list
  (`src/stoner/voice/data/function_words.yaml`) is hand-assembled from the
  standard closed-class inventories of English grammar — no external corpus,
  frequency list, or copyrighted word list was copied.
- ***Stoner* by John Williams** — the name. A book about doing the work with
  quiet devotion, whatever the outcome. (And yes, the other reading of the
  name is intentional too.)
