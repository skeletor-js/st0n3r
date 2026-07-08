# The production line

`stoner ship` turns a finished manuscript into things you can send: a readiness check, dependency-free EPUB, typeset PDF, submission DOCX, marketing blurbs, and a stitched table-read audio. The deterministic exports need no model and no network; blurbs and model-assisted audio call the writer model; a network TTS backend is explicit opt-in.

```bash
stoner ship check                # readiness report: blockers refuse, warnings inform
stoner ship epub                 # dependency-free, byte-reproducible EPUB3
stoner ship pdf                  # typeset trade-paperback PDF (needs the export extra)
stoner ship docx                 # Shunn submission manuscript (needs the export extra)
stoner ship all                  # the deterministic line: check + EPUB + PDF + DOCX
stoner ship blurbs               # synopsis / query letter / cover brief (calls the model)
stoner ship voices               # rank speakers by dialogue-line count -> voices.yaml
stoner ship audio 1              # stitched table-read WAV for ch-01
```

Everything lands under `export/`. `ship pdf` and `ship docx` need the `export` extra (`pip install 'st0n3r[export]'`, or `[all]`); EPUB and audio are dependency-free.

## Readiness

`stoner ship check` splits its report into **blockers** — genuine not-dones that refuse the ship — and **warnings**, which are surfaced but never refuse:

- **Blockers:** a chapter gap (ch-05 missing), a chapter in a non-shippable status, and — when the [promise ledger](motifs.md) is present — any open promise-kind row, labeled an *unfired gun*. A typed promise left dangling is a real not-done.
- **Warnings:** plain, kind-less open threads. Blocking on those would train you to abandon threads dishonestly, so the check only mentions them.

The promise consume is guarded: on a project without the promise ledger, every open thread degrades to a warning, and the check never errors on one. Deterministic export commands call `require_ready` and refuse on blockers unless you pass `--allow-incomplete` (which records the override in the ledger).

## The exports

- **`ship epub`** writes a dependency-free, **byte-reproducible** EPUB3 — the same manuscript produces the same bytes, which matters for diffing and caching.
- **`ship pdf`** typesets a trade-paperback PDF at `ship.trim` (default US digest 5.5×8.5 inches). Needs the export extra.
- **`ship docx`** writes a Shunn-format submission manuscript; `ship.underline_italics` emits classic Courier-era underlines instead of real italics. Needs the export extra.
- **`ship blurbs`** drafts a synopsis, query letter, and cover brief into `export/` (calls the writer model). `--only synopsis|query|cover` drafts one; drafts are yours once written and won't be overwritten without `--force`.
- **`ship all`** runs the deterministic line — check, EPUB, PDF, DOCX — then prints hints.

Book metadata resolves config → `canon/premise.md` → project name, so an empty `ship.title` falls back to the premise's title heuristic.

## Table-read audio

`stoner ship audio [chapter]` renders a stitched table-read WAV per chapter into `export/audio/`, casting speakers from `export/audio/voices.yaml` (build it first with `stoner ship voices`, which ranks speakers by dialogue-line count). Flags: `--dialogue-only`, `--announce` (speak each speaker's name on change), `--assist` (resolve UNKNOWN speaker lines via the model — advisory), `--force`.

The TTS backend is the one place ship can touch the network, and it never does so silently:

- **`say`** (the default) is local — macOS's built-in synthesizer, no network.
- **`openai`** (`--backend openai`, or `ship.audio.backend: openai`) synthesizes through a vendor API. It's reached only by an explicit choice, and every run records the backend used in the ledger.

## Configuration

```yaml
ship:
  title: ""          # empty -> canon/premise.md title -> project_name
  author: ""
  year: ""
  isbn: ""           # empty -> placeholder in front matter
  contact_lines: []
  dedication: ""
  trim: "5.5x8.5"    # PDF trade-paperback size, WxH inches
  underline_italics: false   # Courier-era underlines in the Shunn DOCX
  audio:
    backend: say             # say (local) | openai (network, opt-in)
    narrator_voice: ""       # empty -> backend's first voice
    announce_speakers: false
    model: gpt-4o-mini-tts   # network-TTS model id, config-overridable
```

`ship blurbs` and `ship audio --assist` resolve against the `writer` role.
