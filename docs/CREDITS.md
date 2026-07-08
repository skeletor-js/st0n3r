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

## Reader-simulation comps (public domain only)

The reader-simulation feature (`stoner readers bench`) compares your manuscript
against **comp** texts you supply yourself. No comp text ships in this package,
and there is no fetch-from-URL: `stoner readers comps add` ingests a *local*
file you already have.

Only ingest texts that are in the **public domain** (or that you otherwise hold
the right to use). st0r3r records the provenance you give it — title, author,
year, and source — but it cannot verify licensing; that responsibility is
yours.

Attribution convention for comps:

- Prefer authoritative public-domain sources (e.g. Project Gutenberg, Standard
  Ebooks, Wikisource, the Internet Archive) and record where the text came from
  in the `--source` field so the comp's `comp.json` carries a citable origin.
- Record the original author and year of first publication in `--author` and
  `--year`; public-domain status usually turns on the year and the author's
  death date in your jurisdiction.
- Comps live under `comps/<slug>/` in your project and are never packaged,
  published, or transmitted by st0n3r — they stay on your machine.
