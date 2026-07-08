# Cast: character interiority agents

`stoner cast` gives major characters a private interior state the writer agent can never see: what they know (and *when* they learned it), what they want but won't say, what they fear, the lies they maintain, and the topics they refuse to discuss. That private state powers a machine-checkable knowledge-boundedness check, an advisory review pass, and a scene-simulation loop where character agents collide to produce dialogue by genuine information asymmetry.

```bash
stoner cast init "Ruth Vann"                       # seed a sheet from canon (no API key)
stoner cast list                                   # who has a sheet, and how much private state
stoner cast show ruth-vann                          # the full ledger, wants, lies, refusals
stoner cast update 3 --auto                          # curate private state from chapter 3
stoner cast check 3                                  # flag anachronistic knowledge (advisory)
stoner cast scene --who ruth-vann,dale-kestner --chapter 3 --brief "They finally talk about the notice."
```

## The privacy model

Canon is the writer's context source *by construction* — `CanonStore.context_pack` walks `canon/` and feeds it straight into the writer prompt. That is exactly why canon cannot hold secrets: a character's unspoken want or active lie that reaches the writer prompt gets narrated onto the page, and subtext dies the moment the narrator can see it.

So private state lives outside canon, in `.stoner/cast/<slug>.json`, and the writer-facing context path never reads it. The public face of a character — appearance, role, voice — stays in `canon/characters/<slug>.md`; the sheet holds only what the narrator must not know. Subtext reaches the manuscript only through scene scripts *you* choose to feed `stoner write N --task`, never automatically.

Checkers — the curator, the boundedness check, and the `interiority` review pass — *do* see private state, because privacy is a writer-facing boundary, not a global one. You see everything via `stoner cast show`.

## The cast sheet

Each sheet is validated JSON:

- **wants** — `stated` (what they say they're after) vs. `real` (the thing driving them they won't name).
- **fears** — what they're afraid of.
- **knowledge** — a ledger of facts, each with an `id` (`k001`, assigned by the harness), the `fact`, `learned_in` (the chapter they learned it; `0` = pre-story backstory), `how` (witnessed / told / inferred / backstory), a supporting `source` quote, and a `secret` flag.
- **lies** — active deceptions: a `claim`, the `truth` it hides (free text or a knowledge id), the `audience`, whether it's still `active`, and the chapter it was `exposed_in`.
- **refusals** — topics the character won't speak to, and why.

`stoner cast init <name>` seeds a sheet from an existing canon character without a model call: it copies the canon `## Wants / Fears` section into `seed_notes` for you to structure. Knowledge and lie ids are always assigned by the harness, never by a model. Human edits are respected — the curator diffs before applying and surfaces conflicts rather than overwriting.

## Maintaining state: `cast update`

After a chapter, `stoner cast update <n>` mirrors the archivist: it reads the chapter, asks the model what each character newly learned, wants, lies about, or refuses, then diffs the result against each sheet. New knowledge is stamped with `learned_in = <n>`. Conflicts — a want shift where wants are already set, a lie exposed in a different chapter, a duplicate fact with a different `learned_in` — are surfaced for you, never auto-resolved. Same-chapter re-runs (routine during redrafts) are idempotent.

`--dry-run` is the default; `--auto` writes. If `cast.auto_update` is on (the default) and cast sheets exist, `stoner write` runs the curator automatically after the archivist stage; a project that never opts into the cast system pays nothing.

## The boundedness check: `cast check`

`stoner cast check <n>` is the machine-checkable core. The model attributes each place a character relies on a fact to the matching knowledge-entry id; then a **pure, deterministic** function diffs `learned_in` against the chapter under review. Any entry a character acts on before they learn it is a violation — **major**, escalating to **critical** when the entry is a secret (acting on an unlearned secret is the worst class of leak). Knowledge no ledger records surfaces as an info-level "unlogged knowledge" finding pointing you at `cast update`.

Because the attribution step is LLM-assisted, every finding here is **advisory and never gates** — the deterministic [slop detector](slop.md) stays the only deterministic-input gate. Reports save to `.stoner/reviews/cast-ch-NN-<ts>.json` (+ `.md`), tagged `kind: "cast"`.

## Scene simulation: `cast scene`

`stoner cast scene --who a,b --chapter N --brief "..."` puts the named characters in a room. Turns proceed deterministic round-robin; each turn is one completion whose system prompt carries **only that character's** private sheet (knowledge bounded to chapter N) plus the public transcript. No character ever sees another's sheet, so information asymmetry is structural. Private notes ("what they think but don't say") go to the transcript only and never reach another agent. The scene ends when everyone passes in a full round, `scene_max_rounds` is hit, or `scene_token_budget` trips; the transcript is written incrementally so an interrupted sim leaves usable state.

Text-only providers (`supports_tools=False`: the codex/claude CLIs) degrade to a single-call role-play — one completion with per-character hidden-state blocks and a strict line protocol, parsed deterministically. It leaks more asymmetry than the multi-call path (one model holds every secret) and is a documented degradation, not parity. `--mode auto|multi|single` overrides the choice.

The sim ends with one assembly call rendering the turn log as a prose dialogue script, printed and saved under `.stoner/cast/scenes/`. **The sim never writes manuscript files** — you feed the script to `stoner write N --task` or draft from it.

## The `interiority` review pass

`interiority` is an additive [review pass](review.md) (not in the default set — opt in via `review_passes:` in `stoner.yaml`). It checks a drafted chapter against the cast sheets for refusals violated without cause, active lies contradicted with no exposure beat, stated-vs-real want collapses, and missed dramatic-irony setups. Advisory only.

## Configuration

One block in `stoner.yaml`:

```yaml
cast:
  auto_update: true        # run the curator after the archivist in `stoner write`
  scene_max_rounds: 8      # cap round-robin turns in a scene sim
  scene_token_budget: 60000
  scene_mode: auto         # auto | multi | single
  sheet_digest_chars: 4000 # cap on a character's bounded digest in scene prompts
```

Scene turns and assembly resolve against the `writer` role; the curator and boundedness attribution resolve against `archivist`.
