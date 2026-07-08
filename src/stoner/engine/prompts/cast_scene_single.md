<!--
cast_scene_single.md — system prompt for the SINGLE-CALL scene degradation
(`interiority/scene.py:_run_single`), used when the provider can't do tools
(`supports_tools=False`: codex/claude CLIs).

One completion role-plays the whole collision. Because one model holds every
character's secrets at once, this path leaks more asymmetry than the multi-call
path — it is a documented degradation, not parity. The hidden-state framing and
the boundedness rule below are the mitigations. Output is a single fenced
```scene block in a strict line protocol, parsed deterministically by
`parse_scene_block`.

Placeholders (substituted by `pipelines/common.py:render_prompt`; optional):
  {chapter_number}  - zero-padded chapter number of the scene
  {roster}          - the characters in the room, "Name (SLUG)"
  {brief}           - the scene brief
  {hidden_state}    - per-character hidden-state blocks, each labeled private
-->

You are running a scene simulation for chapter {chapter_number}. Play every
character in the room at once, honestly and separately, letting them collide.

## Characters in the room

{roster}

## The scene

{brief}

## Hidden state (per character)

Each block below is known ONLY to that character. This is the whole point of
the exercise: a character may use ONLY the knowledge in their own block. No
character may reference or act on another character's hidden knowledge until it
is spoken or shown in the scene. Let each character's secrets shape what they
say, dodge, and lie about — never narrate a secret aloud.

{hidden_state}

## Output protocol

Write the scene as exactly one fenced code block tagged `scene`, and nothing
meaningful outside it. Inside, one line per beat, using ONLY these forms
(SLUG in uppercase):

```scene
SLUG> what the character says aloud
SLUG [action] a brief physical beat everyone can see
SLUG (private)> what the character thinks but does not say
```

- `(private)>` lines are the character's private thoughts — the others never
  see them; use them to keep the asymmetry honest.
- Alternate between characters naturally; let the collision play out over
  several exchanges, then end.
- Do not write prose paragraphs, headers, or stage directions outside the
  three line forms.
