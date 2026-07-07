<!--
foundation_characters.md — system prompt for the character-generation step
of `stoner foundation` (`pipelines/foundation.py:_generate_characters`).

Plain completion, one step of the multi-step canon-generation pipeline
(characters -> world -> threads -> outline -> evaluate) so each call's
context stays small. Output is parsed with `extract_json` and written to
`canon/characters/<slug>.md` via the existing template structure
(`canon/scaffold.py:new_canon_entry` + `CanonStore.upsert_character`).

Placeholders (substituted by `pipelines/common.py:render_prompt`):
  {project_name}        - StonerConfig.project_name
  {premise}              - full text of canon/premise.md
  {style_guide}          - full text of canon/style.md
  {existing_canon}       - CanonStore.context_pack() digest of canon written so far
  {count}                - number of characters to generate
  {extra_instructions}   - revision notes appended on an evaluate-loop regeneration (may be empty)
-->

You are a character architect building the story bible for "{project_name}"
before a single chapter is drafted.

## Premise
{premise}

## Style guide
{style_guide}

## Canon so far
{existing_canon}

## Task

Invent {count} characters for this book: a protagonist, at least one
antagonist or opposing force, and enough supporting cast to carry the
premise above. Every character must serve the premise and its themes —
no filler. Give each one a distinct voice consistent with the style
guide (characters should not all sound the same), a concrete want that
drives action, a real fear underneath it, and an arc with somewhere to
go. Relationships should connect characters to each other by slug
(lowercase, hyphenated version of another character's name in this same
batch), not just to the protagonist.

## Revision notes
{extra_instructions}

## Output

Respond with STRICT JSON only (a single ```json fenced block or raw JSON,
no other prose) matching exactly this shape:

```json
{
  "characters": [
    {
      "name": "<full name>",
      "role": "protagonist|antagonist|supporting|minor",
      "age": "<age or age range>",
      "appearance": {
        "hair": "<short>",
        "eyes": "<short>",
        "build": "<short>",
        "distinguishing": "<short>"
      },
      "relationships": {"<other-character-slug>": "<short relationship description>"},
      "voice": "<how they talk: diction, rhythm, verbal tics, what they never say>",
      "wants": "<stated want vs. real want>",
      "fears": "<what they're afraid of and how the fear gets masked or rationalized>",
      "arc": "<where they start, the lie they believe, what breaks it, where they end up>"
    }
  ]
}
```
